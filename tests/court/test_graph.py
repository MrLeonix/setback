"""Tests for setback.court.graph: the assembled ADK court workflow.

Everything runs offline through `FakeLlm` (see `_fakes.py`) -- a
`google.adk.models.BaseLlm` double handed to `Agent` in place of a real
model name. No network call, no ADC, no Vertex project reaches this file.
"""

from __future__ import annotations

import pytest
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from google.genai.types import ThinkingLevel

from setback.config import ModelConfig
from setback.court.bench import AdjudicationBench
from setback.court.graph import (
    ADJUDICATOR_NODE,
    CLAUSE_REVIEWER_NODE,
    EVIDENCE_REVIEWER_NODE,
    JOIN_NODE,
    TERMINAL_NODES,
    CourtOutcome,
    CourtVerdict,
    build_court_workflow,
    node_name_for_event,
    run_court,
)
from setback.court.roles import ClauseSlice, EvidenceSlice, ReviewStance
from setback.state.breakers import CircuitBreaker, CircuitState, DegradingBreaker
from tests.court._fakes import FakeLlm, adjudicator_body, review_body

_GROUND_ID = "g1"

_CLAUSE_SLICE = ClauseSlice(
    ground_id=_GROUND_ID,
    ground_text="The proposed dwelling exceeds the 9m height limit.",
    category="epi_dcp_provisions",
)

_EVIDENCE_SLICE = EvidenceSlice(
    ground_id=_GROUND_ID,
    ground_text="The proposed dwelling exceeds the 9m height limit.",
)

_KNOWN_ANCHOR_IDS = frozenset({"clause-4.3", "anchor-1"})


class _FakeGrounder:
    def __init__(self, holds_up: bool = True) -> None:
        self.holds_up = holds_up
        self.regrounded: list[str] = []

    async def reground(self, anchor_id: str) -> bool:
        self.regrounded.append(anchor_id)
        return self.holds_up


def _open_breaker() -> CircuitBreaker:
    breaker = CircuitBreaker(name="adjudicator", failure_threshold=1)
    breaker.record_failure()
    assert breaker.is_open
    return breaker


async def _run_events(workflow: object) -> list:
    runner = Runner(
        node=workflow,  # type: ignore[arg-type]
        app_name="test",
        session_service=InMemorySessionService(),
        auto_create_session=True,
    )
    return [
        e
        async for e in runner.run_async(
            user_id="u",
            session_id="s",
            new_message=types.Content(role="user", parts=[types.Part(text="go")]),
        )
    ]


def _terminal_verdict(events: list) -> CourtVerdict:
    terminal = [e for e in events if node_name_for_event(e) in TERMINAL_NODES]
    assert len(terminal) == 1, [node_name_for_event(e) for e in events]
    return CourtVerdict.model_validate(terminal[0].output)


# --- fan-out / join mechanics (the spike's core proof, exercised here too) -------


async def test_two_distinct_reviewer_executions_and_join_receives_both() -> None:
    """The spike's headline assertion: both reviewers actually ran as
    separate graph nodes (not a duplicated/cached single call), and the
    JoinNode's payload carries exactly the two expected predecessors --
    catching both the sequential-chain fan-out bug and a missing branch
    edge into the join."""
    workflow = build_court_workflow(
        _CLAUSE_SLICE,
        _EVIDENCE_SLICE,
        known_anchor_ids=_KNOWN_ANCHOR_IDS,
        adjudicator_model=None,
        clause_model=FakeLlm(
            model="fake-clause", bodies=[review_body(ground_id=_GROUND_ID, stance="support")]
        ),
        evidence_model=FakeLlm(
            model="fake-evidence", bodies=[review_body(ground_id=_GROUND_ID, stance="reject")]
        ),
    )

    events = await _run_events(workflow)

    model_events = [
        e
        for e in events
        if e.content and e.content.role == "model" and not e.partial and not e.get_function_calls()
    ]
    reviewer_events = [
        e for e in model_events if e.author in (CLAUSE_REVIEWER_NODE, EVIDENCE_REVIEWER_NODE)
    ]
    assert len(reviewer_events) == 2, [e.author for e in model_events]
    assert {e.author for e in reviewer_events} == {CLAUSE_REVIEWER_NODE, EVIDENCE_REVIEWER_NODE}

    join_events = [e for e in events if node_name_for_event(e) == JOIN_NODE]
    assert len(join_events) == 1
    assert set(join_events[0].output.keys()) == {CLAUSE_REVIEWER_NODE, EVIDENCE_REVIEWER_NODE}


