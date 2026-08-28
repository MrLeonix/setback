"""s4.15(1) relevance classification for candidate objection grounds.

A thin, fully deterministic lookup over the statutory data in
:mod:`setback.gate.s415`: given the category a candidate ground has been
tagged with, decide whether it is one of the five s4.15(1) heads of
consideration or one of the explicit non-planning grounds, and hand back the
plain-English explanation and statutory basis either way.

An unrecognised category is conservatively treated as irrelevant — this is
the product's hard refusal layer, so an unknown category must never default
to shipping.
"""

from __future__ import annotations

from setback.gate.s415 import ACT_CITATION, NON_PLANNING_GROUNDS, PLANNING_HEADS, RelevanceRuling


def classify_relevance(category: str) -> RelevanceRuling:
    """Classify a candidate ground's category against s4.15(1).

    Args:
        category: The stable category identifier the ground was tagged
            with upstream (e.g. ``"site_suitability"``, ``"property_value"``).

    Returns:
        The matching :class:`~setback.gate.s415.RelevanceRuling` if the
        category is a known s4.15(1) head or a known non-planning ground.
        For any other category, a conservative ``relevant=False`` ruling
        explaining that the category is not a recognised s4.15(1) matter.
    """
    if category in PLANNING_HEADS:
        return PLANNING_HEADS[category]
    if category in NON_PLANNING_GROUNDS:
        return NON_PLANNING_GROUNDS[category]
    return RelevanceRuling(
        category=category,
        relevant=False,
        explanation=(
            f"'{category}' is not a recognised category under s4.15(1) of the "
            f"{ACT_CITATION} — it matches none of the five statutory heads of "
            "consideration and none of the documented non-planning grounds. Treated "
            "conservatively as not planning-relevant."
        ),
        statutory_basis=f"{ACT_CITATION} s4.15(1) (unrecognised category)",
    )
