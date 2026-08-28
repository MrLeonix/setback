"""Structural disjointness test for the court reviewers' input slices.

ARCHITECTURE.md §2 ("Structural disjointness — not just prompted,
enforced"): a `ClauseSlice` must be structurally incapable of carrying an
image part, and an `EvidenceSlice` must be structurally incapable of
carrying legislation/clause text. This is checked here by serializing each
slice type to the exact `genai.types.Content` parts list the model would
receive (`setback.court.roles.clause_slice_to_content` /
`evidence_slice_to_content`) and asserting the forbidden shape never
appears — not by trusting a system-prompt instruction.
"""

from __future__ import annotations

import re

from setback.court.roles import (
    ClauseSlice,
    ClauseText,
    EvidencePhoto,
    EvidencePlan,
    EvidenceSlice,
    ZoningControl,
    clause_slice_to_content,
    evidence_slice_to_content,
)
from setback.evidence.dossier import ProvenanceGrade

_CLAUSE_NUMBER_PATTERN = re.compile(r"s\d+\.\d+|cl\.\s?\d+")

# A handful of fixture cases spanning different shapes (empty, single,
# multiple clauses/anchors) to exercise the serializer, not just one happy path.
_CLAUSE_FIXTURES = [
    ClauseSlice(ground_id="g-empty", ground_text="No clauses cited.", category="public_interest"),
    ClauseSlice(
        ground_id="g-one-clause",
        ground_text="The dwelling exceeds the 9m height limit.",
        category="epi_dcp_provisions",
        clauses=(ClauseText(clause_ref="LEP cl. 4.3", text="Maximum building height is 9m."),),
        controls=(ZoningControl(name="height_limit_m", value="9"),),
    ),
    ClauseSlice(
        ground_id="g-many-clauses",
        ground_text="Multiple DCP provisions are breached.",
        category="site_suitability",
        clauses=(
            ClauseText(clause_ref="DCP s2.4.1", text="Setback from the boundary must be 1.5m."),
            ClauseText(clause_ref="LEP cl. 6.2", text="Foreshore building line applies."),
        ),
        controls=(
            ZoningControl(name="fsr", value="0.5:1"),
            ZoningControl(name="lot_size_m2", value="450"),
        ),
    ),
]

_EVIDENCE_FIXTURES = [
    EvidenceSlice(ground_id="g-empty", ground_text="No photos or plans cited."),
    EvidenceSlice(
        ground_id="g-one-photo",
        ground_text="The roofline visibly overshadows the neighbour's solar panels.",
        photos=(
            EvidencePhoto(
                anchor_id="anchor-1",
                caption="North-facing solar panels in deep shadow at 11am.",
                source_ref="resident-upload-1.jpg",
                grade=ProvenanceGrade.RESIDENT_PHOTO,
            ),
        ),
    ),
    EvidenceSlice(
        ground_id="g-photo-and-plan",
        ground_text="The ridge height annotation on the plans matches the shadow observed.",
        photos=(
            EvidencePhoto(
                anchor_id="anchor-2",
                caption="Ridge line visibly above the neighbouring roofline.",
                source_ref="resident-upload-2.jpg",
                grade=ProvenanceGrade.STREET_VIEW_SOLAR_FALLBACK,
            ),
        ),
        plans=(
            EvidencePlan(
                anchor_id="anchor-3",
                caption="North elevation showing a 9.7m ridge height annotation.",
                source_ref="elevations.pdf#page=1",
            ),
        ),
    ),
]


def test_clause_slice_never_serializes_an_image_part() -> None:
    for slice_ in _CLAUSE_FIXTURES:
        content = clause_slice_to_content(slice_)
        assert content.parts, f"{slice_.ground_id}: expected at least one part"
        for part in content.parts:
            assert part.inline_data is None, (
                f"{slice_.ground_id}: found inline_data in a ClauseSlice"
            )
            assert part.file_data is None, f"{slice_.ground_id}: found file_data in a ClauseSlice"


def test_clause_slice_has_no_field_capable_of_holding_binary_image_data() -> None:
    """Belt-and-braces structural check independent of the serializer: walk
    every field type reachable from `ClauseSlice` and confirm none is
    `bytes` (the type an inline image part would require)."""
    for model in (ClauseSlice, ClauseText, ZoningControl):
        for field in model.model_fields.values():
            assert field.annotation is not bytes, f"{model.__name__} has a bytes field"


def test_evidence_slice_never_serializes_clause_number_text() -> None:
    for slice_ in _EVIDENCE_FIXTURES:
        content = evidence_slice_to_content(slice_)
        assert content.parts, f"{slice_.ground_id}: expected at least one part"
        joined_text = "".join(part.text or "" for part in content.parts)
        match = _CLAUSE_NUMBER_PATTERN.search(joined_text)
        matched_text = match.group() if match else ""
        assert match is None, (
            f"{slice_.ground_id}: EvidenceSlice serialization contained clause-number-shaped "
            f"text {matched_text!r}: {joined_text!r}"
        )
