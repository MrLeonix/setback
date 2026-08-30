"""Abuse guards for the resident-facing console: rate limits, a global
concurrent-tribunal cap, and a daily ledger-spend breaker.

Three independent guards, each a small, framework-light dependency/function
with no reference to `setback.console.app` (off this work package's lane --
see the module docstring's "wiring" note below for the exact snippet
`console/app.py` needs).

1. **Rate limiting** (:class:`SlidingWindowRateLimiter`,
   :func:`per_ip_case_creation_guard`, :func:`per_case_interview_turn_guard`):
   a per-IP sliding window on ``POST /api/cases`` (default 5/hour) and a
   per-case sliding window on interview turns (default 60/day).
2. **Concurrency** (:func:`enforce_concurrent_tribunal_cap`): refuses a new
   tribunal run once `max_concurrent` (default 2) are already in progress,
   determined from the case store's own event log -- this works whether the
   actual `setback-tribunal` execution runs in-process
   (`console.app.LocalPipelineJobTrigger`, local/dev only) or in a separate
   Cloud Run Job container (production): both write through the same
   `CaseStore`.
3. **Spend** (:func:`enforce_daily_spend_budget`): sums today's ledger
   spend across every case in the store and refuses a new tribunal run past
   a daily ceiling (default $5.00), independent of any single run's own
   per-run ledger ceiling (`state.ledger.DEFAULT_RUN_CEILING_USD`).

**Wiring for `console/app.py`** (agent B's lane -- not applied here; the
exact patch, for the integrator):

```python
from setback.console.guards import (
    per_ip_case_creation_guard,
    per_case_interview_turn_guard,
    enforce_concurrent_tribunal_cap,
    enforce_daily_spend_budget,
)

_case_creation_guard = per_ip_case_creation_guard()
_interview_turn_guard = per_case_interview_turn_guard()

@app.post("/api/cases", status_code=201, dependencies=[Depends(_case_creation_guard)])
async def create_case(body: CreateCaseRequest) -> dict[str, Any]:
    ...

@app.post(
    "/api/cases/{case_id}/interview", dependencies=[Depends(_interview_turn_guard)]
)
async def answer_interview(case_id: str, body: InterviewAnswerRequest) -> dict[str, Any]:
    ...

@app.post("/api/cases/{case_id}/tribunal", status_code=202)
async def start_tribunal(case_id: str) -> dict[str, Any]:
    await _require_case(case_id)
    await enforce_concurrent_tribunal_cap(store)
    await enforce_daily_spend_budget(store)
    ...  # existing body unchanged from here
```

(`Depends` and `HTTPException` both already import from `fastapi` in
`console/app.py`; only `Depends` needs adding to that existing import line.)
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Final, Protocol

from fastapi import HTTPException, Request

from setback.state.guard_store import GuardCounterStore, GuardTotals, GuardTotalsStore

# --- (a) sliding-window rate limiting ---------------------------------------

DEFAULT_CASE_CREATION_LIMIT: Final[int] = 5
DEFAULT_CASE_CREATION_WINDOW_SECONDS: Final[float] = 3600.0
"""5 new cases per hour, per source IP."""

DEFAULT_INTERVIEW_TURN_LIMIT: Final[int] = 60
DEFAULT_INTERVIEW_TURN_WINDOW_SECONDS: Final[float] = 86400.0
"""60 interview turns per day, per case."""


@dataclass
class SlidingWindowRateLimiter:
    """A plain, in-memory sliding-window counter keyed by an arbitrary
    string (a source IP, a case id, ...).

    Not distributed and not durable across a process restart -- correct for
    a single Cloud Run Service instance, which is this build's whole
    deployment topology (STATUS.md/ARCHITECTURE.md: `setback-console` is one
    scale-to-zero service, not a fleet); a future multi-instance deployment
    would need a shared store (Firestore, Redis) instead, out of this work
    package's scope.
    """

    limit: int
    window_seconds: float
    clock: Callable[[], float] = time.monotonic
    _hits: dict[str, deque[float]] = field(default_factory=dict, init=False, repr=False)

    def allow(self, key: str) -> bool:
        """Record one attempt for `key` and return whether it is allowed
        under the limit. Every call (allowed or not) advances the window;
        a refused attempt is not itself counted as a hit."""
        now = self.clock()
        window_start = now - self.window_seconds
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= window_start:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True

    def hit_count(self, key: str) -> int:
        """The number of hits currently counted against `key`'s window
        (for tests/observability; does not itself prune or advance time)."""
        return len(self._hits.get(key, ()))


class RateLimitExceeded(HTTPException):
    """429 with a resident-readable explanation and a `Retry-After` header
    sized to the limiter's own window."""

    def __init__(self, *, detail: str, retry_after_seconds: float) -> None:
        super().__init__(
            status_code=429,
            detail=detail,
            headers={"Retry-After": str(int(retry_after_seconds))},
        )


