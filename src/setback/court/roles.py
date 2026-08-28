"""Role definitions for the two structurally disjoint reviewers.

The Clause Reviewer checks a candidate ground against the applicable planning
instrument clauses and zoning controls; the Evidence Reviewer checks it
against the evidence dossier (resident photos, architectural plans, and their
grounding anchors). The two reviewers share no prompt context, by design
(ARCHITECTURE.md §2, D2): each is built from a distinct Pydantic slice type
that is structurally incapable of carrying the other reviewer's material.

* :class:`ClauseSlice` has no field that can hold an image reference — only
  clause text, control values, and the s4.15 category. It is never given a
  photo or a plan.
* :class:`EvidenceSlice` has no field that can hold clause text — only photo
  and plan captions, source references, and provenance grades. It is never
  given legislation text or a clause number.

``tests/test_slice_disjointness.py`` asserts this structurally, by
serializing both slice types to the exact ``genai.types.Content`` parts list
sent to the model and checking neither can produce the other's forbidden
shape — not by trusting a prompt instruction.

This module is a pure builder: it constructs :class:`google.adk.agents.Agent`
nodes and their structured-output schemas. It makes no model calls itself;
:mod:`setback.court.graph` wires the agents it returns into the ADK workflow
and runs them.
"""

from __future__ import annotations

from enum import StrEnum

from google.adk.agents import Agent
from google.adk.models import BaseLlm
from google.genai import types
from google.genai.types import ThinkingLevel
from pydantic import BaseModel, ConfigDict, Field

from setback.evidence.dossier import ProvenanceGrade

# --- shared vocabulary --------------------------------------------------------


class ReviewStance(StrEnum):
    """A reviewer's (or the adjudicator's) position on a candidate ground."""

    SUPPORT = "support"
    """The ground is well-founded and should proceed toward the gate."""

    REJECT = "reject"
    """The ground is not well-founded on the material this reviewer saw."""


# --- Clause Reviewer input: legislation only, never imagery -------------------


class ClauseText(BaseModel):
    """One applicable planning-instrument clause, verbatim or closely paraphrased."""

    model_config = ConfigDict(frozen=True)

    clause_ref: str
    text: str


class ZoningControl(BaseModel):
    """One resolved zoning/development control value (e.g. height limit)."""

    model_config = ConfigDict(frozen=True)

    name: str
    value: str


class ClauseSlice(BaseModel):
    """The Clause Reviewer's entire world: a ground's text, its s4.15 category,
    the applicable clause text, and the site's zoning controls.

    No field here is capable of holding an image, a photo caption, or a plan
    reference — that is the structural half of the disjointness guarantee.
    """

    model_config = ConfigDict(frozen=True)

    ground_id: str
    ground_text: str
    category: str
    clauses: tuple[ClauseText, ...] = ()
    controls: tuple[ZoningControl, ...] = ()


# --- Evidence Reviewer input: photos/plans only, never legislation text -------


class EvidencePhoto(BaseModel):
    """One resident (or fallback-sourced) photo anchor cited as evidence."""

    model_config = ConfigDict(frozen=True)

    anchor_id: str
    caption: str
    source_ref: str
    grade: ProvenanceGrade


class EvidencePlan(BaseModel):
    """One architectural plan/elevation anchor cited as evidence."""

    model_config = ConfigDict(frozen=True)

    anchor_id: str
    caption: str
    source_ref: str


class EvidenceSlice(BaseModel):
    """The Evidence Reviewer's entire world: a ground's text plus the photo
    and plan anchors bearing on it.

    No field here is capable of holding clause text or a clause number — that
    is the structural half of the disjointness guarantee.
    """

    model_config = ConfigDict(frozen=True)

    ground_id: str
    ground_text: str
    photos: tuple[EvidencePhoto, ...] = ()
    plans: tuple[EvidencePlan, ...] = ()


# --- structured outputs --------------------------------------------------------


