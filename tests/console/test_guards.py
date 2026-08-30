"""Tests for setback.console.guards: rate limiting, the concurrent-tribunal
cap, and the daily spend breaker.

Fully offline: no live Firestore, no live model calls. The rate limiters
and cap/breaker functions are exercised directly (a fake clock for the
limiters; `InMemoryCaseStore` for the store-backed guards) plus one
FastAPI-integration test per rate limiter to confirm the dependency shape
actually wires into a route the way `console/app.py` will use it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from setback.console.guards import (
    DEFAULT_STALE_RUN_TTL_SECONDS,
    MAX_INTERVIEW_TURNS_PER_CASE,
    MAX_UPLOADS_PER_CASE,
    CachedGuardTotalsReader,
    DailySpendExceeded,
    PublicGuardPaused,
    RateLimitExceeded,
    SlidingWindowRateLimiter,
    TribunalCapacityExceeded,
    count_running_tribunals,
    enforce_concurrent_tribunal_cap,
    enforce_daily_spend_budget,
    hashed_client_id,
    is_privileged_cookie_valid,
    is_privileged_request,
    is_public_guard_paused,
    per_case_interview_turn_cap_guard,
    per_case_interview_turn_guard,
    per_case_upload_cap_guard,
    per_client_daily_case_cap_guard,
    per_ip_case_creation_guard,
    privileged_cookie_value,
    public_guard_client_ip,
    public_guard_dependency,
    record_threshold_events_if_crossed,
    todays_ledger_spend_usd,
)
from setback.models.client import TokenUsage
from setback.state.firestore import InMemoryCaseStore
from setback.state.guard_store import (
    GuardTotals,
    InMemoryGuardCounterStore,
    InMemoryGuardTotalsStore,
)
from setback.state.ledger import Ledger


class _FakeClock:
    """A controllable monotonic clock: `advance` moves time forward
    without a real sleep, so window-expiry tests run instantly."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _fake_request(host: str | None) -> SimpleNamespace:
    client = SimpleNamespace(host=host) if host is not None else None
    return SimpleNamespace(client=client)


# --- SlidingWindowRateLimiter -------------------------------------------------


def test_allows_up_to_the_limit_within_the_window() -> None:
    clock = _FakeClock()
    limiter = SlidingWindowRateLimiter(limit=3, window_seconds=60.0, clock=clock)
    assert limiter.allow("k") is True
    assert limiter.allow("k") is True
    assert limiter.allow("k") is True
    assert limiter.allow("k") is False


def test_refused_attempts_are_not_themselves_counted_as_hits() -> None:
    clock = _FakeClock()
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60.0, clock=clock)
    assert limiter.allow("k") is True
    assert limiter.allow("k") is False
    assert limiter.allow("k") is False
    assert limiter.hit_count("k") == 1


def test_window_expiry_allows_new_hits_after_the_window_elapses() -> None:
    clock = _FakeClock()
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60.0, clock=clock)
    assert limiter.allow("k") is True
    assert limiter.allow("k") is False
    clock.advance(61.0)
    assert limiter.allow("k") is True


def test_different_keys_are_tracked_independently() -> None:
    clock = _FakeClock()
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60.0, clock=clock)
    assert limiter.allow("a") is True
    assert limiter.allow("b") is True
    assert limiter.allow("a") is False
    assert limiter.allow("b") is False


# --- per_ip_case_creation_guard ------------------------------------------------


def test_case_creation_guard_allows_then_blocks_the_same_ip() -> None:
    clock = _FakeClock()
    limiter = SlidingWindowRateLimiter(limit=2, window_seconds=3600.0, clock=clock)
    guard = per_ip_case_creation_guard(limiter)
    request = _fake_request("1.2.3.4")

    guard(request)
    guard(request)
    with pytest.raises(RateLimitExceeded) as exc_info:
        guard(request)
    assert exc_info.value.status_code == 429
    assert "Retry-After" in exc_info.value.headers