def _client_ip(request: Request) -> str:
    """Best-effort caller IP for rate-limit keying.

    `request.client.host` is set by the ASGI server from the real
    connecting socket (Cloud Run terminates TLS and forwards the real
    client address); a missing client (some test transports) falls back to
    a single shared `"unknown"` bucket -- an acceptable, documented
    approximation for a request-shape this guard is not the primary
    defense against (Cloud Run's own DDoS protections sit in front of it),
    not a claim of exact attribution.
    """
    if request.client is not None and request.client.host:
        return request.client.host
    return "unknown"


def per_ip_case_creation_guard(
    limiter: SlidingWindowRateLimiter | None = None,
    *,
    docket_key_provider: Callable[[], str | None] | None = None,
) -> Callable[[Request], None]:
    """Build a FastAPI dependency enforcing a per-IP sliding-window limit on
    case creation. Wire with `Depends(...)` on `POST /api/cases`.

    `docket_key_provider`: when given, a request carrying a verified
    privileged-session cookie (see `is_privileged_request`) bypasses this
    limiter too -- "privileged requests bypass ALL limits" (DESIGN SPEC
    point 1). Left `None` by default (this limiter's own pre-existing test
    suite calls it that way, against a bare `SimpleNamespace(client=...)`
    fake request with no `.cookies` attribute at all) so behaviour here is
    unchanged unless a caller opts in -- `console/app.py`'s production
    wiring does."""
    active_limiter = limiter or SlidingWindowRateLimiter(
        limit=DEFAULT_CASE_CREATION_LIMIT,
        window_seconds=DEFAULT_CASE_CREATION_WINDOW_SECONDS,
    )

    def _dependency(request: Request) -> None:
        if docket_key_provider is not None and is_privileged_request(
            request, docket_key_provider=docket_key_provider
        ):
            return
        key = _client_ip(request)
        if not active_limiter.allow(key):
            raise RateLimitExceeded(
                detail=(
                    f"too many cases created from this address; limit is "
                    f"{active_limiter.limit} per {int(active_limiter.window_seconds)}s"
                ),
                retry_after_seconds=active_limiter.window_seconds,
            )

    return _dependency


def per_case_interview_turn_guard(
    limiter: SlidingWindowRateLimiter | None = None,
) -> Callable[[str], None]:
    """Build a FastAPI dependency enforcing a per-case sliding-window limit
    on interview turns. Wire with `Depends(...)` on
    `POST /api/cases/{case_id}/interview` -- FastAPI resolves the
    dependency's `case_id` parameter from the route's own path parameter of
    the same name automatically."""
    active_limiter = limiter or SlidingWindowRateLimiter(
        limit=DEFAULT_INTERVIEW_TURN_LIMIT,
        window_seconds=DEFAULT_INTERVIEW_TURN_WINDOW_SECONDS,
    )

    def _dependency(case_id: str) -> None:
        if not active_limiter.allow(case_id):
            raise RateLimitExceeded(
                detail=(
                    f"this case has exceeded {active_limiter.limit} interview turns per "
                    f"{int(active_limiter.window_seconds)}s"
                ),
                retry_after_seconds=active_limiter.window_seconds,
            )

    return _dependency


# --- (b) global concurrent-tribunal cap -------------------------------------


class _CaseRecordLike(Protocol):
    @property
    def case_id(self) -> str: ...

    @property
    def created_at(self) -> datetime: ...


class _EventLike(Protocol):
    @property
    def event_type(self) -> str: ...

    @property
    def sequence(self) -> int: ...

    @property
    def recorded_at(self) -> datetime: ...

    @property
    def payload(self) -> Mapping[str, Any]: ...


class CaseStoreLike(Protocol):
    """The narrow slice of `state.firestore.CaseStore` these guards need --
    declared locally (rather than importing the real `CaseStore` Protocol)
    so this module has no import-time coupling to `state/firestore.py`
    beyond the structural shape it actually calls, and so a test double
    only needs to satisfy these four methods."""

    async def list_cases(self, limit: int = 50) -> tuple[_CaseRecordLike, ...]: ...

    async def list_events(self, case_id: str) -> tuple[_EventLike, ...]: ...

    async def load_ledger(self, case_id: str) -> object | None: ...


DEFAULT_MAX_CONCURRENT_TRIBUNALS: Final[int] = 2

_TRIBUNAL_START_EVENT: Final[str] = "tribunal_requested"
_TRIBUNAL_TERMINAL_EVENTS: Final[frozenset[str]] = frozenset(
    {"submission_composed", "job_failed", "tribunal_rerun_ignored"}
)
"""Events `job/pipeline.py`/`job/main.py` record that end a tribunal run,
successfully or not (see their module docstrings) -- a case counts as
"still running" when it has a start event with no later terminal event.

`tribunal_rerun_ignored` is included alongside the two "a run actually
happened" outcomes because it means the opposite of "running": it is
`RealPipelineRunner.run`'s idempotency no-op (see its docstring) for a
second "Start tribunal" press against an already-decided case -- nothing
executes, so no new `submission_composed`/`job_failed` is ever written.
Live incident (2026-08-29, film-day): without this, that request's
`tribunal_requested` outlives the case's one and only terminal event and
the case counts as "running" forever, permanently burning a concurrency
slot for a run that never started -- exactly what happened to both
canonical demo cases at once, exhausting `DEFAULT_MAX_CONCURRENT_TRIBUNALS`
(2) for every other case."""

