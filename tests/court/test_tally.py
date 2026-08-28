"""Tests for setback.court.tally: deterministic vote tallying and the
citation pre-check."""

from __future__ import annotations

from setback.court.roles import ReviewOutput, ReviewStance
from setback.court.tally import CONFIDENCE_THRESHOLD, TallyRoute, tally, void_if_uncited


def _review(
    *,
    ground_id: str = "g1",
    stance: ReviewStance = ReviewStance.SUPPORT,
    confidence: float = 0.9,
    cited_anchor_ids: tuple[str, ...] = (),
) -> ReviewOutput:
    return ReviewOutput(
        ground_id=ground_id,
        stance=stance,
        confidence=confidence,
        cited_anchor_ids=cited_anchor_ids,
        rationale="r",
    )


# --- void_if_uncited -----------------------------------------------------------


def test_void_if_uncited_keeps_review_when_every_citation_resolves() -> None:
    review = _review(cited_anchor_ids=("a1", "a2"))

    result = void_if_uncited(review, known_anchor_ids=frozenset({"a1", "a2", "a3"}))

    assert result == review


def test_void_if_uncited_keeps_review_with_no_citations() -> None:
    review = _review(cited_anchor_ids=())

    result = void_if_uncited(review, known_anchor_ids=frozenset())

    assert result == review


def test_void_if_uncited_voids_review_citing_unknown_anchor() -> None:
    review = _review(cited_anchor_ids=("a1", "unknown"))

    result = void_if_uncited(review, known_anchor_ids=frozenset({"a1"}))

    assert result is None


# --- tally -----------------------------------------------------------------------


def test_tally_clear_when_both_agree_and_confident() -> None:
    clause = _review(stance=ReviewStance.SUPPORT, confidence=0.9)
    evidence = _review(stance=ReviewStance.SUPPORT, confidence=0.8)

    assert tally(clause, evidence) is TallyRoute.CLEAR


def test_tally_split_when_stances_disagree() -> None:
    clause = _review(stance=ReviewStance.SUPPORT)
    evidence = _review(stance=ReviewStance.REJECT)

    assert tally(clause, evidence) is TallyRoute.SPLIT


def test_tally_split_when_clause_confidence_below_threshold() -> None:
    clause = _review(stance=ReviewStance.SUPPORT, confidence=CONFIDENCE_THRESHOLD - 0.01)
    evidence = _review(stance=ReviewStance.SUPPORT, confidence=0.99)

    assert tally(clause, evidence) is TallyRoute.SPLIT


def test_tally_split_when_evidence_confidence_below_threshold() -> None:
    clause = _review(stance=ReviewStance.SUPPORT, confidence=0.99)
    evidence = _review(stance=ReviewStance.SUPPORT, confidence=CONFIDENCE_THRESHOLD - 0.01)

    assert tally(clause, evidence) is TallyRoute.SPLIT


def test_tally_clear_at_exactly_the_confidence_threshold() -> None:
    clause = _review(stance=ReviewStance.SUPPORT, confidence=CONFIDENCE_THRESHOLD)
    evidence = _review(stance=ReviewStance.SUPPORT, confidence=CONFIDENCE_THRESHOLD)

    assert tally(clause, evidence) is TallyRoute.CLEAR


def test_tally_split_when_clause_is_voided() -> None:
    evidence = _review(stance=ReviewStance.SUPPORT, confidence=0.9)

    assert tally(None, evidence) is TallyRoute.SPLIT


def test_tally_split_when_evidence_is_voided() -> None:
    clause = _review(stance=ReviewStance.SUPPORT, confidence=0.9)

    assert tally(clause, None) is TallyRoute.SPLIT


def test_tally_split_when_both_voided() -> None:
    assert tally(None, None) is TallyRoute.SPLIT
