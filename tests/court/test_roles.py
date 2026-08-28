"""Tests for setback.court.roles: the disjoint slice types, structured
output schemas, prompt rendering, and Agent builders."""

from __future__ import annotations

from google.genai.types import ThinkingLevel

from setback.court.roles import (
    AdjudicatorOutput,
    ClauseSlice,
    ClauseText,
    EvidencePhoto,
    EvidencePlan,
    EvidenceSlice,
    ReviewOutput,
    ReviewStance,
    ZoningControl,
    build_adjudicator_agent,
    build_clause_reviewer_agent,
    build_evidence_reviewer_agent,
    clause_slice_to_content,
    evidence_slice_to_content,
    render_clause_slice,
    render_evidence_slice,
)
from setback.evidence.dossier import ProvenanceGrade

_CLAUSE_SLICE = ClauseSlice(
    ground_id="g1",
    ground_text="The proposed dwelling exceeds the 9m height limit.",
    category="epi_dcp_provisions",
    clauses=(ClauseText(clause_ref="LEP cl. 4.3", text="Maximum building height is 9m."),),
    controls=(ZoningControl(name="height_limit_m", value="9"),),
)

_EVIDENCE_SLICE = EvidenceSlice(
    ground_id="g1",
    ground_text="The proposed dwelling exceeds the 9m height limit.",
    photos=(
        EvidencePhoto(
            anchor_id="anchor-1",
            caption="Ridge line visibly above the neighbouring roofline.",
            source_ref="resident-upload-1.jpg",
            grade=ProvenanceGrade.RESIDENT_PHOTO,
        ),
    ),
    plans=(
        EvidencePlan(
            anchor_id="anchor-2",
            caption="North elevation showing a 9.7m ridge height annotation.",
            source_ref="elevations.pdf#page=1",
        ),
    ),
)


# --- structured output schemas ----------------------------------------------------


def test_review_output_round_trips_via_json() -> None:
    output = ReviewOutput(
        ground_id="g1",
        stance=ReviewStance.SUPPORT,
        confidence=0.8,
        cited_anchor_ids=("anchor-1",),
        rationale="r",
    )

    restored = ReviewOutput.model_validate_json(output.model_dump_json())

    assert restored == output


def test_adjudicator_output_round_trips_via_json() -> None:
    output = AdjudicatorOutput(
        ground_id="g1", stance=ReviewStance.REJECT, confidence=0.5, rationale="r"
    )

    restored = AdjudicatorOutput.model_validate_json(output.model_dump_json())

    assert restored == output


# --- prompt rendering ---------------------------------------------------------


def test_render_clause_slice_includes_clause_and_control_text() -> None:
    text = render_clause_slice(_CLAUSE_SLICE)

    assert "LEP cl. 4.3" in text
    assert "Maximum building height is 9m." in text
    assert "height_limit_m = 9" in text
    assert "epi_dcp_provisions" in text


def test_render_evidence_slice_includes_photo_and_plan_captions() -> None:
    text = render_evidence_slice(_EVIDENCE_SLICE)

    assert "anchor-1" in text
    assert "Ridge line visibly above the neighbouring roofline." in text
    assert "anchor-2" in text
    assert "9.7m ridge height" in text
    assert ProvenanceGrade.RESIDENT_PHOTO.value in text


def test_clause_slice_to_content_is_a_single_text_part() -> None:
    content = clause_slice_to_content(_CLAUSE_SLICE)

    assert content.role == "user"
    assert content.parts is not None
    assert len(content.parts) == 1
    assert content.parts[0].text is not None
    assert content.parts[0].inline_data is None
    assert content.parts[0].file_data is None


def test_evidence_slice_to_content_is_a_single_text_part() -> None:
    content = evidence_slice_to_content(_EVIDENCE_SLICE)

    assert content.role == "user"
    assert content.parts is not None
    assert len(content.parts) == 1
    assert content.parts[0].text is not None


# --- agent builders -------------------------------------------------------------


def test_build_clause_reviewer_agent_is_named_and_schema_bound() -> None:
    agent = build_clause_reviewer_agent(
        _CLAUSE_SLICE, model="gemini-3.5-flash-lite", thinking_level=ThinkingLevel.MINIMAL
    )

    assert agent.name == "clause_reviewer"
    assert agent.output_schema is ReviewOutput
    assert agent.model == "gemini-3.5-flash-lite"
    assert "9m height limit" in agent.instruction or "9m" in agent.instruction


def test_build_evidence_reviewer_agent_sets_temperature_zero() -> None:
    agent = build_evidence_reviewer_agent(
        _EVIDENCE_SLICE, model="gemini-3.5-flash-lite", thinking_level=ThinkingLevel.MINIMAL
    )

    assert agent.name == "evidence_reviewer"
    assert agent.output_schema is ReviewOutput
    assert agent.generate_content_config is not None
    assert agent.generate_content_config.temperature == 0.0


def test_build_adjudicator_agent_is_named_and_schema_bound() -> None:
    agent = build_adjudicator_agent(model="gemini-3.7-flash", thinking_level=ThinkingLevel.LOW)

    assert agent.name == "adjudicator"
    assert agent.output_schema is AdjudicatorOutput
    assert agent.model == "gemini-3.7-flash"


def test_clause_reviewer_and_evidence_reviewer_instructions_share_no_slice_content() -> None:
    """A structural sanity check on top of the disjointness test: the two
    reviewers' baked-in instructions don't quote each other's slice."""
    clause_agent = build_clause_reviewer_agent(
        _CLAUSE_SLICE, model="gemini-3.5-flash-lite", thinking_level=ThinkingLevel.MINIMAL
    )
    evidence_agent = build_evidence_reviewer_agent(
        _EVIDENCE_SLICE, model="gemini-3.5-flash-lite", thinking_level=ThinkingLevel.MINIMAL
    )

    assert "anchor-1" not in clause_agent.instruction
    assert "LEP cl. 4.3" not in evidence_agent.instruction
