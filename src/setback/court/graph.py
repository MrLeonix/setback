"""The ADK court graph: two parallel, structurally disjoint reviewers, a
deterministic tally, and conditional adjudication.

Built from the proven construction in ``spike-adkCourt.md``, exactly:

* Fan-out/fan-in **must** use the nested-tuple edge form —
  ``(START, (clause_agent, evidence_agent))`` and
  ``((clause_agent, evidence_agent), join)`` — never a bare top-level tuple
  of three-plus nodes, which ADK parses as a *sequential chain* instead of
  parallel branches. Both forms "run" without error; only the nested form is
  actually parallel and feeds both agents the same shared input.
* Every branch that must reach the `JoinNode` needs its own explicit edge
  into it — a forgotten branch edge still runs that branch, but silently
  drops its output from the join payload.
* An `LlmAgent` node's external event has its `.output` field cleared by the
  ADK `Runner` even when structured output parsed successfully. Every
  terminal node in this graph (`finalize_clear`, `post_adjudicate`,
  `conservative_default`) is a plain `FunctionNode`, whose events are never
  subject to that clearing, so `run_court` never has to work around it.

::

    IngestNode output (ClauseSlice + EvidenceSlice)
              |
        ┌─────┴─────┐
        │           │
    clause_reviewer  evidence_reviewer      (parallel, disjoint)
        │           │
        └─────┬─────┘
              │
             join
              │
            tally  ──CLEAR──> finalize_clear ─────────────┐
              │                                            │
            SPLIT                                          ▼
              │                                    CourtVerdict (terminal)
       ┌──────┴───────┐                                    ▲
       │ (bench open)  │ (bench closed/half-open)           │
       ▼               ▼                                    │
  conservative_    adjudicator ──> post_adjudicate ──────────┘
  default

**Ledger truth.** Every `google.adk.agents.Agent` node this module builds
calls Vertex AI directly through ADK's own internal transport, never
through :class:`setback.models.client.ModelClient` — so, prior to this
fix, none of the reviewers' or the adjudicator's token usage ever reached
:class:`setback.state.ledger.Ledger`, silently understating a run's real
cost (job/pipeline.py's own "known gap" docstring note flagged exactly
this). `Event` extends ADK's `LlmResponse`, which carries the same
`usage_metadata: types.GenerateContentResponseUsageMetadata | None` field
`ModelClient._generate_gemini` already reads — verified both offline
(`tests/court/test_graph.py`'s ledger-truth tests, via `FakeLlm`, which
deliberately never sets it) and live (`tests/court/live_usage_check.py`,
one real Vertex call: a real `Agent`-driven reviewer call *does* populate
`event.usage_metadata`, exactly like a direct `genai` call does). Pass
`ledger=` to :func:`run_court`/:func:`run_court_verbose` to book each
stage's usage as it's extracted from that run's own event stream; omit it
(the default) for exactly the prior, unledgered behaviour. When an event
genuinely carries no usage (a transport that doesn't report it, or a
`BaseLlm` test double), the booked :class:`~setback.models.client.TokenUsage`
falls back to a `len(text) // 4` character-based estimate and is marked
`estimated=True` on the record — an honest, labelled guess rather than a
silent zero or a fabricated precise number.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Literal

from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.models import BaseLlm
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, InMemorySessionService
from google.adk.workflow import START, FunctionNode, JoinNode, Workflow
from google.genai import types
from pydantic import BaseModel, ConfigDict

from setback import config

# A bare model-id string handed to `google.adk.agents.Agent` (every real
# reviewer/adjudicator node this module builds) constructs its OWN internal
# `genai.Client` lazily, on first use -- unlike `setback.models.client.
# ModelClient`, which passes `vertexai=True, project=..., location=...`
# explicitly. ADK's client instead reads these three environment variables
# (falling back to the public Gemini Developer API, which then fails with
# "No API key was provided", if they are unset). This is the exact Vertex
# config the live spike used (`spike-adkCourt.md`: "Vertex config:
# GOOGLE_GENAI_USE_VERTEXAI=TRUE, GOOGLE_CLOUD_PROJECT=vexcourt-agent,
# GOOGLE_CLOUD_LOCATION=global") -- set here, once, at import time, from
# `setback.config`'s own project/location so every ADK agent this module
# builds is correctly Vertex-routed under ADC without every caller needing
# to remember to set them. `setdefault` never overrides an operator's own
# explicit environment configuration.
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", config.GCP_PROJECT)
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", config.VERTEX_LOCATION)
from setback.court import roles, tally
from setback.court.bench import (
    AdjudicationBench,
    ContestedCitationGrounder,
    reground_contested_citations,
)
from setback.court.roles import (
    AdjudicatorOutput,
    ClauseSlice,
    EvidenceSlice,
    ReviewOutput,
    ReviewStance,
)
from setback.models.client import TokenUsage
from setback.state.ledger import Ledger

CLAUSE_REVIEWER_NODE = "clause_reviewer"
EVIDENCE_REVIEWER_NODE = "evidence_reviewer"
JOIN_NODE = "join"
TALLY_NODE = "tally"
FINALIZE_CLEAR_NODE = "finalize_clear"
ADJUDICATOR_NODE = "adjudicator"
POST_ADJUDICATE_NODE = "post_adjudicate"
CONSERVATIVE_DEFAULT_NODE = "conservative_default"

TERMINAL_NODES = frozenset({FINALIZE_CLEAR_NODE, POST_ADJUDICATE_NODE, CONSERVATIVE_DEFAULT_NODE})


class CourtOutcome(StrEnum):
    """Whether the court graph reached a confident resolution for a ground."""

    RESOLVED = "resolved"
    """Unanimous agreement, or an adjudication with genuine confidence."""

    UNRESOLVED_FLAGGED = "unresolved_flagged"
    """The conservative default: adjudication was unavailable, contested
    citations failed a recheck, or the adjudicator itself lacked confidence.
    The ground is flagged for human review, never guessed at
    (ARCHITECTURE.md §2, "conservative default on unresolved split")."""


class CourtVerdict(BaseModel):
    """The court graph's final, single decision for one candidate ground."""

    ground_id: str
    outcome: CourtOutcome
    stance: ReviewStance
    confidence: float
    cited_anchor_ids: tuple[str, ...]
    rationale: str
    source: Literal["unanimous", "adjudicated", "conservative_default"]


