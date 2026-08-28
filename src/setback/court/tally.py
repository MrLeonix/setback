"""Adjudication tally: combines disjoint reviewer verdicts into a decision.

A candidate ground survives only when both reviewers support it, or when a
tie-break adjudication step explicitly resolves a split verdict in its favour.
"""

from __future__ import annotations

from setback.court.roles import ReviewVerdict


def tally_verdicts(verdicts: list[ReviewVerdict]) -> bool:
    """Decide whether a candidate ground survives adjudication.

    Args:
        verdicts: The verdicts from each disjoint reviewer for one ground.

    Returns:
        True if the ground survives and may proceed toward the gate.

    Raises:
        NotImplementedError: Adjudication is not yet implemented.
    """
    raise NotImplementedError
