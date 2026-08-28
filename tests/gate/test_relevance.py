"""Tests for setback.gate.relevance: s4.15(1) category classification."""

from __future__ import annotations

from setback.gate.relevance import classify_relevance

from setback.gate.s415 import ACT_CITATION, NON_PLANNING_GROUNDS, PLANNING_HEADS


def test_classifies_every_planning_head_as_relevant() -> None:
    for category, expected in PLANNING_HEADS.items():
        ruling = classify_relevance(category)

        assert ruling.relevant is True
        assert ruling.category == category
        assert ruling.statutory_basis == expected.statutory_basis
        assert ruling.explanation == expected.explanation


def test_classifies_every_non_planning_ground_as_irrelevant() -> None:
    for category, expected in NON_PLANNING_GROUNDS.items():
        ruling = classify_relevance(category)

        assert ruling.relevant is False
        assert ruling.category == category
        assert ruling.explanation == expected.explanation


def test_property_value_is_refused_with_the_right_explanation() -> None:
    ruling = classify_relevance("property_value")

    assert ruling.relevant is False
    assert "property value" in ruling.explanation.lower()
    assert "s4.15(1)" in ruling.statutory_basis


def test_view_loss_explanation_carries_the_control_hook_nuance() -> None:
    ruling = classify_relevance("private_view_loss")

    assert ruling.relevant is False
    assert "control" in ruling.explanation.lower()


def test_unknown_category_is_conservatively_refused_as_irrelevant() -> None:
    ruling = classify_relevance("something_nobody_registered")

    assert ruling.relevant is False
    assert ruling.category == "something_nobody_registered"
    assert ACT_CITATION in ruling.statutory_basis
    assert ruling.explanation


def test_planning_heads_and_non_planning_grounds_do_not_collide() -> None:
    assert PLANNING_HEADS.keys().isdisjoint(NON_PLANNING_GROUNDS.keys())
