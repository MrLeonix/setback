"""Tests for setback.gate.validator: the deterministic s4.15 + citation gate.

Test focus is the refusal paths, per the work package:
  - a property-value ground refused with the right explanation
  - a fabricated document id
  - a real document with an out-of-range page
  - a control-value mismatch
  - a fully valid overshadowing ground that ships
plus the requeue-then-flag breaker semantics on repeated citation failures.
"""

from __future__ import annotations

from setback.gate.validator import (
    BoundingBox,
    CandidateGround,
    CaseDocument,
    CaseDossier,
    Citation,
    GateStatus,
    PageBounds,
    PlanningControl,
    validate_ground,
)
from setback.state.breakers import CircuitBreaker

# --- fixtures shared across tests --------------------------------------------


def _dossier() -> CaseDossier:
    return CaseDossier(
        documents={
            "soee": CaseDocument(
                document_id="soee",
                page_count=42,
                page_bounds=PageBounds(width=595.0, height=842.0),  # A4 points
            ),
            "elevations": CaseDocument(
                document_id="elevations",
                page_count=6,
                page_bounds=PageBounds(width=841.0, height=1189.0),  # A0 points
            ),
        },
        controls={
            "height_of_buildings": PlanningControl(name="height_of_buildings", value="9m"),
            "fsr": PlanningControl(name="fsr", value="0.55:1"),
        },
    )


def _valid_overshadowing_ground() -> CandidateGround:
    return CandidateGround(
        ground_id="ground-overshadowing-1",
        category="environmental_and_social_impacts",
        citations=(
            Citation(
                document_id="elevations",
                page=3,
                bbox=BoundingBox(x0=50.0, y0=60.0, x1=400.0, y1=700.0),
                control_name="height_of_buildings",
                quoted_value="9m",
            ),
        ),
    )


# --- refusal: irrelevant (property value) ------------------------------------


def test_property_value_ground_is_refused_irrelevant_with_explanation() -> None:
    ground = CandidateGround(
        ground_id="ground-property-value",
        category="property_value",
        citations=(),
    )

    decision = validate_ground(ground, _dossier())

    assert decision.status is GateStatus.REFUSED_IRRELEVANT
    assert decision.ground_id == "ground-property-value"
    assert "property value" in decision.explanation.lower()
    assert "s4.15(1)" in decision.statutory_basis
    assert decision.citation_issues == ()


def test_irrelevant_ground_is_refused_even_with_perfect_citations() -> None:
    """Relevance is checked first: a non-planning ground never reaches citation
    checks, even if it happens to cite a real, in-range document."""
    ground = CandidateGround(
        ground_id="ground-property-value-2",
        category="property_value",
        citations=(Citation(document_id="soee", page=1),),
    )

    decision = validate_ground(ground, _dossier())

    assert decision.status is GateStatus.REFUSED_IRRELEVANT


# --- refusal: unsubstantiated citations --------------------------------------


def test_fabricated_document_id_is_refused_unsubstantiated() -> None:
    ground = CandidateGround(
        ground_id="ground-bad-doc",
        category="site_suitability",
        citations=(Citation(document_id="does-not-exist", page=1),),
    )

    decision = validate_ground(ground, _dossier())

    assert decision.status is GateStatus.REFUSED_UNSUBSTANTIATED
    assert any("does-not-exist" in reason for reason in decision.citation_issues)


def test_out_of_range_page_is_refused_unsubstantiated() -> None:
    ground = CandidateGround(
        ground_id="ground-bad-page",
        category="site_suitability",
        citations=(Citation(document_id="elevations", page=99),),
    )

    decision = validate_ground(ground, _dossier())

    assert decision.status is GateStatus.REFUSED_UNSUBSTANTIATED
    assert any("page" in reason.lower() for reason in decision.citation_issues)


def test_bbox_outside_page_bounds_is_refused_unsubstantiated() -> None:
    ground = CandidateGround(
        ground_id="ground-bad-bbox",
        category="site_suitability",
        citations=(
            Citation(
                document_id="elevations",
                page=1,
                bbox=BoundingBox(x0=0.0, y0=0.0, x1=2000.0, y1=2000.0),
            ),
        ),
    )

    decision = validate_ground(ground, _dossier())

    assert decision.status is GateStatus.REFUSED_UNSUBSTANTIATED
    assert any(
        "bbox" in reason.lower() or "bound" in reason.lower() for reason in decision.citation_issues
    )