DEFAULT_STALE_RUN_TTL_SECONDS: Final[float] = 15 * 60.0
"""15 minutes -- longer than any documented legitimate run (ARCHITECTURE.md
§3 describes the pipeline as "a multi-minute, multi-model-call batch job",
not a multi-*ten*-minute one).

DESIGN-DECISIONS.md and ARCHITECTURE.md §4 both say plainly there is no
sweeper: a crashed/OOM-killed/timed-out job execution leaves its case stuck
mid-run "with no automated recovery". A crash never gets to record a
terminal event, so that case's `tribunal_requested` would otherwise count
as "running" forever -- one crash permanently halves
`DEFAULT_MAX_CONCURRENT_TRIBUNALS` (2), and a second wedges it to zero,
with nothing but a manual re-trigger able to clear it. This TTL is the
guard's own count-side mitigation for that documented gap: a start event
older than this stops counting toward the cap, so a crashed run's
concurrency slot recovers on its own instead of staying wedged. It does not
touch the store -- a genuinely still-running job keeps running; it is
simply no longer charged against capacity past this age."""


async def count_running_tribunals(
    store: CaseStoreLike,
    *,
    case_limit: int = 200,
    now: datetime | None = None,
    stale_run_ttl_seconds: float = DEFAULT_STALE_RUN_TTL_SECONDS,
) -> int:
    """Count cases with a `tribunal_requested` event newer (higher
    `sequence`) than their latest terminal event, or with no terminal event
    at all -- excluding one whose `tribunal_requested` is older than
    `stale_run_ttl_seconds` (see :data:`DEFAULT_STALE_RUN_TTL_SECONDS`).

    `case_limit` bounds how many of the most recently created cases
    (`CaseStore.list_cases` is already sorted newest-first) are checked --
    a documented simplification appropriate to this single-demo-case
    hackathon build's data volume, not a claim of exhaustive correctness at
    an arbitrary scale.
    """
    current_time = now or datetime.now(UTC)
    cases = await store.list_cases(limit=case_limit)
    running = 0
    for case in cases:
        events = await store.list_events(case.case_id)
        start_event = max(
            (e for e in events if e.event_type == _TRIBUNAL_START_EVENT),
            key=lambda e: e.sequence,
            default=None,
        )
        if start_event is None:
            continue
        terminal_sequence = max(
            (e.sequence for e in events if e.event_type in _TRIBUNAL_TERMINAL_EVENTS),
            default=None,
        )
        if terminal_sequence is not None and terminal_sequence >= start_event.sequence:
            continue
        age_seconds = (current_time - start_event.recorded_at).total_seconds()
        if age_seconds >= stale_run_ttl_seconds:
            continue
        running += 1
    return running


class TribunalCapacityExceeded(HTTPException):
    """429: the global concurrent-tribunal cap is currently full."""

    def __init__(self, *, max_concurrent: int) -> None:
        super().__init__(
            status_code=429,
            detail=(
                f"the tribunal is at capacity ({max_concurrent} run(s) in progress); "
                "please try again shortly"
            ),
        )


async def enforce_concurrent_tribunal_cap(
    store: CaseStoreLike, *, max_concurrent: int = DEFAULT_MAX_CONCURRENT_TRIBUNALS
) -> None:
    """Raise :class:`TribunalCapacityExceeded` if `max_concurrent` tribunal
    runs are already in progress. Call before recording a new
    `tribunal_requested` event / triggering the job."""
    if await count_running_tribunals(store) >= max_concurrent:
        raise TribunalCapacityExceeded(max_concurrent=max_concurrent)


# --- (c) daily spend breaker -------------------------------------------------

DEFAULT_DAILY_SPEND_CEILING_USD: Final[float] = 5.0


def _as_utc_date(value: datetime) -> date:
    if value.tzinfo is None:
        return value.date()
    return value.astimezone(UTC).date()


async def todays_ledger_spend_usd(
    store: CaseStoreLike, *, today: date | None = None, case_limit: int = 200
) -> float:
    """Sum `total_cost_usd` across every case's ledger, for cases *created*
    today (UTC).

    **Documented approximation.** `state.ledger.CallRecord` records no
    per-call timestamp, only a per-case `Ledger` snapshot -- there is no way
    to attribute an individual call to a calendar day more precisely than
    the case it belongs to. This counts a case's *entire* ledger under the
    day it was *created*; a case whose tribunal run happens to straddle
    midnight would have its post-midnight calls counted against
    "yesterday". Acceptable at this build's single-case-at-a-time scale;
    a real per-call timestamp on `state.firestore.LedgerCallSnapshot` would
    remove the approximation, and is reported to the integrator as a
    `state/firestore.py` follow-up (off this work package's lane).
    """
    target_day = today or datetime.now(UTC).date()
    cases = await store.list_cases(limit=case_limit)
    total = 0.0
    for case in cases:
        if _as_utc_date(case.created_at) != target_day:
            continue
        ledger = await store.load_ledger(case.case_id)
        if ledger is not None:
            total += ledger.total_cost_usd  # type: ignore[attr-defined]
    return total


