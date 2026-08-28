"""Tests for setback.state.ledger: per-run token/cost accounting with a
self-abort spend ceiling."""

from __future__ import annotations

import pytest

from setback.models.client import TokenUsage
from setback.state.ledger import DEFAULT_RUN_CEILING_USD, BudgetExceededError, Ledger


def test_default_ceiling_is_two_dollars() -> None:
    assert DEFAULT_RUN_CEILING_USD == 2.0
    assert Ledger().ceiling_usd == 2.0


def test_cost_for_flash_lite_matches_vertex_pricing() -> None:
    ledger = Ledger()
    usage = TokenUsage(prompt_tokens=1_000_000, output_tokens=1_000_000, thinking_tokens=0)

    cost = ledger.cost_for("gemini-3.5-flash-lite", usage)

    assert cost == pytest.approx(0.30 + 2.50)


def test_cost_for_bench_model_bills_thinking_tokens_at_output_rate() -> None:
    ledger = Ledger()
    usage = TokenUsage(prompt_tokens=0, output_tokens=0, thinking_tokens=1_000_000)

    cost = ledger.cost_for("gemini-3.7-flash", usage)

    assert cost == pytest.approx(7.50)


def test_cost_for_gemma_maas_model() -> None:
    ledger = Ledger()
    usage = TokenUsage(prompt_tokens=1_000_000, output_tokens=1_000_000, thinking_tokens=0)

    cost = ledger.cost_for("gemma-4-26b-a4b-it-maas", usage)

    assert cost == pytest.approx(0.15 + 0.60)


def test_cost_for_unknown_model_raises_value_error() -> None:
    ledger = Ledger()
    usage = TokenUsage(prompt_tokens=1, output_tokens=1)

    with pytest.raises(ValueError, match="no pricing"):
        ledger.cost_for("some-unpriced-model", usage)


def test_record_accumulates_total_cost_across_calls() -> None:
    ledger = Ledger(ceiling_usd=10.0)
    usage = TokenUsage(prompt_tokens=100_000, output_tokens=100_000)

    ledger.record(stage="interview", model="gemini-3.5-flash-lite", usage=usage)
    ledger.record(stage="bench", model="gemini-3.7-flash", usage=usage)

    expected = ledger.cost_for("gemini-3.5-flash-lite", usage) + ledger.cost_for(
        "gemini-3.7-flash", usage
    )
    assert ledger.total_cost_usd == pytest.approx(expected)
    assert len(ledger.records) == 2


def test_record_raises_budget_exceeded_over_ceiling() -> None:
    ledger = Ledger(ceiling_usd=0.001)
    usage = TokenUsage(prompt_tokens=1_000_000, output_tokens=1_000_000)

    with pytest.raises(BudgetExceededError):
        ledger.record(stage="bench", model="gemini-3.7-flash", usage=usage)


def test_record_does_not_append_a_call_that_breaches_the_ceiling() -> None:
    ledger = Ledger(ceiling_usd=0.001)
    usage = TokenUsage(prompt_tokens=1_000_000, output_tokens=1_000_000)

    with pytest.raises(BudgetExceededError):
        ledger.record(stage="bench", model="gemini-3.7-flash", usage=usage)

    assert ledger.total_cost_usd == 0.0
    assert ledger.records == ()


def test_record_allows_calls_up_to_the_ceiling_then_blocks_the_next() -> None:
    ledger = Ledger(ceiling_usd=3.0)
    cheap_usage = TokenUsage(prompt_tokens=1_000_000, output_tokens=1_000_000)  # $2.80

    ledger.record(stage="interview", model="gemini-3.5-flash-lite", usage=cheap_usage)
    assert ledger.total_cost_usd == pytest.approx(2.80)

    with pytest.raises(BudgetExceededError):
        ledger.record(stage="interview", model="gemini-3.5-flash-lite", usage=cheap_usage)

    assert ledger.total_cost_usd == pytest.approx(2.80)


def test_records_property_is_an_immutable_snapshot() -> None:
    ledger = Ledger()
    usage = TokenUsage(prompt_tokens=1, output_tokens=1)
    ledger.record(stage="interview", model="gemini-3.5-flash-lite", usage=usage)

    records = ledger.records
    assert isinstance(records, tuple)
    assert records[0].stage == "interview"
    assert records[0].model == "gemini-3.5-flash-lite"
    assert records[0].usage == usage
