"""Tests for setback.state.breakers: per-stage circuit breakers with
degrade-not-halt semantics."""

from __future__ import annotations

from setback.state.breakers import CircuitBreaker, CircuitState, DegradingBreaker


def _make_clock(start: float = 0.0) -> tuple[list[float], object]:
    time_box = [start]

    def clock() -> float:
        return time_box[0]

    return time_box, clock


# --- CircuitBreaker -----------------------------------------------------------


def test_breaker_starts_closed() -> None:
    breaker = CircuitBreaker(name="bench")

    assert breaker.state is CircuitState.CLOSED
    assert not breaker.is_open


def test_breaker_stays_closed_below_failure_threshold() -> None:
    breaker = CircuitBreaker(name="bench", failure_threshold=3)

    breaker.record_failure()
    breaker.record_failure()

    assert breaker.state is CircuitState.CLOSED


def test_breaker_opens_at_failure_threshold() -> None:
    breaker = CircuitBreaker(name="bench", failure_threshold=3)

    breaker.record_failure()
    breaker.record_failure()
    breaker.record_failure()

    assert breaker.state is CircuitState.OPEN
    assert breaker.is_open


def test_breaker_success_resets_failure_count() -> None:
    breaker = CircuitBreaker(name="bench", failure_threshold=3)

    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()

    assert breaker.state is CircuitState.CLOSED


def test_breaker_transitions_to_half_open_after_reset_timeout() -> None:
    _time, clock = _make_clock()
    breaker = CircuitBreaker(
        name="bench", failure_threshold=1, reset_timeout_seconds=30, clock=clock
    )

    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN

    _time[0] += 30
    assert breaker.state is CircuitState.HALF_OPEN
    assert not breaker.is_open  # half-open allows a probe through


def test_breaker_stays_open_before_reset_timeout_elapses() -> None:
    _time, clock = _make_clock()
    breaker = CircuitBreaker(
        name="bench", failure_threshold=1, reset_timeout_seconds=30, clock=clock
    )

    breaker.record_failure()
    _time[0] += 29

    assert breaker.state is CircuitState.OPEN


def test_half_open_probe_success_closes_breaker() -> None:
    _time, clock = _make_clock()
    breaker = CircuitBreaker(
        name="bench", failure_threshold=1, reset_timeout_seconds=30, clock=clock
    )

    breaker.record_failure()
    _time[0] += 30
    breaker.record_success()

    assert breaker.state is CircuitState.CLOSED


def test_half_open_probe_failure_reopens_and_restarts_cooldown() -> None:
    _time, clock = _make_clock()
    breaker = CircuitBreaker(
        name="bench", failure_threshold=1, reset_timeout_seconds=30, clock=clock
    )

    breaker.record_failure()
    _time[0] += 30
    assert breaker.state is CircuitState.HALF_OPEN

    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN

    _time[0] += 29
    assert breaker.state is CircuitState.OPEN  # cooldown restarted, not elapsed yet


# --- DegradingBreaker -----------------------------------------------------------


def test_degrading_breaker_returns_primary_while_closed() -> None:
    degrading = DegradingBreaker(
        breaker=CircuitBreaker(name="bench", failure_threshold=3),
        primary="gemini-3.7-flash",
        fallback="gemini-3.5-flash-lite",
    )

    assert degrading.current() == "gemini-3.7-flash"


def test_degrading_breaker_returns_fallback_once_open() -> None:
    breaker = CircuitBreaker(name="bench", failure_threshold=1)
    degrading = DegradingBreaker(
        breaker=breaker, primary="gemini-3.7-flash", fallback="gemini-3.5-flash-lite"
    )

    degrading.record_failure()

    assert degrading.current() == "gemini-3.5-flash-lite"


def test_degrading_breaker_returns_primary_during_half_open_probe() -> None:
    _time, clock = _make_clock()
    breaker = CircuitBreaker(
        name="bench", failure_threshold=1, reset_timeout_seconds=10, clock=clock
    )
    degrading = DegradingBreaker(
        breaker=breaker, primary="gemini-3.7-flash", fallback="gemini-3.5-flash-lite"
    )

    degrading.record_failure()
    assert degrading.current() == "gemini-3.5-flash-lite"

    _time[0] += 10
    assert degrading.current() == "gemini-3.7-flash"


def test_degrading_breaker_record_success_closes_and_returns_primary() -> None:
    breaker = CircuitBreaker(name="bench", failure_threshold=1)
    degrading = DegradingBreaker(
        breaker=breaker, primary="gemini-3.7-flash", fallback="gemini-3.5-flash-lite"
    )

    degrading.record_failure()
    degrading.record_success()

    assert degrading.current() == "gemini-3.7-flash"
    assert breaker.state is CircuitState.CLOSED
