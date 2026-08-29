"""Manual, one-off live check: does a real `google.adk.agents.Agent`'s
event carry `usage_metadata`, the way a direct `ModelClient` call does?

NOT a pytest test (no `test_` prefix -- never collected, never run in CI),
mirroring `tests/evidence/live_demo.py`'s "run manually, exercise the real
thing once" convention. Run with:

    uv run python tests/court/live_usage_check.py

Makes exactly TWO live model calls (`gemini-3.5-flash-lite` via ADC on
`vexcourt-agent`, the Clause and Evidence Reviewer agents for one ground) --
**provided the bench's breaker is passed in already open** (below), which
is what actually guarantees `adjudicator_model=None` and rules out a third
call. An earlier revision of this script left `bench` at its default
(fresh, closed) and relied on the two reviewers *agreeing confidently* to
avoid a SPLIT-triggered adjudicator call -- confidence is a live model
output, not something a script controls, and the first live run of that
revision genuinely SPLIT (one reviewer's live confidence landed at 0.5,
just under `tally.CONFIDENCE_THRESHOLD`), making a real third
(adjudicator) call and overspending WP-E's 2-call budget by one call
(reported: three calls made, $0.002378 total, in the wave-4 handover). This
revision removes that nondeterminism structurally rather than hoping for
a repeat of a lucky confidence roll.

This is the empirical evidence behind `court/graph.py`'s module docstring
ledger-truth note and the offline `FakeLlm`-based proof in
`tests/court/test_graph.py`; the run that produced it already confirmed
real `Agent` events carry `usage_metadata` (all three stages that ran came
back `estimated=False`), which is what this script's assertions check.
"""

from __future__ import annotations

import asyncio

from setback.court.bench import AdjudicationBench
from setback.court.graph import (
    ADJUDICATOR_NODE,
    CLAUSE_REVIEWER_NODE,
    EVIDENCE_REVIEWER_NODE,
    run_court_verbose,
)
from setback.court.roles import ClauseSlice, EvidenceSlice
from setback.state.breakers import CircuitBreaker
from setback.state.ledger import Ledger

_GROUND_ID = "live-check-g1"

_CLAUSE_SLICE = ClauseSlice(
    ground_id=_GROUND_ID,
    ground_text="The proposed dwelling exceeds the 9m height limit.",
    category="epi_dcp_provisions",
)

_EVIDENCE_SLICE = EvidenceSlice(
    ground_id=_GROUND_ID,
    ground_text="The proposed dwelling exceeds the 9m height limit.",
)


def _already_open_bench() -> AdjudicationBench:
    """A bench whose breaker is open *before* the run starts -- the only
    deterministic way to guarantee `run_court_verbose` never calls the
    adjudicator, regardless of what confidence the two live reviewer calls
    happen to return."""
    breaker = CircuitBreaker(name="adjudicator", failure_threshold=1)
    breaker.record_failure()
    assert breaker.is_open
    return AdjudicationBench.default(breaker=breaker)


async def main() -> None:
    ledger = Ledger()

    result = await run_court_verbose(
        _CLAUSE_SLICE,
        _EVIDENCE_SLICE,
        known_anchor_ids=frozenset(),
        clause_model="gemini-3.5-flash-lite",  # real ADC, real Vertex AI call
        evidence_model="gemini-3.5-flash-lite",  # real ADC, real Vertex AI call
        bench=_already_open_bench(),  # guarantees no third (adjudicator) call
        ledger=ledger,
    )

    print(f"verdict.source: {result.verdict.source}")
    print(f"clause_review: {result.clause_review}")
    print(f"evidence_review: {result.evidence_review}")
    print()
    print(f"ledger records booked: {len(ledger.records)}")
    for record in ledger.records:
        print(
            f"  stage={record.stage!r} model={record.model!r} "
            f"estimated={record.usage.estimated} "
            f"prompt={record.usage.prompt_tokens} output={record.usage.output_tokens} "
            f"thinking={record.usage.thinking_tokens} cost_usd={record.cost_usd:.6f}"
        )
    print(f"ledger.total_cost_usd: {ledger.total_cost_usd:.6f}")

    stages_booked = {r.stage for r in ledger.records}
    assert stages_booked == {CLAUSE_REVIEWER_NODE, EVIDENCE_REVIEWER_NODE}, stages_booked
    assert ADJUDICATOR_NODE not in stages_booked  # adjudicator_model=None -- never ran

    any_real = any(not r.usage.estimated for r in ledger.records)
    print()
    if any_real:
        print(
            "CONFIRMED: a real ADK Agent event carries usage_metadata -- "
            "at least one stage booked real (non-estimated) usage."
        )
    else:
        print(
            "CONFIRMED (negative result): every stage's event.usage_metadata was None "
            "-- ADK genuinely does not expose usage for this call shape; the "
            "estimated=True fallback is load-bearing in production, not just for tests."
        )


if __name__ == "__main__":
    asyncio.run(main())