def test_case_creation_guard_tracks_distinct_ips_independently() -> None:
    clock = _FakeClock()
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=3600.0, clock=clock)
    guard = per_ip_case_creation_guard(limiter)

    guard(_fake_request("1.1.1.1"))
    guard(_fake_request("2.2.2.2"))  # a different IP: not blocked by the first's limit
    with pytest.raises(RateLimitExceeded):
        guard(_fake_request("1.1.1.1"))


def test_case_creation_guard_without_a_docket_key_provider_ignores_privilege() -> None:
    """Backward-compatible default: no `docket_key_provider` (this guard's
    own pre-existing call shape) never even looks at `.cookies` -- the fake
    `SimpleNamespace(client=...)` request used throughout this section has
    none, and must keep working exactly as before."""
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=3600.0)
    guard = per_ip_case_creation_guard(limiter)
    request = _fake_request("1.2.3.4")
    guard(request)
    with pytest.raises(RateLimitExceeded):
        guard(request)


async def test_case_creation_guard_privileged_session_bypasses_it() -> None:
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=3600.0)
    guard = per_ip_case_creation_guard(limiter, docket_key_provider=lambda: "secret-key")
    cookie = privileged_cookie_value("secret-key")
    request = _fake_public_request(host="1.2.3.4", cookies={"sb_priv": cookie})
    guard(request)
    guard(request)
    guard(request)  # must never raise for a privileged session


def test_case_creation_guard_falls_back_to_unknown_bucket_with_no_client() -> None:
    clock = _FakeClock()
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=3600.0, clock=clock)
    guard = per_ip_case_creation_guard(limiter)

    guard(_fake_request(None))
    with pytest.raises(RateLimitExceeded):
        guard(_fake_request(None))


def test_case_creation_guard_wires_into_a_real_fastapi_route() -> None:
    limiter = SlidingWindowRateLimiter(limit=2, window_seconds=3600.0)
    guard = per_ip_case_creation_guard(limiter)
    app = FastAPI()

    @app.post("/api/cases", dependencies=[Depends(guard)])
    def create_case() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)
    assert client.post("/api/cases").status_code == 200
    assert client.post("/api/cases").status_code == 200
    third = client.post("/api/cases")
    assert third.status_code == 429
    assert "Retry-After" in third.headers


# --- per_case_interview_turn_guard ---------------------------------------------


def test_interview_turn_guard_allows_then_blocks_the_same_case() -> None:
    clock = _FakeClock()
    limiter = SlidingWindowRateLimiter(limit=2, window_seconds=86400.0, clock=clock)
    guard = per_case_interview_turn_guard(limiter)

    guard("case-1")
    guard("case-1")
    with pytest.raises(RateLimitExceeded):
        guard("case-1")


def test_interview_turn_guard_tracks_distinct_cases_independently() -> None:
    clock = _FakeClock()
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=86400.0, clock=clock)
    guard = per_case_interview_turn_guard(limiter)

    guard("case-1")
    guard("case-2")
    with pytest.raises(RateLimitExceeded):
        guard("case-1")


def test_interview_turn_guard_wires_into_a_real_fastapi_route_with_path_param() -> None:
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=86400.0)
    guard = per_case_interview_turn_guard(limiter)
    app = FastAPI()

    @app.post("/api/cases/{case_id}/interview", dependencies=[Depends(guard)])
    def answer(case_id: str) -> dict[str, str]:
        return {"case_id": case_id}

    client = TestClient(app)
    assert client.post("/api/cases/case-1/interview").status_code == 200
    # Same case_id blocked, but a different case_id's own window is untouched.
    assert client.post("/api/cases/case-1/interview").status_code == 429
    assert client.post("/api/cases/case-2/interview").status_code == 200


# --- concurrent tribunal cap ---------------------------------------------------


async def _make_case_with_events(store: InMemoryCaseStore, case_id_seed: str) -> str:
    case = await store.create_case(application_number=case_id_seed, resident_session="s")
    return case.case_id


async def test_count_running_tribunals_is_zero_with_no_requests() -> None:
    store = InMemoryCaseStore()
    await _make_case_with_events(store, "PAN-1")
    assert await count_running_tribunals(store) == 0