# --- CLEAR path (the spike never exercised this live) ----------------------------


async def test_clear_path_when_both_reviewers_agree_confidently() -> None:
    verdict = await run_court(
        _CLAUSE_SLICE,
        _EVIDENCE_SLICE,
        known_anchor_ids=_KNOWN_ANCHOR_IDS,
        clause_model=FakeLlm(
            model="fake-clause",
            bodies=[review_body(ground_id=_GROUND_ID, stance="support", confidence=0.95)],
        ),
        evidence_model=FakeLlm(
            model="fake-evidence",
            bodies=[review_body(ground_id=_GROUND_ID, stance="support", confidence=0.9)],
        ),
        bench=AdjudicationBench.default(),
    )

    assert verdict.outcome is CourtOutcome.RESOLVED
    assert verdict.source == "unanimous"
    assert verdict.stance is ReviewStance.SUPPORT
    assert verdict.ground_id == _GROUND_ID


async def test_clear_path_never_calls_the_adjudicator() -> None:
    clause_fake = FakeLlm(
        model="fake-clause", bodies=[review_body(ground_id=_GROUND_ID, stance="support")]
    )
    evidence_fake = FakeLlm(
        model="fake-evidence", bodies=[review_body(ground_id=_GROUND_ID, stance="support")]
    )
    adjudicator_fake = FakeLlm(
        model="fake-adjudicator", bodies=[adjudicator_body(ground_id=_GROUND_ID)]
    )
    workflow = build_court_workflow(
        _CLAUSE_SLICE,
        _EVIDENCE_SLICE,
        known_anchor_ids=_KNOWN_ANCHOR_IDS,
        adjudicator_model=adjudicator_fake,
        clause_model=clause_fake,
        evidence_model=evidence_fake,
    )

    events = await _run_events(workflow)

    assert adjudicator_fake.call_count == 0
    assert not any(node_name_for_event(e) == ADJUDICATOR_NODE for e in events)


# --- SPLIT path resolved by the adjudicator --------------------------------------


async def test_split_path_resolved_by_adjudicator() -> None:
    workflow = build_court_workflow(
        _CLAUSE_SLICE,
        _EVIDENCE_SLICE,
        known_anchor_ids=_KNOWN_ANCHOR_IDS,
        adjudicator_model=FakeLlm(
            model="fake-adjudicator",
            bodies=[adjudicator_body(ground_id=_GROUND_ID, stance="support", confidence=0.9)],
        ),
        clause_model=FakeLlm(
            model="fake-clause", bodies=[review_body(ground_id=_GROUND_ID, stance="support")]
        ),
        evidence_model=FakeLlm(
            model="fake-evidence", bodies=[review_body(ground_id=_GROUND_ID, stance="reject")]
        ),
    )

    events = await _run_events(workflow)
    verdict = _terminal_verdict(events)

    assert verdict.outcome is CourtOutcome.RESOLVED
    assert verdict.source == "adjudicated"
    assert verdict.stance is ReviewStance.SUPPORT
    assert any(node_name_for_event(e) == ADJUDICATOR_NODE for e in events)


# --- SPLIT path, no adjudicator available: conservative default -----------------


async def test_split_path_conservative_default_when_adjudicator_unavailable() -> None:
    verdict = await run_court(
        _CLAUSE_SLICE,
        _EVIDENCE_SLICE,
        known_anchor_ids=_KNOWN_ANCHOR_IDS,
        clause_model=FakeLlm(
            model="fake-clause", bodies=[review_body(ground_id=_GROUND_ID, stance="support")]
        ),
        evidence_model=FakeLlm(
            model="fake-evidence", bodies=[review_body(ground_id=_GROUND_ID, stance="reject")]
        ),
        bench=AdjudicationBench.default(breaker=_open_breaker()),
    )

    assert verdict.outcome is CourtOutcome.UNRESOLVED_FLAGGED
    assert verdict.source == "conservative_default"
    assert verdict.confidence == 0.0


# --- voided-citation path ----------------------------------------------------------