class ReviewOutput(BaseModel):
    """A single reviewer's structured verdict on one candidate ground."""

    model_config = ConfigDict(frozen=True)

    ground_id: str
    stance: ReviewStance
    confidence: float = Field(ge=0.0, le=1.0)
    cited_anchor_ids: tuple[str, ...] = ()
    """Clause refs (Clause Reviewer) or evidence anchor ids (Evidence
    Reviewer) this verdict relies on. Checked by
    :func:`setback.court.tally.void_if_uncited` against the case's known
    citation manifest before the vote is counted."""
    rationale: str


class AdjudicatorOutput(BaseModel):
    """The adjudicator's structured resolution of a SPLIT ground."""

    model_config = ConfigDict(frozen=True)

    ground_id: str
    stance: ReviewStance
    confidence: float = Field(ge=0.0, le=1.0)
    cited_anchor_ids: tuple[str, ...] = ()
    rationale: str


# --- prompt rendering (also exercised directly by the disjointness test) -----

_CLAUSE_REVIEWER_PREAMBLE = (
    "You are a NSW planning LAW-ONLY reviewer of a resident's development "
    "application objection ground. You may reason ONLY about the ground's "
    "text, its s4.15(1) category, the applicable clause text, and the "
    "zoning controls given below. You have not been given, and must never "
    "assume the existence of, any photo, plan, or other image evidence — "
    "that is entirely out of scope for you; assume any evidence claimed by "
    "the ground exists and is credible, and judge only whether the ground "
    "is legally well-founded on the clause text and controls alone. Cite "
    "every clause_ref or control name you rely on in cited_anchor_ids. "
    "Respond ONLY via the structured schema, with rationale in at most 3 "
    "sentences."
)

_EVIDENCE_REVIEWER_PREAMBLE = (
    "You are an EVIDENCE-ONLY reviewer of a resident's development "
    "application objection ground. You may reason ONLY about whether the "
    "photos and plans listed below are sufficient and credible to support "
    "the claim made in the ground's text. You have not been given, and must "
    "never assume the existence of, any clause number, legislation text, or "
    "planning-instrument reference — that is entirely out of scope for you; "
    "assume the legal characterisation of the ground is correct, and judge "
    "only whether the physical evidence backs it up. Cite every anchor_id "
    "you rely on in cited_anchor_ids. Respond ONLY via the structured "
    "schema, with rationale in at most 3 sentences."
)

_ADJUDICATOR_INSTRUCTION = (
    "Two structurally disjoint reviewers assessed the same development "
    "application objection ground and disagreed, or one of them expressed "
    "low confidence, or one of their citations was voided for referencing "
    "material outside the case dossier. One reviewer judged ONLY legal "
    "clause validity (ignoring evidence quality); the other judged ONLY "
    "evidence quality (ignoring which clause applies). Their structured "
    "verdicts will be given to you as the user turn. Weigh both and decide "
    "the final stance for whether this ground should proceed toward the "
    "s4.15 gate. If you cannot resolve the conflict with genuine "
    "confidence, say so honestly with a low confidence score rather than "
    "guessing — a low-confidence adjudication is treated conservatively "
    "downstream, never guessed past. Respond ONLY via the structured "
    "schema, with rationale in at most 3 sentences."
)


def render_clause_slice(slice_: ClauseSlice) -> str:
    """Render `slice_` as the plain-text user turn sent to the Clause Reviewer."""
    lines = [
        f"GROUND {slice_.ground_id} (s4.15 category: {slice_.category}):",
        slice_.ground_text,
        "",
        "APPLICABLE CLAUSES:",
    ]
    lines.extend(f"- {clause.clause_ref}: {clause.text}" for clause in slice_.clauses)
    if not slice_.clauses:
        lines.append("- (none provided)")
    lines.append("")
    lines.append("ZONING CONTROLS:")
    lines.extend(f"- {control.name} = {control.value}" for control in slice_.controls)
    if not slice_.controls:
        lines.append("- (none provided)")
    return "\n".join(lines)