async def test_count_running_tribunals_counts_a_started_but_unfinished_run() -> None:
    store = InMemoryCaseStore()
    case_id = await _make_case_with_events(store, "PAN-1")
    await store.append_event(case_id, "tribunal-req", "tribunal_requested", payload={})
    assert await count_running_tribunals(store) == 1


async def test_count_running_tribunals_excludes_a_completed_run() -> None:
    store = InMemoryCaseStore()
    case_id = await _make_case_with_events(store, "PAN-1")
    await store.append_event(case_id, "tribunal-req", "tribunal_requested", payload={})
    await store.append_event(case_id, "submitted", "submission_composed", payload={})
    assert await count_running_tribunals(store) == 0


async def test_count_running_tribunals_excludes_a_failed_run() -> None:
    store = InMemoryCaseStore()
    case_id = await _make_case_with_events(store, "PAN-1")
    await store.append_event(case_id, "tribunal-req", "tribunal_requested", payload={})
    await store.append_event(case_id, "failed", "job_failed", payload={})
    assert await count_running_tribunals(store) == 0


async def test_count_running_tribunals_counts_a_re_requested_run_after_completion() -> None:
    """A case can be re-triggered after finishing -- a fresh
    `tribunal_requested` with a higher sequence than the prior terminal
    event must count as running again."""
    store = InMemoryCaseStore()
    case_id = await _make_case_with_events(store, "PAN-1")
    await store.append_event(case_id, "tribunal-req-1", "tribunal_requested", payload={})
    await store.append_event(case_id, "submitted", "submission_composed", payload={})
    await store.append_event(case_id, "tribunal-req-2", "tribunal_requested", payload={})
    assert await count_running_tribunals(store) == 1


async def test_count_running_tribunals_excludes_an_idempotent_rerun_replay() -> None:
    """Live-incident regression (2026-08-29 film-day capacity block): a
    second "Start tribunal" press on an already-decided case is caught by
    `job.pipeline.RealPipelineRunner.run`'s idempotency guard, which
    records `tribunal_rerun_ignored` and does nothing else -- no new
    `submission_composed`/`job_failed` is ever written, because nothing
    actually ran. The route still records the new `tribunal_requested`
    unconditionally (see `test_a_second_tribunal_start_...` in
    `test_app.py`), so without this fix that request's higher sequence
    number outlives the case's one and only terminal event and the case
    counts as "running" forever, permanently burning one of only
    `DEFAULT_MAX_CONCURRENT_TRIBUNALS` (2) slots. This is exactly what
    happened live to both canonical demo cases at once, exhausting the cap
    for every other case in the docket."""
    store = InMemoryCaseStore()
    case_id = await _make_case_with_events(store, "PAN-1")
    await store.append_event(case_id, "tribunal-req-1", "tribunal_requested", payload={})
    await store.append_event(case_id, "submitted", "submission_composed", payload={})
    await store.append_event(case_id, "tribunal-req-2", "tribunal_requested", payload={})
    await store.append_event(case_id, "rerun-ignored", "tribunal_rerun_ignored", payload={})
    assert await count_running_tribunals(store) == 0


async def test_count_running_tribunals_excludes_a_run_past_the_stale_ttl() -> None:
    """DESIGN-DECISIONS.md/ARCHITECTURE.md both say plainly there is no
    sweeper: a crashed/OOM-killed/timed-out job execution leaves its case
    stuck with no automated recovery. Without this TTL that stuck
    `tribunal_requested` (no terminal event ever follows -- nothing crashed
    *recorded* anything) would count as "running" forever, permanently
    burning a concurrency slot; a second crash would burn the other one and
    wedge every future case behind `DEFAULT_MAX_CONCURRENT_TRIBUNALS` (2)
    stuck slots that can never clear themselves."""
    requested_at = datetime(2026, 8, 29, 7, 0, tzinfo=UTC)
    store = InMemoryCaseStore(clock=lambda: requested_at)
    case_id = await _make_case_with_events(store, "PAN-1")
    await store.append_event(case_id, "tribunal-req", "tribunal_requested", payload={})

    past_ttl = requested_at + timedelta(seconds=DEFAULT_STALE_RUN_TTL_SECONDS, microseconds=1)
    assert await count_running_tribunals(store, now=past_ttl) == 0