class DailySpendExceeded(HTTPException):
    """429: today's summed ledger spend has reached the daily ceiling."""

    def __init__(self, *, spent_usd: float, ceiling_usd: float) -> None:
        super().__init__(
            status_code=429,
            detail=(
                f"today's tribunal spend (${spent_usd:.2f}) has reached the "
                f"${ceiling_usd:.2f}/day limit; new tribunal runs resume tomorrow"
            ),
        )


async def enforce_daily_spend_budget(
    store: CaseStoreLike, *, daily_ceiling_usd: float = DEFAULT_DAILY_SPEND_CEILING_USD
) -> None:
    """Raise :class:`DailySpendExceeded` if today's summed ledger spend has
    already reached `daily_ceiling_usd`. Call alongside
    :func:`enforce_concurrent_tribunal_cap` in the tribunal-start route."""
    spent = await todays_ledger_spend_usd(store)
    if spent >= daily_ceiling_usd:
        raise DailySpendExceeded(spent_usd=spent, ceiling_usd=daily_ceiling_usd)


# --- (d) public-abuse guard: the privileged (judge/founder) session --------
#
# "Layered + key bypass" (founder-selected design, 2026-08-29): the docket
# board's existing key gate (`console/app.py`'s `_docket_key_accepted`)
# doubles as the privileged-session grant. Opening `/docket` with a VALID
# key sets an `sb_priv` cookie (`console/app.py`'s `docket_board` route);
# every guard function below treats a request carrying a *verified* copy of
# that cookie as exempt from every limit in this module.
#
# The cookie is never the docket key itself, nor derivable back into it: it
# is `HMAC-SHA256(key=docket_key_bytes, msg="setback-privileged-v1")`, so
# rotating `SETBACK_DOCKET_KEY` (e.g. after a suspected leak) naturally
# invalidates every cookie issued under the old key -- a new key derives a
# different HMAC, and the old cookie value simply stops verifying. There is
# no separate revocation list to maintain.
#
# SECURITY-REVIEW NOTE (2026-08-30), full blast radius of a rotation: the
# per-client daily case-creation cap below (`hashed_client_id` /
# `_client_identity_salt`) derives ITS salt from this exact same docket key
# (per the design spec, "salt is derived in-process from the docket key").
# A rotation therefore does two things, not one: it invalidates every
# privileged cookie (intended, documented above) AND it silently resets
# every anonymous actor's per-client daily-cap counter to a fresh bucket --
# a client hash under the new salt shares no history with its hash under
# the old one, so `guard_counters/case_<old-hash>_<yyyymmdd>` becomes
# unreachable and every actor effectively gets a new 5-per-day allowance
# the moment the key changes. This is a real, if bounded, side effect: it
# does NOT touch the global spend ceiling or either count backstop
# (`guard_totals/public` is not keyed by the docket key at all), and does
# NOT touch the older, independent hourly per-IP limiter
# (`per_ip_case_creation_guard`, keyed by raw IP, no salt) -- both keep
# enforcing exactly as before. Left as spec-compliant (not silently
# reworked to a separately-rotatable salt) rather than introducing a new
# secret to provision under tonight's deploy window; a founder rotating the
# key for a real leak should expect this reset as a secondary effect, not
# just the (larger, intended) cookie invalidation. Pinned by
# `test_rotating_the_docket_key_also_resets_the_per_client_daily_cap` and
# `test_rotating_the_docket_key_does_not_affect_the_global_ceiling` in
# `tests/console/test_guards.py`.

PRIVILEGED_COOKIE_NAME: Final[str] = "sb_priv"
PRIVILEGED_COOKIE_MAX_AGE_SECONDS: Final[int] = 60 * 24 * 3600
"""60 days -- long enough that a judge or the founder, once let in during
the hackathon window, never has to re-enter the docket key."""

_PRIVILEGED_HMAC_MESSAGE: Final[bytes] = b"setback-privileged-v1"


def privileged_cookie_value(docket_key: str) -> str:
    """The `sb_priv` cookie value for a valid docket key. See the section
    docstring above for why an HMAC rather than the key itself."""
    return hmac.new(docket_key.encode(), _PRIVILEGED_HMAC_MESSAGE, hashlib.sha256).hexdigest()


def is_privileged_cookie_valid(cookie_value: str | None, docket_key: str | None) -> bool:
    """Constant-time verification (`hmac.compare_digest`) of a `sb_priv`
    cookie value against the console's current `docket_key`.

    `docket_key` unset (no `SETBACK_DOCKET_KEY` configured -- local dev, or
    any test that never sets it) never counts as privileged even with a
    cookie present: with the docket gate itself disabled in that mode
    (`console/app.py`'s `_docket_key_accepted`), there is no real secret a
    cookie could ever have been issued to prove knowledge of.
    """
    if not docket_key or not cookie_value:
        return False
    expected = privileged_cookie_value(docket_key)
    return hmac.compare_digest(cookie_value, expected)