async def test_voided_citation_forces_split_even_when_stances_agree() -> None:
    """A reviewer citing an anchor id absent from the case's known citation
    manifest is voided outright -- even though both reviewers "agree" on
    stance, the voided opinion must never be silently counted."""
    verdict = await run_court(
        _CLAUSE_SLICE,
        _EVIDENCE_SLICE,
        known_anchor_ids=frozenset({"clause-4.3"}),  # "anchor-1" is NOT known
        clause_model=FakeLlm(
            model="fake-clause",
            bodies=[
                review_body(ground_id=_GROUND_ID, stance="support", cited_anchor_ids=["clause-4.3"])
            ],
        ),
        evidence_model=FakeLlm(
            model="fake-evidence",
            bodies=[
                review_body(
                    ground_id=_GROUND_ID,
                    stance="support",
                    cited_anchor_ids=["anchor-1"],  # unresolvable -> voided
                )
            ],
        ),
        bench=AdjudicationBench.default(breaker=_open_breaker()),
    )

    # Both reviewers "agreed" on stance, but the evidence opinion was voided,
    # so this must route SPLIT (and, with no adjudicator available here,
    # fall to the conservative default) rather than CLEAR.
    assert verdict.outcome is CourtOutcome.UNRESOLVED_FLAGGED
    assert verdict.source == "conservative_default"


async def test_voided_citation_still_resolvable_via_adjudicator() -> None:
    """Same voided-citation setup as above, but with the adjudicator
    available -- it should be consulted and can still resolve the ground."""
    workflow = build_court_workflow(
        _CLAUSE_SLICE,
        _EVIDENCE_SLICE,
        known_anchor_ids=frozenset({"clause-4.3"}),
        adjudicator_model=FakeLlm(
            model="fake-adjudicator",
            bodies=[adjudicator_body(ground_id=_GROUND_ID, stance="support", confidence=0.9)],
        ),
        clause_model=FakeLlm(
            model="fake-clause",
            bodies=[
                review_body(ground_id=_GROUND_ID, stance="support", cited_anchor_ids=["clause-4.3"])
            ],
        ),
        evidence_model=FakeLlm(
            model="fake-evidence",
            bodies=[
                review_body(ground_id=_GROUND_ID, stance="support", cited_anchor_ids=["anchor-1"])
            ],
        ),
    )

    events = await _run_events(workflow)
    verdict = _terminal_verdict(events)

    assert any(node_name_for_event(e) == ADJUDICATOR_NODE for e in events)
    assert verdict.outcome is CourtOutcome.RESOLVED
    assert verdict.source == "adjudicated"


# --- contested-citation re-grounding hook ----------------------------------------


async def test_contested_citation_regrounding_failure_forces_conservative_default() -> None:
    grounder = _FakeGrounder(holds_up=False)
    workflow = build_court_workflow(
        _CLAUSE_SLICE,
        _EVIDENCE_SLICE,
        known_anchor_ids=_KNOWN_ANCHOR_IDS,
        adjudicator_model=FakeLlm(
            model="fake-adjudicator",
            bodies=[
                adjudicator_body(
                    ground_id=_GROUND_ID,
                    stance="support",
                    confidence=0.95,
                    cited_anchor_ids=["anchor-1"],
                )
            ],
        ),
        clause_model=FakeLlm(
            model="fake-clause", bodies=[review_body(ground_id=_GROUND_ID, stance="support")]
        ),
        evidence_model=FakeLlm(
            model="fake-evidence", bodies=[review_body(ground_id=_GROUND_ID, stance="reject")]
        ),
        contested_citation_grounder=grounder,
    )

    events = await _run_events(workflow)
    verdict = _terminal_verdict(events)

    assert verdict.outcome is CourtOutcome.UNRESOLVED_FLAGGED
    assert verdict.source == "conservative_default"
    assert grounder.regrounded == ["anchor-1"]


async def test_contested_citation_regrounding_success_keeps_adjudicated_resolution() -> None:
    grounder = _FakeGrounder(holds_up=True)
    workflow = build_court_workflow(
        _CLAUSE_SLICE,
        _EVIDENCE_SLICE,
        known_anchor_ids=_KNOWN_ANCHOR_IDS,
        adjudicator_model=FakeLlm(
            model="fake-adjudicator",
            bodies=[
                adjudicator_body(
                    ground_id=_GROUND_ID,
                    stance="support",
                    confidence=0.95,
                    cited_anchor_ids=["anchor-1"],
                )
            ],
        ),
        clause_model=FakeLlm(
            model="fake-clause", bodies=[review_body(ground_id=_GROUND_ID, stance="support")]
        ),
        evidence_model=FakeLlm(
            model="fake-evidence", bodies=[review_body(ground_id=_GROUND_ID, stance="reject")]
        ),
        contested_citation_grounder=grounder,
    )

    events = await _run_events(workflow)
    verdict = _terminal_verdict(events)

    assert verdict.outcome is CourtOutcome.RESOLVED
    assert verdict.source == "adjudicated"
    assert grounder.regrounded == ["anchor-1"]