async def test_count_running_tribunals_counts_a_run_still_within_the_stale_ttl() -> None:
    requested_at = datetime(2026, 8, 29, 7, 0, tzinfo=UTC)
    store = InMemoryCaseStore(clock=lambda: requested_at)
    case_id = await _make_case_with_events(store, "PAN-1")
    await store.append_event(case_id, "tribunal-req", "tribunal_requested", payload={})

    within_ttl = requested_at + timedelta(seconds=DEFAULT_STALE_RUN_TTL_SECONDS - 1)
    assert await count_running_tribunals(store, now=within_ttl) == 1


async def test_count_running_tribunals_stale_ttl_is_keyed_to_the_latest_start_event() -> None:
    """A re-requested run (see the "re-requested after completion" case
    above) must be judged by its own, most recent `tribunal_requested`
    timestamp -- not by the case's first one, which would make an entirely
    fresh run look stale just because the case is old."""
    first_requested_at = datetime(2026, 8, 29, 6, 0, tzinfo=UTC)
    second_requested_at = first_requested_at + timedelta(seconds=DEFAULT_STALE_RUN_TTL_SECONDS + 60)
    current = {"value": first_requested_at}
    store = InMemoryCaseStore(clock=lambda: current["value"])
    case_id = await _make_case_with_events(store, "PAN-1")
    await store.append_event(case_id, "tribunal-req-1", "tribunal_requested", payload={})
    await store.append_event(case_id, "submitted", "submission_composed", payload={})

    current["value"] = second_requested_at
    await store.append_event(case_id, "tribunal-req-2", "tribunal_requested", payload={})

    just_after_second_request = second_requested_at + timedelta(seconds=1)
    assert await count_running_tribunals(store, now=just_after_second_request) == 1


async def test_count_running_tribunals_does_not_resurrect_a_completed_run_past_the_ttl() -> None:
    """The TTL only ever *excludes* a would-be-running case; it must never
    flip an already-terminal run back to "running" just because a lot of
    time has passed."""
    requested_at = datetime(2026, 8, 29, 7, 0, tzinfo=UTC)
    store = InMemoryCaseStore(clock=lambda: requested_at)
    case_id = await _make_case_with_events(store, "PAN-1")
    await store.append_event(case_id, "tribunal-req", "tribunal_requested", payload={})
    await store.append_event(case_id, "submitted", "submission_composed", payload={})

    long_after = requested_at + timedelta(seconds=DEFAULT_STALE_RUN_TTL_SECONDS * 10)
    assert await count_running_tribunals(store, now=long_after) == 0


async def test_enforce_concurrent_tribunal_cap_raises_once_the_cap_is_reached() -> None:
    store = InMemoryCaseStore()
    for i in range(2):
        case_id = await _make_case_with_events(store, f"PAN-{i}")
        await store.append_event(case_id, f"req-{i}", "tribunal_requested", payload={})

    with pytest.raises(TribunalCapacityExceeded) as exc_info:
        await enforce_concurrent_tribunal_cap(store, max_concurrent=2)
    assert exc_info.value.status_code == 429


async def test_enforce_concurrent_tribunal_cap_allows_below_the_cap() -> None:
    store = InMemoryCaseStore()
    case_id = await _make_case_with_events(store, "PAN-0")
    await store.append_event(case_id, "req-0", "tribunal_requested", payload={})

    await enforce_concurrent_tribunal_cap(store, max_concurrent=2)  # must not raise


# --- daily spend breaker -------------------------------------------------------


def _booked_ledger(*, cost_calls: int = 1) -> Ledger:
    ledger = Ledger(ceiling_usd=1000.0)
    for _ in range(cost_calls):
        ledger.record(
            stage="test",
            model="gemini-3.5-flash-lite",
            usage=TokenUsage(prompt_tokens=1_000, output_tokens=1_000),
        )
    return ledger


