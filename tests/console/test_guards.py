"""Tests for setback.console.guards: rate limiting, the concurrent-tribunal
cap, and the daily spend breaker.

Fully offline: no live Firestore, no live model calls. The rate limiters
and cap/breaker functions are exercised directly (a fake clock for the
limiters; `InMemoryCaseStore` for the store-backed guards) plus one
FastAPI-integration test per rate limiter to confirm the dependency shape
actually wires into a route the way `console/app.py` will use it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from setback.console.guards import (
    DailySpendExceeded,
    RateLimitExceeded,
    SlidingWindowRateLimiter,
    TribunalCapacityExceeded,
    count_running_tribunals,
    enforce_concurrent_tribunal_cap,
    enforce_daily_spend_budget,
    per_case_interview_turn_guard,
    per_ip_case_creation_guard,
    todays_ledger_spend_usd,
)
from setback.models.client import TokenUsage
from setback.state.firestore import InMemoryCaseStore
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
