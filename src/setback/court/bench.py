"""The adjudication bench: the adjudicator's degrade-not-halt wiring, plus the
second-pass contested-citation grounding hook.

The adjudicator already runs the top model tier (`config.BENCH`, i.e.
``gemini-3.7-flash`` at ``LOW`` thinking) — there is nowhere lower to
degrade to, unlike the reviewers' breakers. So :class:`AdjudicationBench`
doesn't choose between two model tiers the way a degrading reviewer would;
its `DegradingBreaker` chooses between "call the adjudicator"
(`primary=config.BENCH`) and "skip the call and fall straight to the
conservative default" (`fallback=None`), once three consecutive adjudicator
call failures have opened its breaker (ARCHITECTURE.md §4). This is exactly
`state.breakers.DegradingBreaker`, reused as-is with `ModelConfig | None` as
its value type — no new abstraction.

The second half of this module is the contested-citation grounding hook
(spike-grounding.md): when the adjudicator resolves a SPLIT ground, any
anchor id it cites should survive a second grounding pass on the higher
model tier before the resolution is trusted. The evidence package doesn't
expose a public function for that yet (`evidence.dossier.build_dossier` is
still a `NotImplementedError` stub) — :class:`ContestedCitationGrounder` is
the narrow port the integrator wires a real implementation into once it
does; tests supply a fake.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from setback.config import BENCH, ModelConfig
from setback.state.breakers import CircuitBreaker, DegradingBreaker


@runtime_checkable
class ContestedCitationGrounder(Protocol):
    """A second grounding pass for one anchor id the adjudicator cited on a
    contested (SPLIT) ground.

    Implementations live in the evidence package once it exposes one; this
    protocol only declares the shape :mod:`setback.court.graph` depends on.
    """

    async def reground(self, anchor_id: str) -> bool:
        """Re-verify that `anchor_id` still resolves under a second,
        higher-tier grounding pass.

        Returns:
            True if the citation holds up; False if it does not (and the
            adjudicator's resolution should therefore not be trusted).
        """
        ...


@dataclass(frozen=True)
class AdjudicationBench:
    """Wires the `AdjudicatorNode`'s degrade-not-halt decision.

    `tier()` returns `config.BENCH` while the underlying breaker is closed
    or half-open (letting a recovery probe through), and `None` only while
    it is genuinely open — the caller's signal to skip the model call
    entirely and fall back to the conservative default rather than retrying
    a tier there is no lower fallback for.
    """

    degrading: DegradingBreaker[ModelConfig | None]

    @classmethod
    def default(cls, breaker: CircuitBreaker | None = None) -> AdjudicationBench:
        """Build a bench over a fresh (or caller-supplied) breaker.

        Passing the same `CircuitBreaker` back in across cases/grounds is
        how a caller opts into persisted degrade-not-halt state; a fresh
        breaker (the default) starts closed every time.
        """
        return cls(
            DegradingBreaker(
                breaker=breaker or CircuitBreaker(name="adjudicator"),
                primary=BENCH,
                fallback=None,
            )
        )

    def tier(self) -> ModelConfig | None:
        """The adjudicator model to call, or `None` to skip straight to the
        conservative default."""
        return self.degrading.current()

    def record_success(self) -> None:
        """Report that a call made with `tier()` succeeded."""
        self.degrading.record_success()

    def record_failure(self) -> None:
        """Report that a call made with `tier()` failed."""
        self.degrading.record_failure()


async def reground_contested_citations(
    anchor_ids: Iterable[str], grounder: ContestedCitationGrounder
) -> bool:
    """Re-verify every anchor id the adjudicator cited via a second,
    higher-tier grounding pass.

    Args:
        anchor_ids: The adjudicator's cited anchor ids.
        grounder: The second-pass grounding port.

    Returns:
        True only if every citation still holds up. A single failed
        recheck is enough to distrust the adjudicator's resolution — the
        caller should fall back to the conservative default rather than
        ship a citation that didn't survive re-grounding.
    """
    for anchor_id in anchor_ids:
        if not await grounder.reground(anchor_id):
            return False
    return True