async def test_todays_ledger_spend_sums_only_cases_created_today() -> None:
    today = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
    yesterday = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)
    # A mutable "current instant" rather than a one-shot iterator: `create_case`
    # calls the clock more than once internally (once for `created_at`, again
    # for its own `case_created` event), and this only needs each case's
    # *own* creation to land consistently on one side of midnight.
    current = {"value": today}
    store = InMemoryCaseStore(clock=lambda: current["value"])

    today_case = await store.create_case(application_number="PAN-today", resident_session="s")
    current["value"] = yesterday
    yesterday_case = await store.create_case(
        application_number="PAN-yesterday", resident_session="s"
    )

    today_ledger = _booked_ledger(cost_calls=1)
    yesterday_ledger = _booked_ledger(cost_calls=5)
    await store.save_ledger(today_case.case_id, today_ledger)
    await store.save_ledger(yesterday_case.case_id, yesterday_ledger)

    spent = await todays_ledger_spend_usd(store, today=today.date())
    assert spent == pytest.approx(today_ledger.total_cost_usd)
    assert spent < yesterday_ledger.total_cost_usd


async def test_todays_ledger_spend_is_zero_with_no_ledgers() -> None:
    store = InMemoryCaseStore()
    await store.create_case(application_number="PAN-1", resident_session="s")
    assert await todays_ledger_spend_usd(store) == 0.0


async def test_enforce_daily_spend_budget_raises_once_ceiling_is_reached() -> None:
    store = InMemoryCaseStore()
    case = await store.create_case(application_number="PAN-1", resident_session="s")
    ledger = _booked_ledger(cost_calls=1)
    await store.save_ledger(case.case_id, ledger)

    with pytest.raises(DailySpendExceeded) as exc_info:
        await enforce_daily_spend_budget(store, daily_ceiling_usd=ledger.total_cost_usd)
    assert exc_info.value.status_code == 429


async def test_enforce_daily_spend_budget_allows_spend_under_the_ceiling() -> None:
    store = InMemoryCaseStore()
    case = await store.create_case(application_number="PAN-1", resident_session="s")
    ledger = _booked_ledger(cost_calls=1)
    await store.save_ledger(case.case_id, ledger)

    await enforce_daily_spend_budget(
        store, daily_ceiling_usd=ledger.total_cost_usd + 1.0
    )  # must not raise


# --- public-abuse guard: privileged session cookie --------------------------


class _FakeHeaders(dict):
    """A tiny case-insensitive `.get` stand-in for Starlette's `Headers`."""

    def get(self, key: str, default: str | None = None) -> str | None:
        return super().get(key.lower(), default)