async def test_low_confidence_adjudication_forces_conservative_default() -> None:
    workflow = build_court_workflow(
        _CLAUSE_SLICE,
        _EVIDENCE_SLICE,
        known_anchor_ids=_KNOWN_ANCHOR_IDS,
        adjudicator_model=FakeLlm(
            model="fake-adjudicator",
            bodies=[adjudicator_body(ground_id=_GROUND_ID, stance="support", confidence=0.1)],
        ),
        clause_model=FakeLlm(
            model="fake-clause", bodies=[review_body(ground_id=_GROUND_ID, stance="support")]
        ),
        evidence_model=FakeLlm(
            model="fake-evidence", bodies=[review_body(ground_id=_GROUND_ID, stance="reject")]
        ),
    )

    events = await _run_events(workflow)
    verdict = _terminal_verdict(events)

    assert verdict.outcome is CourtOutcome.UNRESOLVED_FLAGGED
    assert verdict.source == "conservative_default"


# --- breaker wiring via run_court -------------------------------------------------


def _fake_adjudicator_bench(
    breaker: CircuitBreaker, adjudicator_fake: FakeLlm
) -> AdjudicationBench:
    """An `AdjudicationBench` whose `tier()` carries a `FakeLlm` in place of
    the real `config.BENCH` model string, so `run_court`'s breaker-recording
    can be exercised end to end without ever risking a live Vertex call."""
    fake_tier = ModelConfig(model=adjudicator_fake, thinking_level=ThinkingLevel.LOW)  # type: ignore[arg-type]
    return AdjudicationBench(DegradingBreaker(breaker=breaker, primary=fake_tier, fallback=None))


async def test_run_court_records_adjudicator_success_on_the_shared_breaker() -> None:
    breaker = CircuitBreaker(name="adjudicator", failure_threshold=3)
    adjudicator_fake = FakeLlm(
        model="fake-adjudicator", bodies=[adjudicator_body(ground_id=_GROUND_ID, stance="support")]
    )
    bench = _fake_adjudicator_bench(breaker, adjudicator_fake)

    verdict = await run_court(
        _CLAUSE_SLICE,
        _EVIDENCE_SLICE,
        known_anchor_ids=_KNOWN_ANCHOR_IDS,
        clause_model=FakeLlm(
            model="fake-clause", bodies=[review_body(ground_id=_GROUND_ID, stance="support")]
        ),
        evidence_model=FakeLlm(
            model="fake-evidence", bodies=[review_body(ground_id=_GROUND_ID, stance="reject")]
        ),
        bench=bench,
    )

    assert verdict.source == "adjudicated"
    assert not breaker.is_open
    assert bench.tier() is not None


async def test_run_court_records_adjudicator_failure_on_the_shared_breaker() -> None:
    """An adjudicator agent whose model raises (simulating a call failure,
    e.g. a schema-validation error) must count as one breaker failure."""

    class _RaisingLlm(FakeLlm):
        async def generate_content_async(self, llm_request: object, stream: bool = False):  # type: ignore[override]
            raise RuntimeError("simulated adjudicator call failure")
            yield  # pragma: no cover - makes this an async generator

    breaker = CircuitBreaker(name="adjudicator", failure_threshold=3)
    bench = _fake_adjudicator_bench(
        breaker, _RaisingLlm(model="fake-adjudicator-raising", bodies=[])
    )

    with pytest.raises(Exception):  # noqa: B017 - the ADK wraps the raw RuntimeError
        await run_court(
            _CLAUSE_SLICE,
            _EVIDENCE_SLICE,
            known_anchor_ids=_KNOWN_ANCHOR_IDS,
            clause_model=FakeLlm(
                model="fake-clause", bodies=[review_body(ground_id=_GROUND_ID, stance="support")]
            ),
            evidence_model=FakeLlm(
                model="fake-evidence", bodies=[review_body(ground_id=_GROUND_ID, stance="reject")]
            ),
            bench=bench,
        )

    assert breaker.state is CircuitState.CLOSED  # one failure, below the threshold of 3


async def test_build_court_workflow_rejects_mismatched_ground_ids() -> None:
    other_evidence = EvidenceSlice(ground_id="different-ground", ground_text="x")

    with pytest.raises(ValueError, match="same ground_id"):
        build_court_workflow(
            _CLAUSE_SLICE,
            other_evidence,
            known_anchor_ids=_KNOWN_ANCHOR_IDS,
            adjudicator_model=None,
        )
