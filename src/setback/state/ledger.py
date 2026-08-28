"""Per-run token/cost accounting with a self-abort spend ceiling.

Every model call's cost is priced at Vertex AI list rates and booked
against a single run's :class:`Ledger`. Thinking tokens bill at the output
rate (they are Gemini "reasoning" output, not a separate SKU). A call whose
cost would push the running total past the ceiling is refused before it is
booked — the run stops itself rather than discovering the overage after
the fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from setback.models.client import TokenUsage

PRICING_USD_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.7-flash": (1.50, 7.50),
    "gemma-4-26b-a4b-it-maas": (0.15, 0.60),
}
"""Vertex AI list prices, USD per 1,000,000 tokens, as (input_rate, output_rate)."""

DEFAULT_RUN_CEILING_USD = 2.0
"""Default per-run self-abort ceiling, matching setback.config's demo-run cap."""


class BudgetExceededError(RuntimeError):
    """Raised when booking a call's cost would exceed the run's ceiling.

    The call is never booked: the ledger's totals are unchanged after this
    is raised, so the caller may still choose to degrade and retry cheaper.
    """


@dataclass(frozen=True)
class CallRecord:
    """One priced, ledgered model call."""

    stage: str
    model: str
    usage: TokenUsage
    cost_usd: float


@dataclass
class Ledger:
    """Accumulates cost for a single run and self-aborts past its ceiling."""

    ceiling_usd: float = DEFAULT_RUN_CEILING_USD
    _records: list[CallRecord] = field(default_factory=list, init=False, repr=False)

    @property
    def total_cost_usd(self) -> float:
        """The sum of every call's cost booked so far."""
        return sum(r.cost_usd for r in self._records)

    @property
    def records(self) -> tuple[CallRecord, ...]:
        """An immutable snapshot of every call booked so far, in order."""
        return tuple(self._records)

    def cost_for(self, model: str, usage: TokenUsage) -> float:
        """The USD cost of `usage` on `model`, at Vertex list prices.

        Raises:
            ValueError: `model` has no entry in the pricing table.
        """
        try:
            input_rate, output_rate = PRICING_USD_PER_MILLION_TOKENS[model]
        except KeyError as exc:
            raise ValueError(f"no pricing entry for model {model!r}") from exc
        input_cost = usage.prompt_tokens / 1_000_000 * input_rate
        output_cost = usage.billable_output_tokens / 1_000_000 * output_rate
        return input_cost + output_cost

    def record(self, *, stage: str, model: str, usage: TokenUsage) -> CallRecord:
        """Book a call's cost, raising :class:`BudgetExceededError` instead of
        booking it if doing so would exceed the run's ceiling."""
        cost = self.cost_for(model, usage)
        projected_total = self.total_cost_usd + cost
        if projected_total > self.ceiling_usd:
            raise BudgetExceededError(
                f"stage {stage!r} call on {model!r} would bring the run total to "
                f"${projected_total:.4f}, exceeding the ${self.ceiling_usd:.2f} ceiling"
            )
        call_record = CallRecord(stage=stage, model=model, usage=usage, cost_usd=cost)
        self._records.append(call_record)
        return call_record