def test_control_value_mismatch_is_refused_unsubstantiated() -> None:
    ground = CandidateGround(
        ground_id="ground-bad-control",
        category="environmental_and_social_impacts",
        citations=(
            Citation(
                document_id="elevations",
                page=1,
                control_name="height_of_buildings",
                quoted_value="12m",  # the case's actual control is 9m
            ),
        ),
    )

    decision = validate_ground(ground, _dossier())

    assert decision.status is GateStatus.REFUSED_UNSUBSTANTIATED
    assert any("height_of_buildings" in reason for reason in decision.citation_issues)
    assert any("12m" in reason for reason in decision.citation_issues)


def test_unknown_control_name_is_refused_unsubstantiated() -> None:
    ground = CandidateGround(
        ground_id="ground-unknown-control",
        category="site_suitability",
        citations=(
            Citation(document_id="elevations", page=1, control_name="setback", quoted_value="6m"),
        ),
    )

    decision = validate_ground(ground, _dossier())

    assert decision.status is GateStatus.REFUSED_UNSUBSTANTIATED
    assert any("setback" in reason for reason in decision.citation_issues)


def test_ground_with_no_citations_is_refused_unsubstantiated() -> None:
    ground = CandidateGround(
        ground_id="ground-no-citations",
        category="site_suitability",
        citations=(),
    )

    decision = validate_ground(ground, _dossier())

    assert decision.status is GateStatus.REFUSED_UNSUBSTANTIATED
    assert decision.citation_issues


# --- shipping: a fully valid ground -------------------------------------------


def test_fully_valid_overshadowing_ground_ships() -> None:
    decision = validate_ground(_valid_overshadowing_ground(), _dossier())

    assert decision.status is GateStatus.SHIPPED
    assert decision.citation_issues == ()
    assert decision.statutory_basis == (
        "Environmental Planning and Assessment Act 1979 (NSW) s4.15(1)(b)"
    )


def test_ground_with_multiple_citations_all_must_resolve() -> None:
    ground = CandidateGround(
        ground_id="ground-multi",
        category="site_suitability",
        citations=(
            Citation(document_id="soee", page=5),
            Citation(document_id="does-not-exist", page=1),
        ),
    )

    decision = validate_ground(ground, _dossier())

    assert decision.status is GateStatus.REFUSED_UNSUBSTANTIATED
    assert any("does-not-exist" in reason for reason in decision.citation_issues)


# --- breaker semantics: requeue then flag ------------------------------------


def test_repeated_citation_failures_flag_after_two_requeues() -> None:
    ground = CandidateGround(
        ground_id="ground-flaky",
        category="site_suitability",
        citations=(Citation(document_id="does-not-exist", page=1),),
    )
    breaker = CircuitBreaker(name="gate:ground-flaky")

    first = validate_ground(ground, _dossier(), breaker=breaker)
    second = validate_ground(ground, _dossier(), breaker=breaker)
    third = validate_ground(ground, _dossier(), breaker=breaker)

    assert first.status is GateStatus.REFUSED_UNSUBSTANTIATED
    assert second.status is GateStatus.REFUSED_UNSUBSTANTIATED
    assert third.status is GateStatus.FLAGGED
    assert third.citation_issues


def test_a_fix_between_requeues_resets_the_breaker() -> None:
    bad_ground = CandidateGround(
        ground_id="ground-recovers",
        category="site_suitability",
        citations=(Citation(document_id="does-not-exist", page=1),),
    )
    fixed_ground = CandidateGround(
        ground_id="ground-recovers",
        category="site_suitability",
        citations=(Citation(document_id="soee", page=1),),
    )
    breaker = CircuitBreaker(name="gate:ground-recovers")

    validate_ground(bad_ground, _dossier(), breaker=breaker)
    fixed_decision = validate_ground(fixed_ground, _dossier(), breaker=breaker)
    # Same breaker, freshly failing ground again: should count as a first
    # failure again, not the second, because the fix in between succeeded.
    still_bad = validate_ground(bad_ground, _dossier(), breaker=breaker)

    assert fixed_decision.status is GateStatus.SHIPPED
    assert still_bad.status is GateStatus.REFUSED_UNSUBSTANTIATED


def test_without_a_breaker_citation_failures_never_escalate_to_flagged() -> None:
    ground = CandidateGround(
        ground_id="ground-no-breaker",
        category="site_suitability",
        citations=(Citation(document_id="does-not-exist", page=1),),
    )

    for _ in range(5):
        decision = validate_ground(ground, _dossier())
        assert decision.status is GateStatus.REFUSED_UNSUBSTANTIATED