def is_privileged_request(
    request: Request, *, docket_key_provider: Callable[[], str | None]
) -> bool:
    """True when `request` carries a verified `sb_priv` cookie for the
    console's current docket key (read fresh from `docket_key_provider` on
    every call -- never cached across a key rotation). Every guard
    dependency below calls this first and returns early, unchecked, when it
    is `True`."""
    cookie_value = request.cookies.get(PRIVILEGED_COOKIE_NAME)
    return is_privileged_cookie_valid(cookie_value, docket_key_provider())


# --- (e) public-abuse guard: per-actor identity, never a raw IP ------------


def public_guard_client_ip(request: Request) -> str:
    """Best-effort caller IP for the public-abuse guard's per-actor caps,
    Cloud-Run-aware: Google Front End appends the true client address as
    the LAST entry of `X-Forwarded-For` on every request it forwards to a
    Cloud Run service -- every earlier entry is client-supplied and
    therefore forgeable (a client can send its own `X-Forwarded-For` header
    with any leading addresses it likes; GFE always appends the real one
    after whatever the client sent), so only the last entry is trustworthy.

    Kept as its own function rather than folded into the existing
    `_client_ip` above (used by the hourly per-IP case-creation limiter):
    that function's own test suite (`_fake_request`, a bare
    `SimpleNamespace(client=...)` with no `headers` attribute at all)
    predates this Cloud-Run header-parsing need, and changing it would
    require touching every one of those call sites for no behavioural gain
    there. Falls back to `request.client.host`, then a shared `"unknown"`
    bucket, exactly like `_client_ip`.

    SECURITY-REVIEW NOTE (2026-08-30), UNVERIFIED PLATFORM ASSUMPTION --
    read before trusting this in production: "the last entry is always the
    true client IP" is the DESIGN SPEC's own instruction, not something
    re-derived here, and it is NOT uniformly true across every Google
    Cloud ingress shape. Google's own External Application Load Balancer
    docs (docs.cloud.google.com/load-balancing/docs/https,
    "X-Forwarded-For header" section, checked 2026-08-30) state it
    *appends two* addresses -- the client IP, THEN the load balancer's own
    forwarding-rule IP -- which would make the true client IP the
    SECOND-TO-LAST entry, with the (constant, shared-by-every-request)
    load-balancer IP last; several independent write-ups of bare Cloud Run
    (no separate external HTTPS Load Balancer resource in front of the
    `*.run.app`/custom-domain ingress) instead describe GFE appending only
    one address, making last correct -- and at least one names a *third*
    shape again (Firebase Hosting in front of Cloud Run adds Fastly as yet
    another hop, shifting the true client IP to third-from-last). This
    module cannot tell which ingress shape `setback-console` actually runs
    behind from inside the request handler. Getting this wrong is not a
    minor bug: if the last entry actually turns out to be a constant
    infrastructure IP rather than the client's, `hashed_client_id` collapses
    *every* anonymous visitor into one shared identity, and the "5 cases
    per client per day" cap would then apply to the whole public site
    combined rather than to each visitor -- a functional outage, not a
    security hole, but arguably worse for launch night. VERIFY BEFORE
    RELYING ON THIS: after the one authorized production deploy, hit the
    live service from two different real networks and confirm (server-side
    only, never logged/printed with real values) that this function
    returns two different values for those two requests, and that a
    hand-forged leading `X-Forwarded-For` entry does not change the
    result. Left as "last entry" here (spec-compliant, not silently
    changed to "second-to-last") because the secondary sources found
    during this review conflict with each other and neither is a
    Cloud-Run-specific first-party statement definitive enough to justify
    silently overriding the founder's explicit instruction on this branch.
    """
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        candidates = [part.strip() for part in forwarded_for.split(",") if part.strip()]
        if candidates:
            return candidates[-1]
    if request.client is not None and request.client.host:
        return request.client.host
    return "unknown"


_CLIENT_SALT_LABEL: Final[bytes] = b"setback-client-salt-v1"


def _client_identity_salt(docket_key: str | None) -> bytes:
    """A per-deployment salt derived in-process from the docket key (never
    stored, never logged) -- so `hashed_client_id` below never needs to
    persist or transmit a raw IP anywhere (GDPR/PII rule). Falls back to a
    fixed, documented seed when no docket key is configured (local dev, or
    a test that never sets `SETBACK_DOCKET_KEY`); production always runs
    with a real docket key configured, so this fallback never actually
    governs a live deployment -- it only keeps this guard functional in an
    environment with no key at all."""
    seed = (docket_key or "setback-no-docket-key-configured").encode()
    return hmac.new(seed, _CLIENT_SALT_LABEL, hashlib.sha256).digest()


def hashed_client_id(client_ip: str, *, docket_key: str | None) -> str:
    """`sha256(salt + client_ip)`, safe to use as (part of) a Firestore
    document id or field -- the raw IP itself is never stored, logged, or
    transmitted anywhere past this function's own stack frame."""
    salt = _client_identity_salt(docket_key)
    return hashlib.sha256(salt + client_ip.encode()).hexdigest()


