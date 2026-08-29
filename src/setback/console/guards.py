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

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Final, Protocol

from fastapi import HTTPException, Request

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
) -> Callable[[Request], None]:
    """Build a FastAPI dependency enforcing a per-IP sliding-window limit on
    case creation. Wire with `Depends(...)` on `POST /api/cases`."""
    active_limiter = limiter or SlidingWindowRateLimiter(
        limit=DEFAULT_CASE_CREATION_LIMIT,
        window_seconds=DEFAULT_CASE_CREATION_WINDOW_SECONDS,
    )

    def _dependency(request: Request) -> None:
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
_TRIBUNAL_TERMINAL_EVENTS: Final[frozenset[str]] = frozenset({"submission_composed", "job_failed"})
"""Events `job/pipeline.py`/`job/main.py` record that end a tribunal run,
successfully or not (see their module docstrings) -- a case counts as
"still running" when it has a start event with no later terminal event."""


async def count_running_tribunals(store: CaseStoreLike, *, case_limit: int = 200) -> int:
    """Count cases with a `tribunal_requested` event newer (higher
    `sequence`) than their latest terminal event, or with no terminal event
    at all.

    `case_limit` bounds how many of the most recently created cases
    (`CaseStore.list_cases` is already sorted newest-first) are checked --
    a documented simplification appropriate to this single-demo-case
    hackathon build's data volume, not a claim of exhaustive correctness at
    an arbitrary scale.
    """
    cases = await store.list_cases(limit=case_limit)
    running = 0
    for case in cases:
        events = await store.list_events(case.case_id)
        start_sequence = max(
            (e.sequence for e in events if e.event_type == _TRIBUNAL_START_EVENT),
            default=None,
        )
        if start_sequence is None:
            continue
        terminal_sequence = max(
            (e.sequence for e in events if e.event_type in _TRIBUNAL_TERMINAL_EVENTS),
            default=None,
        )
        if terminal_sequence is None or terminal_sequence < start_sequence:
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


__all__ = [
    "DEFAULT_CASE_CREATION_LIMIT",
    "DEFAULT_CASE_CREATION_WINDOW_SECONDS",
    "DEFAULT_DAILY_SPEND_CEILING_USD",
    "DEFAULT_INTERVIEW_TURN_LIMIT",
    "DEFAULT_INTERVIEW_TURN_WINDOW_SECONDS",
    "DEFAULT_MAX_CONCURRENT_TRIBUNALS",
    "CaseStoreLike",
    "DailySpendExceeded",
    "RateLimitExceeded",
    "SlidingWindowRateLimiter",
    "TribunalCapacityExceeded",
    "count_running_tribunals",
    "enforce_concurrent_tribunal_cap",
    "enforce_daily_spend_budget",
    "per_case_interview_turn_guard",
    "per_ip_case_creation_guard",
    "todays_ledger_spend_usd",
]