def _fake_public_request(
    *,
    host: str | None = "1.2.3.4",
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> SimpleNamespace:
    client = SimpleNamespace(host=host) if host is not None else None
    return SimpleNamespace(
        client=client,
        headers=_FakeHeaders({k.lower(): v for k, v in (headers or {}).items()}),
        cookies=cookies or {},
    )


def test_privileged_cookie_value_is_deterministic_for_the_same_key() -> None:
    assert privileged_cookie_value("secret-key") == privileged_cookie_value("secret-key")


def test_privileged_cookie_value_differs_for_different_keys() -> None:
    assert privileged_cookie_value("secret-key") != privileged_cookie_value("other-key")


def test_privileged_cookie_value_is_not_the_key_itself() -> None:
    assert privileged_cookie_value("secret-key") != "secret-key"


def test_is_privileged_cookie_valid_accepts_the_matching_value() -> None:
    value = privileged_cookie_value("secret-key")
    assert is_privileged_cookie_valid(value, "secret-key") is True


def test_is_privileged_cookie_valid_rejects_a_tampered_value() -> None:
    assert is_privileged_cookie_valid("0" * 64, "secret-key") is False


def test_is_privileged_cookie_valid_rejects_when_no_docket_key_is_configured() -> None:
    value = privileged_cookie_value("secret-key")
    assert is_privileged_cookie_valid(value, None) is False


def test_is_privileged_cookie_valid_rejects_a_missing_cookie() -> None:
    assert is_privileged_cookie_valid(None, "secret-key") is False


def test_rotating_the_docket_key_invalidates_a_previously_issued_cookie() -> None:
    """DESIGN SPEC point 1: rotating `SETBACK_DOCKET_KEY` must invalidate
    every cookie issued under the old key -- the new key derives a
    different HMAC, so an old cookie value no longer verifies."""
    old_cookie = privileged_cookie_value("old-key")
    assert is_privileged_cookie_valid(old_cookie, "new-key") is False


def test_is_privileged_request_true_with_a_valid_cookie() -> None:
    cookie = privileged_cookie_value("secret-key")
    request = _fake_public_request(cookies={"sb_priv": cookie})
    assert is_privileged_request(request, docket_key_provider=lambda: "secret-key") is True


def test_is_privileged_request_false_with_a_tampered_cookie() -> None:
    request = _fake_public_request(cookies={"sb_priv": "0" * 64})
    assert is_privileged_request(request, docket_key_provider=lambda: "secret-key") is False


def test_is_privileged_request_false_with_no_cookie() -> None:
    request = _fake_public_request(cookies={})
    assert is_privileged_request(request, docket_key_provider=lambda: "secret-key") is False


# --- public-abuse guard: X-Forwarded-For parsing -----------------------------


def test_public_guard_client_ip_uses_the_last_x_forwarded_for_entry() -> None:
    request = _fake_public_request(host="10.0.0.1", headers={"X-Forwarded-For": "1.2.3.4, 5.6.7.8"})
    assert public_guard_client_ip(request) == "5.6.7.8"


def test_public_guard_client_ip_ignores_a_forged_first_entry() -> None:
    """A client can send any `X-Forwarded-For` value it wants; only Google
    Front End's own appended (last) entry is trustworthy."""
    forged = _fake_public_request(host="10.0.0.1", headers={"X-Forwarded-For": "9.9.9.9, 5.6.7.8"})
    genuine = _fake_public_request(host="10.0.0.1", headers={"X-Forwarded-For": "1.1.1.1, 5.6.7.8"})
    assert public_guard_client_ip(forged) == public_guard_client_ip(genuine) == "5.6.7.8"


def test_public_guard_client_ip_falls_back_to_request_client_host() -> None:
    request = _fake_public_request(host="10.0.0.1", headers={})
    assert public_guard_client_ip(request) == "10.0.0.1"


def test_public_guard_client_ip_falls_back_to_unknown_with_nothing_available() -> None:
    request = _fake_public_request(host=None, headers={})
    assert public_guard_client_ip(request) == "unknown"


# --- public-abuse guard: salted client hashing -------------------------------


def test_hashed_client_id_never_contains_the_raw_ip() -> None:
    hashed = hashed_client_id("203.0.113.7", docket_key="secret-key")
    assert "203.0.113.7" not in hashed
    assert "203" not in hashed.split(".")  # no accidental dotted-quad leakage


def test_hashed_client_id_is_deterministic_for_the_same_inputs() -> None:
    first = hashed_client_id("203.0.113.7", docket_key="secret-key")
    second = hashed_client_id("203.0.113.7", docket_key="secret-key")
    assert first == second


def test_hashed_client_id_differs_for_different_ips() -> None:
    a = hashed_client_id("203.0.113.7", docket_key="secret-key")
    b = hashed_client_id("203.0.113.8", docket_key="secret-key")
    assert a != b


def test_hashed_client_id_differs_when_the_docket_key_differs() -> None:
    """The salt is derived from the docket key, so the same IP hashes
    differently under a different deployment key."""
    a = hashed_client_id("203.0.113.7", docket_key="key-one")
    b = hashed_client_id("203.0.113.7", docket_key="key-two")
    assert a != b


# --- public-abuse guard: per-client daily case-creation cap dependency ------


async def test_per_client_daily_case_cap_guard_allows_then_blocks() -> None:
    counter_store = InMemoryGuardCounterStore()
    guard = per_client_daily_case_cap_guard(
        counter_store, docket_key_provider=lambda: "secret-key", limit=2
    )
    request = _fake_public_request(host="1.2.3.4")
    await guard(request)
    await guard(request)
    with pytest.raises(RateLimitExceeded) as exc_info:
        await guard(request)
    assert exc_info.value.status_code == 429


async def test_per_client_daily_case_cap_guard_bypassed_when_privileged() -> None:
    counter_store = InMemoryGuardCounterStore()
    guard = per_client_daily_case_cap_guard(
        counter_store, docket_key_provider=lambda: "secret-key", limit=1
    )
    cookie = privileged_cookie_value("secret-key")
    request = _fake_public_request(host="1.2.3.4", cookies={"sb_priv": cookie})
    await guard(request)
    await guard(request)
    await guard(request)  # must never raise for a privileged session


# --- public-abuse guard: per-case interview-turn cap dependency -------------


async def test_per_case_interview_turn_cap_guard_blocks_past_the_limit() -> None:
    store = InMemoryCaseStore()
    case = await store.create_case(application_number="PAN-1", resident_session="s")
    for i in range(MAX_INTERVIEW_TURNS_PER_CASE):
        await store.append_event(
            case.case_id, f"turn-{i}", "interview_turn", payload={"role": "resident"}
        )
    guard = per_case_interview_turn_cap_guard(store, docket_key_provider=lambda: None)
    request = _fake_public_request()
    with pytest.raises(RateLimitExceeded):
        await guard(request, case.case_id)


async def test_per_case_interview_turn_cap_guard_allows_under_the_limit() -> None:
    store = InMemoryCaseStore()
    case = await store.create_case(application_number="PAN-1", resident_session="s")
    guard = per_case_interview_turn_cap_guard(store, docket_key_provider=lambda: None)
    request = _fake_public_request()
    await guard(request, case.case_id)  # must not raise


async def test_per_case_interview_turn_cap_guard_bypassed_when_privileged() -> None:
    store = InMemoryCaseStore()
    case = await store.create_case(application_number="PAN-1", resident_session="s")
    for i in range(MAX_INTERVIEW_TURNS_PER_CASE + 5):
        await store.append_event(
            case.case_id, f"turn-{i}", "interview_turn", payload={"role": "resident"}
        )
    guard = per_case_interview_turn_cap_guard(store, docket_key_provider=lambda: "secret-key")
    cookie = privileged_cookie_value("secret-key")
    request = _fake_public_request(cookies={"sb_priv": cookie})
    await guard(request, case.case_id)  # must not raise


# --- public-abuse guard: per-case upload cap dependency ---------------------


async def test_per_case_upload_cap_guard_blocks_past_the_limit() -> None:
    store = InMemoryCaseStore()
    case = await store.create_case(application_number="PAN-1", resident_session="s")
    for i in range(MAX_UPLOADS_PER_CASE):
        await store.append_event(
            case.case_id, f"doc-{i}", "document_uploaded", payload={"document_id": f"doc-{i}"}
        )
    guard = per_case_upload_cap_guard(store, docket_key_provider=lambda: None)
    request = _fake_public_request()
    with pytest.raises(RateLimitExceeded):
        await guard(request, case.case_id)


async def test_per_case_upload_cap_guard_allows_under_the_limit() -> None:
    store = InMemoryCaseStore()
    case = await store.create_case(application_number="PAN-1", resident_session="s")
    guard = per_case_upload_cap_guard(store, docket_key_provider=lambda: None)
    request = _fake_public_request()
    await guard(request, case.case_id)  # must not raise


# --- public-abuse guard: global ceiling / backstops --------------------------


def test_is_public_guard_paused_false_when_under_every_limit() -> None:
    totals = GuardTotals(spend_usd=1.0, anonymous_cases=1, anonymous_turns=1)
    paused = is_public_guard_paused(totals, ceiling_usd=26.0, max_cases=5000, max_turns=100000)
    assert paused is False


def test_is_public_guard_paused_true_once_the_dollar_ceiling_is_reached() -> None:
    totals = GuardTotals(spend_usd=26.0, anonymous_cases=1, anonymous_turns=1)
    paused = is_public_guard_paused(totals, ceiling_usd=26.0, max_cases=5000, max_turns=100000)
    assert paused is True


def test_is_public_guard_paused_true_once_the_case_backstop_is_reached() -> None:
    totals = GuardTotals(spend_usd=0.0, anonymous_cases=5000, anonymous_turns=0)
    paused = is_public_guard_paused(totals, ceiling_usd=26.0, max_cases=5000, max_turns=100000)
    assert paused is True


def test_is_public_guard_paused_true_once_the_turn_backstop_is_reached() -> None:
    totals = GuardTotals(spend_usd=0.0, anonymous_cases=0, anonymous_turns=100000)
    paused = is_public_guard_paused(totals, ceiling_usd=26.0, max_cases=5000, max_turns=100000)
    assert paused is True


async def test_public_guard_dependency_open_state_allows_through() -> None:
    totals_store = InMemoryGuardTotalsStore()
    reader = CachedGuardTotalsReader(totals_store)
    guard = public_guard_dependency(reader, docket_key_provider=lambda: None, ceiling_usd=26.0)
    await guard(_fake_public_request())  # must not raise


async def test_public_guard_dependency_paused_state_raises() -> None:
    totals_store = InMemoryGuardTotalsStore()
    await totals_store.add_spend(100.0)
    reader = CachedGuardTotalsReader(totals_store)
    guard = public_guard_dependency(reader, docket_key_provider=lambda: None, ceiling_usd=26.0)
    with pytest.raises(PublicGuardPaused) as exc_info:
        await guard(_fake_public_request())
    assert exc_info.value.status_code == 429


async def test_public_guard_dependency_privileged_bypasses_paused_state() -> None:
    totals_store = InMemoryGuardTotalsStore()
    await totals_store.add_spend(100.0)
    reader = CachedGuardTotalsReader(totals_store)
    guard = public_guard_dependency(
        reader, docket_key_provider=lambda: "secret-key", ceiling_usd=26.0
    )
    cookie = privileged_cookie_value("secret-key")
    request = _fake_public_request(cookies={"sb_priv": cookie})
    await guard(request)  # must not raise


async def test_cached_guard_totals_reader_serves_a_stale_value_within_the_ttl() -> None:
    totals_store = InMemoryGuardTotalsStore()
    clock = _FakeClock()
    reader = CachedGuardTotalsReader(totals_store, ttl_seconds=60.0, clock=clock)
    first = await reader.get_totals()
    await totals_store.add_spend(5.0)
    still_cached = await reader.get_totals()
    assert still_cached == first  # the store already changed, cache has not

    clock.advance(61.0)
    refreshed = await reader.get_totals()
    assert refreshed.spend_usd == pytest.approx(5.0)


async def test_cached_guard_totals_reader_invalidate_forces_a_refresh() -> None:
    totals_store = InMemoryGuardTotalsStore()
    reader = CachedGuardTotalsReader(totals_store, ttl_seconds=60.0)
    await reader.get_totals()
    await totals_store.add_spend(5.0)
    reader.invalidate()
    refreshed = await reader.get_totals()
    assert refreshed.spend_usd == pytest.approx(5.0)


# --- public-abuse guard: threshold-event idempotence -------------------------


async def test_record_threshold_events_if_crossed_writes_each_crossed_threshold_once() -> None:
    totals_store = InMemoryGuardTotalsStore()
    totals = GuardTotals(spend_usd=15.0, anonymous_cases=0, anonymous_turns=0)  # 50%+ of 26
    await record_threshold_events_if_crossed(totals_store, totals, ceiling_usd=26.0)
    assert await totals_store.record_threshold_event(50) is False  # already recorded
    assert await totals_store.record_threshold_event(80) is True  # not yet crossed


async def test_record_threshold_events_if_crossed_is_idempotent_across_repeated_calls() -> None:
    totals_store = InMemoryGuardTotalsStore()
    totals = GuardTotals(spend_usd=26.0, anonymous_cases=0, anonymous_turns=0)  # 100%
    await record_threshold_events_if_crossed(totals_store, totals, ceiling_usd=26.0)
    await record_threshold_events_if_crossed(totals_store, totals, ceiling_usd=26.0)
    await record_threshold_events_if_crossed(totals_store, totals, ceiling_usd=26.0)
    # Every threshold was crossed by 100% spend, but each is recorded once.
    for threshold in (50, 80, 100):
        assert await totals_store.record_threshold_event(threshold) is False