def node_name_for_event(event: Event) -> str | None:
    """The workflow node name that produced `event`.

    Measured live against this ADK build (see `court_offline_check.py` in
    the spike scratchpad): non-`LlmAgent` node events (`JoinNode`,
    `FunctionNode`) carry `event.author == <workflow name>`, not the node's
    own name — the reliable signal is `event.node_info.path`, whose last
    segment (before its `@run_id` suffix) is the node name. `LlmAgent`
    nodes' events do carry their own name in `.author` as well, so this
    helper works uniformly for every node type.
    """
    if event.node_info is None or not event.node_info.path:
        return None
    return event.node_info.path.rsplit("/", 1)[-1].split("@", 1)[0]


def _dedupe(anchor_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Order-preserving de-duplication of cited anchor ids from both reviewers."""
    return tuple(dict.fromkeys(anchor_ids))


def _model_response_event(events: Sequence[Event], node_name: str) -> Event | None:
    """The terminal model-response event `node_name` produced -- the one
    carrying (or, per the module docstring, genuinely lacking) its
    `usage_metadata` -- never a partial chunk or a function-call turn."""
    for event in events:
        if node_name_for_event(event) != node_name:
            continue
        if event.content is None or event.content.role != "model":
            continue
        if event.partial or event.get_function_calls():
            continue
        return event
    return None


def _event_text(event: Event | None) -> str:
    """Every text part of `event`'s content, concatenated -- the raw
    response text used for the character-based fallback estimate."""
    if event is None or event.content is None or not event.content.parts:
        return ""
    return "".join(part.text for part in event.content.parts if part.text)


def _estimate_tokens(text: str) -> int:
    """A deliberately rough, ~4-characters-per-token estimate (the common
    heuristic for English prose) used only when `event.usage_metadata` is
    genuinely absent -- see the module docstring's ledger-truth note."""
    return max(1, len(text) // 4)


def _usage_for_event(event: Event | None, *, prompt_text: str) -> TokenUsage:
    """The real, model-reported usage from `event.usage_metadata` when ADK
    provided one; otherwise a marked `estimated=True` character-count guess
    from `prompt_text` and the event's own response text."""
    metadata = event.usage_metadata if event is not None else None
    if metadata is not None:
        return TokenUsage(
            prompt_tokens=metadata.prompt_token_count or 0,
            output_tokens=metadata.candidates_token_count or 0,
            thinking_tokens=metadata.thoughts_token_count or 0,
            estimated=False,
        )
    return TokenUsage(
        prompt_tokens=_estimate_tokens(prompt_text),
        output_tokens=_estimate_tokens(_event_text(event)),
        estimated=True,
    )


def _model_name(model: str | BaseLlm) -> str:
    """The pricing-table model id for `model`, whether it's a bare string or
    a `BaseLlm` (real or test double) -- every `BaseLlm` carries its own
    `.model` field (see `tests/court/_fakes.py:FakeLlm`)."""
    return model if isinstance(model, str) else model.model


def _book_stage_usage(
    ledger: Ledger,
    events: Sequence[Event],
    *,
    clause_slice: ClauseSlice,
    evidence_slice: EvidenceSlice,
    clause_model: str | BaseLlm,
    evidence_model: str | BaseLlm,
    adjudicator_model: str | BaseLlm | None,
) -> None:
    """Book every stage this run actually executed against `ledger`,
    extracted straight from `events` -- the ledger-truth fix described in
    the module docstring. Raises :class:`~setback.state.ledger.
    BudgetExceededError` (propagated from `Ledger.record`) exactly like any
    other ledgered call, per ARCHITECTURE.md §4's hard-stop semantics."""
    ledger.record(
        stage=CLAUSE_REVIEWER_NODE,
        model=_model_name(clause_model),
        usage=_usage_for_event(
            _model_response_event(events, CLAUSE_REVIEWER_NODE),
            prompt_text=roles.render_clause_slice(clause_slice),
        ),
    )
    ledger.record(
        stage=EVIDENCE_REVIEWER_NODE,
        model=_model_name(evidence_model),
        usage=_usage_for_event(
            _model_response_event(events, EVIDENCE_REVIEWER_NODE),
            prompt_text=roles.render_evidence_slice(evidence_slice),
        ),
    )
    if adjudicator_model is None:
        return
    adjudicator_event = _model_response_event(events, ADJUDICATOR_NODE)
    if adjudicator_event is None:
        return  # CLEAR path (or bench-open) -- the adjudicator never ran.
    clause_review, evidence_review = _extract_reviews(events)
    adjudicator_prompt = (
        f"clause_review: {clause_review.model_dump_json() if clause_review else 'voided'}\n"
        f"evidence_review: {evidence_review.model_dump_json() if evidence_review else 'voided'}"
    )
    ledger.record(
        stage=ADJUDICATOR_NODE,
        model=_model_name(adjudicator_model),
        usage=_usage_for_event(adjudicator_event, prompt_text=adjudicator_prompt),
    )


def _make_tally_node(known_anchor_ids: frozenset[str]) -> FunctionNode:
    """Build the tally `FunctionNode`: voids uncited opinions, then routes
    CLEAR/SPLIT per `setback.court.tally`."""

    def _tally(node_input: dict[str, Any]) -> Event:
        clause_raw = node_input.get(CLAUSE_REVIEWER_NODE)
        evidence_raw = node_input.get(EVIDENCE_REVIEWER_NODE)
        ground_id = (clause_raw or evidence_raw or {}).get("ground_id", "")

        clause = ReviewOutput.model_validate(clause_raw) if clause_raw is not None else None
        evidence = ReviewOutput.model_validate(evidence_raw) if evidence_raw is not None else None
        if clause is not None and evidence is not None and clause.ground_id != evidence.ground_id:
            raise ValueError(
                f"clause_reviewer and evidence_reviewer reviewed different grounds: "
                f"{clause.ground_id!r} vs {evidence.ground_id!r}"
            )

        clause = tally.void_if_uncited(clause, known_anchor_ids) if clause is not None else None
        evidence = (
            tally.void_if_uncited(evidence, known_anchor_ids) if evidence is not None else None
        )
        route = tally.tally(clause, evidence)

        payload: dict[str, Any] = {
            "ground_id": ground_id,
            "clause": clause.model_dump(mode="json") if clause is not None else None,
            "evidence": evidence.model_dump(mode="json") if evidence is not None else None,
        }
        return Event(output=payload, actions=EventActions(route=route.value))

    return FunctionNode(func=_tally, name=TALLY_NODE)


def _finalize_clear(node_input: dict[str, Any]) -> dict[str, Any]:
    """CLEAR path: both reviewers survived voiding, agreed, and were
    confident — no adjudicator call needed."""
    clause = ReviewOutput.model_validate(node_input["clause"])
    evidence = ReviewOutput.model_validate(node_input["evidence"])
    verdict = CourtVerdict(
        ground_id=node_input["ground_id"],
        outcome=CourtOutcome.RESOLVED,
        stance=clause.stance,
        confidence=min(clause.confidence, evidence.confidence),
        cited_anchor_ids=_dedupe(clause.cited_anchor_ids + evidence.cited_anchor_ids),
        rationale=f"Clause Reviewer: {clause.rationale} | Evidence Reviewer: {evidence.rationale}",
        source="unanimous",
    )
    return verdict.model_dump(mode="json")


def _conservative_default(node_input: dict[str, Any]) -> dict[str, Any]:
    """SPLIT path with no adjudicator available (its breaker is open): the
    ground is flagged, never shipped, never guessed at."""
    verdict = CourtVerdict(
        ground_id=node_input["ground_id"],
        outcome=CourtOutcome.UNRESOLVED_FLAGGED,
        stance=ReviewStance.REJECT,
        confidence=0.0,
        cited_anchor_ids=(),
        rationale=(
            "Reviewers disagreed (or a citation was voided) and adjudication was "
            "unavailable; the ground is flagged for human review rather than shipped."
        ),
        source="conservative_default",
    )
    return verdict.model_dump(mode="json")


def _make_post_adjudicate_node(grounder: ContestedCitationGrounder | None) -> FunctionNode:
    """Build the post-adjudication `FunctionNode`: applies the contested-
    citation recheck and the conservative default when confidence or
    re-grounding doesn't hold up.

    A plain `FunctionNode`, deliberately — its event is never subject to the
    `LlmAgent` output-clearing quirk, so this is also where the graph's
    external output for the SPLIT-then-adjudicated path becomes trustworthy
    to read straight off `event.output`.
    """

    async def _post_adjudicate(node_input: dict[str, Any]) -> dict[str, Any]:
        adjudication = AdjudicatorOutput.model_validate(node_input)
        resolved = adjudication.confidence >= tally.CONFIDENCE_THRESHOLD
        if resolved and grounder is not None and adjudication.cited_anchor_ids:
            resolved = await reground_contested_citations(adjudication.cited_anchor_ids, grounder)

        if not resolved:
            verdict = CourtVerdict(
                ground_id=adjudication.ground_id,
                outcome=CourtOutcome.UNRESOLVED_FLAGGED,
                stance=ReviewStance.REJECT,
                confidence=0.0,
                cited_anchor_ids=(),
                rationale=(
                    "The adjudicator could not confidently resolve this ground, or one "
                    "of its cited anchors failed a contested-citation recheck; flagged "
                    "for human review rather than shipped."
                ),
                source="conservative_default",
            )
        else:
            verdict = CourtVerdict(
                ground_id=adjudication.ground_id,
                outcome=CourtOutcome.RESOLVED,
                stance=adjudication.stance,
                confidence=adjudication.confidence,
                cited_anchor_ids=adjudication.cited_anchor_ids,
                rationale=adjudication.rationale,
                source="adjudicated",
            )
        return verdict.model_dump(mode="json")

    return FunctionNode(func=_post_adjudicate, name=POST_ADJUDICATE_NODE)


def build_court_workflow(
    clause_slice: ClauseSlice,
    evidence_slice: EvidenceSlice,
    *,
    known_anchor_ids: frozenset[str],
    adjudicator_model: str | BaseLlm | None,
    clause_model: str | BaseLlm = config.INTERVIEW.model,
    evidence_model: str | BaseLlm = config.INTERVIEW.model,
    contested_citation_grounder: ContestedCitationGrounder | None = None,
) -> Workflow:
    """Construct the ADK workflow graph for one ground's adversarial review.

    Args:
        clause_slice: The Clause Reviewer's entire input for this ground.
        evidence_slice: The Evidence Reviewer's entire input for this ground
            (must share `clause_slice.ground_id`).
        known_anchor_ids: The case's citation manifest — every clause ref
            and evidence anchor id a reviewer is allowed to cite.
        adjudicator_model: The adjudicator's model (string id or an injected
            `BaseLlm` fake), or `None` to wire the SPLIT route straight to
            the conservative default without ever building an adjudicator
            node — the caller's signal that `AdjudicationBench.tier()` is
            currently open.
        clause_model: The Clause Reviewer's model (string id or a `BaseLlm`
            fake). Defaults to the shared cheap worker tier.
        evidence_model: The Evidence Reviewer's model (string id or a
            `BaseLlm` fake). Defaults to the shared cheap worker tier.
        contested_citation_grounder: The second-pass grounding port for an
            adjudicated ground's cited anchors, or `None` to skip that check.

    Returns:
        The constructed workflow, ready to run against a single shared
        `new_message` via an ADK `Runner`.

    Raises:
        ValueError: `clause_slice` and `evidence_slice` review different
            grounds.
    """
    if clause_slice.ground_id != evidence_slice.ground_id:
        raise ValueError(
            f"clause_slice and evidence_slice must review the same ground_id: "
            f"{clause_slice.ground_id!r} vs {evidence_slice.ground_id!r}"
        )

    clause_agent = roles.build_clause_reviewer_agent(
        clause_slice, model=clause_model, thinking_level=config.INTERVIEW.thinking_level
    )
    evidence_agent = roles.build_evidence_reviewer_agent(
        evidence_slice, model=evidence_model, thinking_level=config.INTERVIEW.thinking_level
    )
    join = JoinNode(name=JOIN_NODE)
    tally_node = _make_tally_node(known_anchor_ids)
    finalize_clear_node = FunctionNode(func=_finalize_clear, name=FINALIZE_CLEAR_NODE)

    if adjudicator_model is not None:
        adjudicator_agent = roles.build_adjudicator_agent(
            model=adjudicator_model, thinking_level=config.BENCH.thinking_level
        )
        post_adjudicate_node = _make_post_adjudicate_node(contested_citation_grounder)
        split_edges = [
            (tally_node, {"CLEAR": finalize_clear_node, "SPLIT": adjudicator_agent}),
            (adjudicator_agent, post_adjudicate_node),
        ]
    else:
        conservative_node = FunctionNode(func=_conservative_default, name=CONSERVATIVE_DEFAULT_NODE)
        split_edges = [(tally_node, {"CLEAR": finalize_clear_node, "SPLIT": conservative_node})]

    # Nested-tuple fan-out/fan-in -- the proven, non-obvious construction.
    # A bare `(START, clause_agent, evidence_agent)` would be parsed as a
    # sequential chain instead; see the module docstring and
    # spike-adkCourt.md.
    edges: list[Any] = [
        (START, (clause_agent, evidence_agent)),
        ((clause_agent, evidence_agent), join),
        (join, tally_node),
        *split_edges,
    ]
    return Workflow(name="court", edges=edges)


class CourtRunResult(BaseModel):
    """A court run's final verdict, plus the two raw reviewer opinions the
    tally node computed on the way there -- for a caller (the tribunal job)
    that needs to show both reviewer opinions to the resident, not just the
    final decision.

    `clause_review`/`evidence_review` are `None` exactly when
    :func:`setback.court.tally.void_if_uncited` voided that opinion (an
    uncited-anchor citation failure), matching the semantics `run_court`
    itself already applies internally -- never the raw, uncounted opinion.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    verdict: CourtVerdict
    clause_review: ReviewOutput | None
    evidence_review: ReviewOutput | None


async def _run_court_events(
    clause_slice: ClauseSlice,
    evidence_slice: EvidenceSlice,
    *,
    known_anchor_ids: frozenset[str],
    clause_model: str | BaseLlm,
    evidence_model: str | BaseLlm,
    bench: AdjudicationBench | None,
    contested_citation_grounder: ContestedCitationGrounder | None,
    session_service: BaseSessionService | None,
    ledger: Ledger | None,
) -> tuple[CourtVerdict, list[Event]]:
    """Run the court graph end-to-end for one ground, returning both its
    final verdict and the full raw event list -- the shared implementation
    behind :func:`run_court` and :func:`run_court_verbose`, so the two never
    drift on how the graph is actually driven."""
    bench = bench or AdjudicationBench.default()
    tier = bench.tier()
    adjudicator_model: str | BaseLlm | None = tier.model if tier is not None else None

    workflow = build_court_workflow(
        clause_slice,
        evidence_slice,
        known_anchor_ids=known_anchor_ids,
        adjudicator_model=adjudicator_model,
        clause_model=clause_model,
        evidence_model=evidence_model,
        contested_citation_grounder=contested_citation_grounder,
    )
    runner = Runner(
        node=workflow,
        app_name="setback-court",
        session_service=session_service or InMemorySessionService(),
        auto_create_session=True,
    )

    events: list[Event] = []
    try:
        async for event in runner.run_async(
            user_id="setback-tribunal",
            session_id=f"court-{clause_slice.ground_id}",
            new_message=types.Content(role="user", parts=[types.Part(text="review this ground")]),
        ):
            events.append(event)
    except Exception:
        if adjudicator_model is not None:
            bench.record_failure()
        raise

    if adjudicator_model is not None and any(
        node_name_for_event(e) == ADJUDICATOR_NODE for e in events
    ):
        bench.record_success()

    terminal_events = [e for e in events if node_name_for_event(e) in TERMINAL_NODES]
    if len(terminal_events) != 1:
        raise RuntimeError(
            f"expected exactly one terminal court event, got {len(terminal_events)}: "
            f"{[node_name_for_event(e) for e in events]}"
        )
    verdict = CourtVerdict.model_validate(terminal_events[0].output)

    if ledger is not None:
        _book_stage_usage(
            ledger,
            events,
            clause_slice=clause_slice,
            evidence_slice=evidence_slice,
            clause_model=clause_model,
            evidence_model=evidence_model,
            adjudicator_model=adjudicator_model,
        )

    return verdict, events


def _extract_reviews(events: Sequence[Event]) -> tuple[ReviewOutput | None, ReviewOutput | None]:
    """Pull the (possibly voided-to-`None`) Clause/Evidence reviewer
    opinions straight off the tally node's `FunctionNode` event.

    Safe to read directly off `.output`: only `LlmAgent` events have their
    `.output` cleared by the ADK `Runner` (see the module docstring); the
    tally node is a plain `FunctionNode`, so its payload -- built in
    `_make_tally_node` from the already-`void_if_uncited`-filtered opinions
    -- is exactly what a caller needs, with no re-parsing of model text.
    """
    for event in events:
        if node_name_for_event(event) == TALLY_NODE and isinstance(event.output, dict):
            clause_raw = event.output.get("clause")
            evidence_raw = event.output.get("evidence")
            clause = ReviewOutput.model_validate(clause_raw) if clause_raw is not None else None
            evidence = (
                ReviewOutput.model_validate(evidence_raw) if evidence_raw is not None else None
            )
            return clause, evidence
    return None, None


async def run_court(
    clause_slice: ClauseSlice,
    evidence_slice: EvidenceSlice,
    *,
    known_anchor_ids: frozenset[str],
    clause_model: str | BaseLlm = config.INTERVIEW.model,
    evidence_model: str | BaseLlm = config.INTERVIEW.model,
    bench: AdjudicationBench | None = None,
    contested_citation_grounder: ContestedCitationGrounder | None = None,
    session_service: BaseSessionService | None = None,
    ledger: Ledger | None = None,
) -> CourtVerdict:
    """Run the court graph end-to-end for one ground and return its verdict.

    Args:
        clause_slice: The Clause Reviewer's input for this ground.
        evidence_slice: The Evidence Reviewer's input for this ground.
        known_anchor_ids: The case's citation manifest.
        clause_model: The Clause Reviewer's model (string id or `BaseLlm` fake).
        evidence_model: The Evidence Reviewer's model (string id or `BaseLlm` fake).
        bench: The adjudicator's `AdjudicationBench`. Pass the same instance
            back in across grounds/cases to preserve degrade-not-halt state;
            defaults to a fresh, closed bench.
        contested_citation_grounder: The second-pass grounding port, or
            `None` to skip that check.
        session_service: Injectable ADK session service; defaults to a
            fresh in-memory one (fine for a single one-shot run).
        ledger: When given, every stage this run actually executes
            (clause/evidence reviewers, and the adjudicator on a SPLIT ground)
            has its token usage extracted from the run's own ADK event
            stream and booked against it -- see the module docstring's
            ledger-truth note. `None` (the default) preserves the prior,
            unledgered behaviour exactly.

    Returns:
        The graph's single `CourtVerdict` for this ground.

    Raises:
        RuntimeError: The graph did not produce exactly one terminal event
            (a bug in the graph construction, not a normal outcome).
        setback.state.ledger.BudgetExceededError: `ledger` was given and
            booking a stage's usage would exceed its ceiling.
    """
    verdict, _events = await _run_court_events(
        clause_slice,
        evidence_slice,
        known_anchor_ids=known_anchor_ids,
        clause_model=clause_model,
        evidence_model=evidence_model,
        bench=bench,
        contested_citation_grounder=contested_citation_grounder,
        session_service=session_service,
        ledger=ledger,
    )
    return verdict


async def run_court_verbose(
    clause_slice: ClauseSlice,
    evidence_slice: EvidenceSlice,
    *,
    known_anchor_ids: frozenset[str],
    clause_model: str | BaseLlm = config.INTERVIEW.model,
    evidence_model: str | BaseLlm = config.INTERVIEW.model,
    bench: AdjudicationBench | None = None,
    contested_citation_grounder: ContestedCitationGrounder | None = None,
    session_service: BaseSessionService | None = None,
    ledger: Ledger | None = None,
) -> CourtRunResult:
    """Run the court graph exactly like :func:`run_court`, but also return
    the two reviewers' raw opinions -- for a caller (the tribunal job) that
    needs to show both reviewer opinions to the resident, not just the
    final decision.

    Same arguments (including `ledger` -- see `run_court`'s docstring for
    the ledger-truth behaviour), same graph execution, same failure/breaker
    semantics as `run_court` (both share `_run_court_events`); this is
    purely an additive view over the same run, not a second execution.
    """
    verdict, events = await _run_court_events(
        clause_slice,
        evidence_slice,
        known_anchor_ids=known_anchor_ids,
        clause_model=clause_model,
        evidence_model=evidence_model,
        bench=bench,
        contested_citation_grounder=contested_citation_grounder,
        session_service=session_service,
        ledger=ledger,
    )
    clause_review, evidence_review = _extract_reviews(events)
    return CourtRunResult(
        verdict=verdict, clause_review=clause_review, evidence_review=evidence_review
    )
