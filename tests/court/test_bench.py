"""Tests for setback.court.bench: the adjudicator's degrade-not-halt wiring
and the contested-citation grounding hook."""

from __future__ import annotations

from setback.config import BENCH
from setback.court.bench import AdjudicationBench, reground_contested_citations
from setback.state.breakers import CircuitBreaker


class _FakeGrounder:
    """Records every anchor id it was asked to reground and returns a
    pre-programmed outcome, keyed by anchor id (default: always holds up)."""

    def __init__(self, outcomes: dict[str, bool] | None = None) -> None:
        self._outcomes = outcomes or {}
        self.regrounded: list[str] = []

    async def reground(self, anchor_id: str) -> bool:
        self.regrounded.append(anchor_id)
        return self._outcomes.get(anchor_id, True)


# --- AdjudicationBench -----------------------------------------------------------


def test_bench_default_tier_is_bench_config() -> None:
    bench = AdjudicationBench.default()

    assert bench.tier() == BENCH


def test_bench_tier_is_none_once_breaker_opens() -> None:
    breaker = CircuitBreaker(name="adjudicator", failure_threshold=3)
    bench = AdjudicationBench.default(breaker=breaker)

    bench.record_failure()
    bench.record_failure()
    bench.record_failure()

    assert bench.tier() is None


def test_bench_tier_returns_to_bench_config_after_success() -> None:
    breaker = CircuitBreaker(name="adjudicator", failure_threshold=1)
    bench = AdjudicationBench.default(breaker=breaker)
    bench.record_failure()
    assert bench.tier() is None

    # Simulate cooldown elapsing by using a breaker whose clock we control
    # instead: rebuild with a fake clock at the exact reset boundary.
    time_box = [0.0]
    breaker2 = CircuitBreaker(
        name="adjudicator",
        failure_threshold=1,
        reset_timeout_seconds=10.0,
        clock=lambda: time_box[0],
    )
    bench2 = AdjudicationBench.default(breaker=breaker2)
    bench2.record_failure()
    assert bench2.tier() is None
    time_box[0] = 10.0  # cooldown elapsed -> half-open -> primary allowed through
    assert bench2.tier() == BENCH
    bench2.record_success()
    assert bench2.tier() == BENCH


def test_bench_shares_breaker_state_across_grounds_when_same_instance_passed() -> None:
    breaker = CircuitBreaker(name="adjudicator", failure_threshold=2)
    ground_one_bench = AdjudicationBench.default(breaker=breaker)
    ground_one_bench.record_failure()

    # A second ground reusing the SAME breaker sees the accumulated failures.
    ground_two_bench = AdjudicationBench.default(breaker=breaker)
    ground_two_bench.record_failure()

    assert ground_two_bench.tier() is None
    assert ground_one_bench.tier() is None  # same underlying breaker


# --- reground_contested_citations ------------------------------------------------


async def test_reground_contested_citations_true_when_all_hold_up() -> None:
    grounder = _FakeGrounder()

    result = await reground_contested_citations(["a1", "a2"], grounder)

    assert result is True
    assert grounder.regrounded == ["a1", "a2"]


async def test_reground_contested_citations_false_on_first_failure() -> None:
    grounder = _FakeGrounder(outcomes={"a2": False})

    result = await reground_contested_citations(["a1", "a2", "a3"], grounder)

    assert result is False
    # Short-circuits: a3 is never even checked once a2 fails.
    assert grounder.regrounded == ["a1", "a2"]


async def test_reground_contested_citations_true_for_empty_anchor_list() -> None:
    grounder = _FakeGrounder()

    result = await reground_contested_citations([], grounder)

    assert result is True
    assert grounder.regrounded == []