# --- (f) public-abuse guard: per-actor caps ---------------------------------

MAX_CASES_PER_CLIENT_PER_DAY: Final[int] = int(os.environ.get("MAX_CASES_PER_CLIENT_PER_DAY", "5"))
"""5 new cases per client per rolling day, keyed by `hashed_client_id` --
independent of, and layered underneath, the existing hourly per-IP limiter
(`per_ip_case_creation_guard`) above."""

MAX_INTERVIEW_TURNS_PER_CASE: Final[int] = int(os.environ.get("MAX_INTERVIEW_TURNS_PER_CASE", "30"))
"""30 interview turns per case, counted server-side from this case's own
stored `interview_turn` events -- not a new counter, so a resumed session
(a fresh process, no in-memory rate-limiter state) is still capped
correctly."""

MAX_UPLOADS_PER_CASE: Final[int] = int(os.environ.get("MAX_UPLOADS_PER_CASE", "5"))
"""5 uploads per case, counted server-side from this case's own stored
`document_uploaded` events, the same way as `MAX_INTERVIEW_TURNS_PER_CASE`."""

MAX_REFUSAL_FEEDBACK_PER_CASE: Final[int] = int(
    os.environ.get("MAX_REFUSAL_FEEDBACK_PER_CASE", "10")
)
"""10 refusal-feedback submissions per case (security-review finding,
2026-08-30): `POST /api/cases/{case_id}/grounds/{ground_id}/feedback`
(`console/app.py`'s `refusal_feedback`) makes one real model call per
request (`interview.flow.capture_refusal_feedback` -> `composer.compose`)
and, unlike every other model-calling mutating route, originally shipped
with no cap or ceiling gate at all -- distinct `ground_id`/`pushback` pairs
are never deduplicated against each other (only an exact repeat is a
no-op, and its model call still runs before that dedup check), so an
anonymous actor in a tight loop against this one endpoint could exhaust
the entire public demo budget by itself. Counted server-side from this
case's own stored `resident_refusal_feedback` events, the same way as
`MAX_UPLOADS_PER_CASE`."""


def per_client_daily_case_cap_guard(
    counter_store: GuardCounterStore,
    *,
    docket_key_provider: Callable[[], str | None],
    limit: int = MAX_CASES_PER_CLIENT_PER_DAY,
    now: Callable[[], datetime] | None = None,
) -> Callable[[Request], Awaitable[None]]:
    """Build a FastAPI dependency enforcing `limit` new cases per client
    (`hashed_client_id`) per rolling UTC day, backed by `counter_store`.
    Wire with `Depends(...)` on `POST /api/cases`, alongside the existing
    hourly `per_ip_case_creation_guard`. A privileged request bypasses this
    entirely (see `is_privileged_request`)."""
    clock = now or (lambda: datetime.now(UTC))

    async def _dependency(request: Request) -> None:
        if is_privileged_request(request, docket_key_provider=docket_key_provider):
            return
        docket_key = docket_key_provider()
        client_hash = hashed_client_id(public_guard_client_ip(request), docket_key=docket_key)
        allowed = await counter_store.try_increment_daily(
            "case", client_hash, day=clock().date(), limit=limit
        )
        if not allowed:
            raise RateLimitExceeded(
                detail=(
                    f"too many cases created from this address today; limit is {limit} per day"
                ),
                retry_after_seconds=86400.0,
            )

    return _dependency


def _resident_turn_count(events: tuple[_EventLike, ...]) -> int:
    return sum(
        1
        for e in events
        if e.event_type == "interview_turn" and e.payload.get("role") == "resident"
    )


def per_case_interview_turn_cap_guard(
    store: CaseStoreLike,
    *,
    docket_key_provider: Callable[[], str | None],
    limit: int = MAX_INTERVIEW_TURNS_PER_CASE,
) -> Callable[[Request, str], Awaitable[None]]:
    """Build a FastAPI dependency enforcing `limit` interview turns per
    case, counted from this case's own stored events (see
    `MAX_INTERVIEW_TURNS_PER_CASE`). Wire with `Depends(...)` on
    `POST /api/cases/{case_id}/interview` -- FastAPI resolves both `request`
    and the route's own `case_id` path parameter automatically."""

    async def _dependency(request: Request, case_id: str) -> None:
        if is_privileged_request(request, docket_key_provider=docket_key_provider):
            return
        events = await store.list_events(case_id)
        if _resident_turn_count(events) >= limit:
            raise RateLimitExceeded(
                detail=(
                    f"this case has reached {limit} interview turns; "
                    "it can still be browsed, just not extended further"
                ),
                retry_after_seconds=0.0,
            )

    return _dependency


