"""Deterministic citation gate and s4.15 relevance refusal.

Runs after adjudication and before dispatch. This is the product's hard
refusal layer: fully deterministic, no model calls anywhere in the decision
path. Every candidate ground is checked in two stages:

1. **Relevance** (:mod:`setback.gate.relevance`): is the ground's category
   one of the five s4.15(1) heads of consideration? Non-planning grounds
   (property value, bare view loss, commercial competition, the applicant's
   personal circumstances, unanchored "neighbourhood character") are refused
   immediately, before any citation is even looked at.
2. **Citations**: for a planning-relevant ground, every cited anchor must
   resolve against the case dossier — the document id must exist, the page
   must be in range, an optional bounding box must sit within the page's
   bounds, and an optional quoted planning-control value must match the
   control actually stored on the case. A ground with no citations at all
   fails this stage too: an uncited planning-relevant ground is still
   unsubstantiated.

Citation failures are refused, not silently dropped, and use a
:class:`~setback.state.breakers.CircuitBreaker` (reused as-is, not
reimplemented) to cap how many times a ground can be requeued for a fix:
two failed attempts refuse the ground as unsubstantiated (the breaker stays
closed below its default `failure_threshold=3`, signalling "try again");
the third consecutive failure trips the breaker and the ground is flagged
for human review instead of refused again. Passing the same
:class:`CircuitBreaker` instance back in on a retry is how a caller opts
into this — a fresh call with no breaker (or a fresh one) never escalates
past refusal, which is the correct behaviour for a single one-shot check.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from setback.gate.relevance import classify_relevance
from setback.state.breakers import CircuitBreaker

# --- case dossier: documents and planning controls the gate checks against --


@dataclass(frozen=True)
class PageBounds:
    """The physical size of every page in a document, in PDF points."""

    width: float
    height: float


@dataclass(frozen=True)
class BoundingBox:
    """A rectangular region on a page, in PDF points, origin bottom-left."""

    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True)
class CaseDocument:
    """One document in the case dossier manifest."""

    document_id: str
    page_count: int
    page_bounds: PageBounds


@dataclass(frozen=True)
class PlanningControl:
    """A single planning control value resolved for the case (e.g. height limit)."""

    name: str
    value: str


@dataclass(frozen=True)
class CaseDossier:
    """The case dossier manifest and spatial controls a ground's citations are
    checked against.

    This is the gate's own view of the case, not a shared model — the gate is a
    read-only, deterministic consumer, and defining this here (rather than
    importing a shape from another package's lane) keeps it independently
    testable. Whatever package assembles the real case state is responsible for
    mapping into this shape.
    """

    documents: Mapping[str, CaseDocument]
    controls: Mapping[str, PlanningControl]


# --- candidate ground: what the gate is asked to validate --------------------


@dataclass(frozen=True)
class Citation:
    """One cited anchor backing a candidate ground.

    `control_name`/`quoted_value` are both set only when the ground text quotes
    a planning-control value (e.g. "the 9m height limit"); leave both `None`
    for a citation that just points at supporting text with no quoted value.
    """

    document_id: str
    page: int
    bbox: BoundingBox | None = None
    control_name: str | None = None
    quoted_value: str | None = None


@dataclass(frozen=True)
class CandidateGround:
    """A single candidate objection ground awaiting the gate's decision."""

    ground_id: str
    category: str
    citations: tuple[Citation, ...]


# --- decision -----------------------------------------------------------------


class GateStatus(StrEnum):
    """The gate's decision for one candidate ground."""

    SHIPPED = "shipped"
    """Relevant and every citation resolved: ready for dispatch."""

    REFUSED_IRRELEVANT = "refused-irrelevant"
    """Not a s4.15(1) matter: refused before citations were even checked."""

    REFUSED_UNSUBSTANTIATED = "refused-unsubstantiated"
    """Relevant, but at least one citation failed to resolve (attempt 1 or 2)."""

    FLAGGED = "flagged"
    """Relevant, but citations have now failed 3 times running: needs a human."""


@dataclass(frozen=True)
class GateDecision:
    """The gate's typed decision for one ground, renderable directly by the
    dispatcher and console."""

    ground_id: str
    status: GateStatus
    category: str
    explanation: str
    statutory_basis: str
    citation_issues: tuple[str, ...]


# --- validation -----------------------------------------------------------------


def _check_citations(ground: CandidateGround, dossier: CaseDossier) -> tuple[str, ...]:
    """Return machine-readable reasons every failing citation produced, or an
    empty tuple if every citation resolves. A ground with no citations fails
    too."""
    if not ground.citations:
        return (f"ground {ground.ground_id!r} has no citations to substantiate it",)

    issues: list[str] = []
    for citation in ground.citations:
        document = dossier.documents.get(citation.document_id)
        if document is None:
            issues.append(
                f"cited document {citation.document_id!r} does not exist in the case dossier"
            )
            continue

        if not 1 <= citation.page <= document.page_count:
            issues.append(
                f"cited page {citation.page} of document {citation.document_id!r} is out of "
                f"range (document has {document.page_count} pages)"
            )

        if citation.bbox is not None:
            bounds = document.page_bounds
            box = citation.bbox
            in_bounds = (
                0.0 <= box.x0 < box.x1 <= bounds.width and 0.0 <= box.y0 < box.y1 <= bounds.height
            )
            if not in_bounds:
                issues.append(
                    f"cited bbox {box!r} on document {citation.document_id!r} page "
                    f"{citation.page} is outside the page bounds {bounds!r}"
                )

        if citation.control_name is not None:
            control = dossier.controls.get(citation.control_name)
            if control is None:
                issues.append(f"cited control {citation.control_name!r} does not exist on the case")
            elif citation.quoted_value != control.value:
                issues.append(
                    f"quoted value {citation.quoted_value!r} for control "
                    f"{citation.control_name!r} does not match the case's actual value "
                    f"{control.value!r}"
                )

    return tuple(issues)


def validate_ground(
    ground: CandidateGround,
    dossier: CaseDossier,
    breaker: CircuitBreaker | None = None,
) -> GateDecision:
    """Deterministically validate a single candidate ground before dispatch.

    Args:
        ground: The candidate objection ground to validate.
        dossier: The case dossier manifest and planning controls to check
            `ground`'s citations against.
        breaker: An optional, caller-owned :class:`CircuitBreaker` tracking
            this ground's consecutive citation failures across requeue
            attempts. Pass the same instance back in on each retry to get
            the requeue-then-flag escalation; omit it (or pass a fresh one
            each call) for a single one-shot check that only ever refuses.

    Returns:
        The gate's decision: shipped, refused for irrelevance, refused for
        unsubstantiated citations, or flagged after repeated failures.
    """
    ruling = classify_relevance(ground.category)
    if not ruling.relevant:
        return GateDecision(
            ground_id=ground.ground_id,
            status=GateStatus.REFUSED_IRRELEVANT,
            category=ruling.category,
            explanation=ruling.explanation,
            statutory_basis=ruling.statutory_basis,
            citation_issues=(),
        )

    issues = _check_citations(ground, dossier)
    if not issues:
        if breaker is not None:
            breaker.record_success()
        return GateDecision(
            ground_id=ground.ground_id,
            status=GateStatus.SHIPPED,
            category=ruling.category,
            explanation=ruling.explanation,
            statutory_basis=ruling.statutory_basis,
            citation_issues=(),
        )

    if breaker is not None:
        breaker.record_failure()
        status = GateStatus.FLAGGED if breaker.is_open else GateStatus.REFUSED_UNSUBSTANTIATED
    else:
        status = GateStatus.REFUSED_UNSUBSTANTIATED

    explanation = (
        "This ground is planning-relevant, but repeated attempts to substantiate its "
        "citations have failed and it has been flagged for human review."
        if status is GateStatus.FLAGGED
        else "This ground is planning-relevant, but one or more of its citations could not "
        "be substantiated against the case dossier."
    )
    return GateDecision(
        ground_id=ground.ground_id,
        status=status,
        category=ruling.category,
        explanation=explanation,
        statutory_basis=ruling.statutory_basis,
        citation_issues=issues,
    )