def render_evidence_slice(slice_: EvidenceSlice) -> str:
    """Render `slice_` as the plain-text user turn sent to the Evidence Reviewer."""
    lines = [f"GROUND {slice_.ground_id}:", slice_.ground_text, "", "PHOTOS:"]
    lines.extend(
        f"- [{photo.anchor_id}] (provenance grade {photo.grade.value}) {photo.caption} "
        f"(source: {photo.source_ref})"
        for photo in slice_.photos
    )
    if not slice_.photos:
        lines.append("- (none provided)")
    lines.append("")
    lines.append("PLANS:")
    lines.extend(
        f"- [{plan.anchor_id}] {plan.caption} (source: {plan.source_ref})" for plan in slice_.plans
    )
    if not slice_.plans:
        lines.append("- (none provided)")
    return "\n".join(lines)


def clause_slice_to_content(slice_: ClauseSlice) -> types.Content:
    """Serialize `slice_` into the exact ``genai`` ``Content`` parts list the
    Clause Reviewer would receive — a single text part, never image data.
    Exercised directly by ``tests/test_slice_disjointness.py``."""
    return types.Content(role="user", parts=[types.Part(text=render_clause_slice(slice_))])


def evidence_slice_to_content(slice_: EvidenceSlice) -> types.Content:
    """Serialize `slice_` into the exact ``genai`` ``Content`` parts list the
    Evidence Reviewer would receive — a single text part, never clause text.
    Exercised directly by ``tests/test_slice_disjointness.py``."""
    return types.Content(role="user", parts=[types.Part(text=render_evidence_slice(slice_))])


# --- agent builders -------------------------------------------------------------


def build_clause_reviewer_agent(
    slice_: ClauseSlice,
    *,
    model: str | BaseLlm,
    thinking_level: ThinkingLevel,
) -> Agent:
    """Build the Clause Reviewer node for one ground's `ClauseSlice`.

    A fresh agent is built per ground because its instruction bakes in that
    ground's slice content directly, following the proven spike construction
    (see ``spike-adkCourt.md``); ADK's `Agent` instruction is static per
    instance, so per-run data is embedded at build time rather than passed
    as a later turn.
    """
    instruction = f"{_CLAUSE_REVIEWER_PREAMBLE}\n\n{render_clause_slice(slice_)}"
    return Agent(
        name="clause_reviewer",
        model=model,
        generate_content_config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_level=thinking_level),
        ),
        instruction=instruction,
        output_schema=ReviewOutput,
    )


def build_evidence_reviewer_agent(
    slice_: EvidenceSlice,
    *,
    model: str | BaseLlm,
    thinking_level: ThinkingLevel,
) -> Agent:
    """Build the Evidence Reviewer node for one ground's `EvidenceSlice`.

    Per the grounding spike, the Evidence Reviewer runs at temperature 0 in
    addition to the shared thinking-level configuration (unlike the Clause
    Reviewer, which has no such requirement).
    """
    instruction = f"{_EVIDENCE_REVIEWER_PREAMBLE}\n\n{render_evidence_slice(slice_)}"
    return Agent(
        name="evidence_reviewer",
        model=model,
        generate_content_config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_level=thinking_level),
            temperature=0.0,
        ),
        instruction=instruction,
        output_schema=ReviewOutput,
    )


def build_adjudicator_agent(*, model: str | BaseLlm, thinking_level: ThinkingLevel) -> Agent:
    """Build the adjudicator node. Its instruction is static — the two
    reviewers' verdicts arrive as its user turn via the graph's tally→SPLIT
    edge, not baked in here."""
    return Agent(
        name="adjudicator",
        model=model,
        generate_content_config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_level=thinking_level),
        ),
        instruction=_ADJUDICATOR_INSTRUCTION,
        output_schema=AdjudicatorOutput,
    )