def per_case_upload_cap_guard(
    store: CaseStoreLike,
    *,
    docket_key_provider: Callable[[], str | None],
    limit: int = MAX_UPLOADS_PER_CASE,
) -> Callable[[Request, str], Awaitable[None]]:
    """Build a FastAPI dependency enforcing `limit` uploads per case,
    counted from this case's own stored `document_uploaded` events. Wire
    with `Depends(...)` on `POST /api/cases/{case_id}/documents`."""

    async def _dependency(request: Request, case_id: str) -> None:
        if is_privileged_request(request, docket_key_provider=docket_key_provider):
            return
        events = await store.list_events(case_id)
        upload_count = sum(1 for e in events if e.event_type == "document_uploaded")
        if upload_count >= limit:
            raise RateLimitExceeded(
                detail=f"this case has reached the {limit}-upload limit",
                retry_after_seconds=0.0,
            )

    return _dependency


def per_case_feedback_cap_guard(
    store: CaseStoreLike,
    *,
    docket_key_provider: Callable[[], str | None],
    limit: int = MAX_REFUSAL_FEEDBACK_PER_CASE,
) -> Callable[[Request, str], Awaitable[None]]:
    """Build a FastAPI dependency enforcing `limit` refusal-feedback
    submissions per case, counted from this case's own stored
    `resident_refusal_feedback` events (see
    `MAX_REFUSAL_FEEDBACK_PER_CASE`). Wire with `Depends(...)` on
    `POST /api/cases/{case_id}/grounds/{ground_id}/feedback`."""

    async def _dependency(request: Request, case_id: str) -> None:
        if is_privileged_request(request, docket_key_provider=docket_key_provider):
            return
        events = await store.list_events(case_id)
        feedback_count = sum(1 for e in events if e.event_type == "resident_refusal_feedback")
        if feedback_count >= limit:
            raise RateLimitExceeded(
                detail=f"this case has reached the {limit}-feedback-submission limit",
                retry_after_seconds=0.0,
            )

    return _dependency


# --- (g) public-abuse guard: the global public-spend ceiling ---------------

PUBLIC_SPEND_CEILING_USD: Final[float] = float(os.environ.get("PUBLIC_SPEND_CEILING_USD", "26.00"))
"""~AUD$40 at the time this ceiling was set (the founder's own number,
2026-08-29) -- USD is this codebase's native pricing currency
(`state.ledger.PRICING_USD_PER_MILLION_TOKENS`), so the ceiling is
expressed in USD and converted once at configuration time rather than
carrying a live FX conversion in the hot path."""

PUBLIC_TURN_COST_ESTIMATE_USD: Final[float] = float(
    os.environ.get("PUBLIC_TURN_COST_ESTIMATE_USD", "0.001")
)
"""A conservative flat estimate booked against the aggregate for every
anonymous interview turn -- the interview model itself
(`config.INTERVIEW`, `gemini-3.5-flash-lite`) is cheap enough that a real
per-call price here would be more precision than this soft ceiling needs."""

STREET_VIEW_FETCH_COST_USD: Final[float] = float(
    os.environ.get("STREET_VIEW_FETCH_COST_USD", "0.007")
)
"""Booked per Street View Static API fetch (`evidence.imagery.
fetch_street_view_fallback`) -- a real, metered Google Maps Platform cost,
distinct from model spend."""

MAX_ANONYMOUS_CASES_TOTAL: Final[int] = int(os.environ.get("MAX_ANONYMOUS_CASES_TOTAL", "5000"))
MAX_ANONYMOUS_TURNS_TOTAL: Final[int] = int(os.environ.get("MAX_ANONYMOUS_TURNS_TOTAL", "100000"))
"""Hard count backstops, independent of the dollar ceiling -- catch a
pathological cheap-request flood (e.g. an empty-answer interview loop) that
`PUBLIC_SPEND_CEILING_USD` alone might not reach quickly."""

_THRESHOLD_PCTS: Final[tuple[int, ...]] = (50, 80, 100)


class PublicGuardPaused(HTTPException):
    """429: the public demo's spend ceiling or a hard count backstop has
    been reached. Only ever wired onto an anonymous *mutating* route (see
    `console/app.py`) -- every read stays open regardless. The detail
    string is the same honest, plain-English copy the landing/case-page
    banner uses (WRITING-STYLE-GUIDE.md-compliant); it never mentions the
    key/bypass mechanism."""

    def __init__(self) -> None:
        super().__init__(
            status_code=429,
            detail=(
                "the public demo budget for this hackathon build has been used up; "
                "every existing case stays open to browse, new interactions are paused"
            ),
        )


def is_public_guard_paused(
    totals: GuardTotals,
    *,
    ceiling_usd: float = PUBLIC_SPEND_CEILING_USD,
    max_cases: int = MAX_ANONYMOUS_CASES_TOTAL,
    max_turns: int = MAX_ANONYMOUS_TURNS_TOTAL,
) -> bool:
    """True once `totals` has reached the dollar ceiling or either hard
    count backstop. Pure and synchronous so both the FastAPI dependency
    (`public_guard_dependency`) and the landing/case-page renderers can
    share one definition of "paused"."""
    return (
        totals.spend_usd >= ceiling_usd
        or totals.anonymous_cases >= max_cases
        or totals.anonymous_turns >= max_turns
    )


