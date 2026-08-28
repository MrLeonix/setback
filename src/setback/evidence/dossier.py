"""Evidence dossier: anchors claims to sources and grades their provenance.

Provenance grades:
    A: resident-supplied photo, directly evidencing the claim.
    B: Street View / Solar API fallback, used when no resident photo exists.
    C: documents-only, derived solely from exhibited plans or public records.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ProvenanceGrade(StrEnum):
    """The strength of evidence backing a single claim."""

    RESIDENT_PHOTO = "A"
    STREET_VIEW_SOLAR_FALLBACK = "B"
    DOCUMENTS_ONLY = "C"


@dataclass(frozen=True)
class EvidenceAnchor:
    """A claim tied to its supporting source and provenance grade."""

    claim: str
    source_ref: str
    grade: ProvenanceGrade


def build_dossier(anchors: list[EvidenceAnchor]) -> list[EvidenceAnchor]:
    """Assemble and order an evidence dossier from individual anchors.

    Args:
        anchors: The individual evidence anchors collected during ingestion
            and interview.

    Returns:
        The dossier, ordered by descending provenance grade.

    Raises:
        NotImplementedError: Dossier assembly is not yet implemented.
    """
    raise NotImplementedError