@dataclass
class CachedGuardTotalsReader:
    """Wraps a `GuardTotalsStore` with a short in-process TTL cache (<=60s
    per the design spec) so every anonymous mutating request, plus every
    landing/case-page render, doesn't hit Firestore just to check whether
    the public demo is paused. One instance is shared for the lifetime of a
    console process (see `console/app.py`'s `create_app`)."""

    store: GuardTotalsStore
    ttl_seconds: float = 30.0
    clock: Callable[[], float] = time.monotonic
    _cached: GuardTotals | None = field(default=None, init=False, repr=False)
    _cached_at: float = field(default=-1.0, init=False, repr=False)

    async def get_totals(self) -> GuardTotals:
        now = self.clock()
        if self._cached is not None and (now - self._cached_at) < self.ttl_seconds:
            return self._cached
        totals = await self.store.get_totals()
        self._cached = totals
        self._cached_at = now
        return totals

    def invalidate(self) -> None:
        """Force the next `get_totals()` to re-read `store` -- called right
        after this process itself books a cost against the aggregate, so a
        request handled by this same instance never has to wait out the
        full TTL to see its own write."""
        self._cached = None
        self._cached_at = -1.0


def public_guard_dependency(
    totals_reader: CachedGuardTotalsReader,
    *,
    docket_key_provider: Callable[[], str | None],
    ceiling_usd: float = PUBLIC_SPEND_CEILING_USD,
    max_cases: int = MAX_ANONYMOUS_CASES_TOTAL,
    max_turns: int = MAX_ANONYMOUS_TURNS_TOTAL,
) -> Callable[[Request], Awaitable[None]]:
    """Build a FastAPI dependency raising `PublicGuardPaused` once the
    public demo is paused, bypassed entirely by a privileged request. Wire
    with `Depends(...)` on every anonymous mutating route: create case,
    interview turn, upload, start tribunal."""

    async def _dependency(request: Request) -> None:
        if is_privileged_request(request, docket_key_provider=docket_key_provider):
            return
        totals = await totals_reader.get_totals()
        if is_public_guard_paused(
            totals, ceiling_usd=ceiling_usd, max_cases=max_cases, max_turns=max_turns
        ):
            raise PublicGuardPaused()

    return _dependency


async def record_threshold_events_if_crossed(
    totals_store: GuardTotalsStore,
    totals: GuardTotals,
    *,
    ceiling_usd: float = PUBLIC_SPEND_CEILING_USD,
) -> None:
    """Write a one-time guard event doc the first time the running spend
    crosses each of 50%/80%/100% of `ceiling_usd`, via
    `GuardTotalsStore.record_threshold_event`'s own idempotency check --
    safe to call after every single spend-affecting mutation regardless of
    whether this particular call is the one that actually crossed a new
    threshold."""
    if ceiling_usd <= 0:
        return
    pct = (totals.spend_usd / ceiling_usd) * 100
    for threshold in _THRESHOLD_PCTS:
        if pct >= threshold:
            await totals_store.record_threshold_event(threshold)


__all__ = [
    "DEFAULT_CASE_CREATION_LIMIT",
    "DEFAULT_CASE_CREATION_WINDOW_SECONDS",
    "DEFAULT_DAILY_SPEND_CEILING_USD",
    "DEFAULT_INTERVIEW_TURN_LIMIT",
    "DEFAULT_INTERVIEW_TURN_WINDOW_SECONDS",
    "DEFAULT_MAX_CONCURRENT_TRIBUNALS",
    "DEFAULT_STALE_RUN_TTL_SECONDS",
    "MAX_ANONYMOUS_CASES_TOTAL",
    "MAX_ANONYMOUS_TURNS_TOTAL",
    "MAX_CASES_PER_CLIENT_PER_DAY",
    "MAX_INTERVIEW_TURNS_PER_CASE",
    "MAX_REFUSAL_FEEDBACK_PER_CASE",
    "MAX_UPLOADS_PER_CASE",
    "PRIVILEGED_COOKIE_MAX_AGE_SECONDS",
    "PRIVILEGED_COOKIE_NAME",
    "PUBLIC_SPEND_CEILING_USD",
    "PUBLIC_TURN_COST_ESTIMATE_USD",
    "STREET_VIEW_FETCH_COST_USD",
    "CachedGuardTotalsReader",
    "CaseStoreLike",
    "DailySpendExceeded",
    "PublicGuardPaused",
    "RateLimitExceeded",
    "SlidingWindowRateLimiter",
    "TribunalCapacityExceeded",
    "count_running_tribunals",
    "enforce_concurrent_tribunal_cap",
    "enforce_daily_spend_budget",
    "hashed_client_id",
    "is_privileged_cookie_valid",
    "is_privileged_request",
    "is_public_guard_paused",
    "per_case_feedback_cap_guard",
    "per_case_interview_turn_cap_guard",
    "per_case_interview_turn_guard",
    "per_case_upload_cap_guard",
    "per_client_daily_case_cap_guard",
    "per_ip_case_creation_guard",
    "privileged_cookie_value",
    "public_guard_client_ip",
    "public_guard_dependency",
    "record_threshold_events_if_crossed",
    "todays_ledger_spend_usd",
]
