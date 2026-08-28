"""Deterministic vote tallying and the citation pre-check.

Two independent nets keep a model's confident-sounding wrong answer out of
the final decision, before adjudication is ever considered:

1. :func:`void_if_uncited` — the citation pre-check. Any reviewer opinion
   citing an anchor id absent from the case's known citation manifest is
   voided outright and is never counted, regardless of how confident or
   well-reasoned its stance reads. A model cannot manufacture a citation
   into existence and have it silently accepted.
2. :func:`tally` — the deterministic route decision. Two *surviving*
   (non-voided) opinions that agree on stance and both clear the confidence
   threshold route ``CLEAR`` (straight to the gate, no adjudicator call).
   Anything else — a voided opinion, a stance disagreement, or low
   confidence on either side — routes ``SPLIT`` (to the adjudication bench,
   or the conservative default if the bench is unavailable).

Both functions are pure and model-free, so the tally is fully unit-testable
without ever touching the ADK graph.
"""

from __future__ import annotations

from enum import StrEnum

from setback.court.roles import ReviewOutput

CONFIDENCE_THRESHOLD = 0.6
"""Below this, an opinion is SPLIT-worthy even if the other reviewer agrees
with its stance -- low confidence on either side is itself a reason to
escalate to adjudication, not just a disagreement between the two."""


class TallyRoute(StrEnum):
    """The route the tally FunctionNode hands back to the graph."""

    CLEAR = "CLEAR"
    """Both reviewers survived voiding, agreed, and were both confident."""

    SPLIT = "SPLIT"
    """Disagreement, low confidence, or a voided opinion: needs adjudication."""


def void_if_uncited(review: ReviewOutput, known_anchor_ids: frozenset[str]) -> ReviewOutput | None:
    """Void `review` if it cites any anchor id absent from `known_anchor_ids`.

    Args:
        review: One reviewer's structured verdict.
        known_anchor_ids: The full set of citation ids (clause refs and/or
            evidence anchor ids) actually present in the case's dossier
            manifest — the ground truth a citation must resolve against.

    Returns:
        `review` unchanged if every citation resolves, or `None` if the
        opinion is voided. A voided opinion is not "counted as against" —
        it is simply absent from the tally, exactly as an unresolved
        citation is never given the benefit of the doubt by the s4.15 gate.
    """
    if any(anchor_id not in known_anchor_ids for anchor_id in review.cited_anchor_ids):
        return None
    return review


def tally(clause: ReviewOutput | None, evidence: ReviewOutput | None) -> TallyRoute:
    """Deterministically route one ground's two (possibly voided) opinions.

    Args:
        clause: The Clause Reviewer's opinion, or `None` if it was voided.
        evidence: The Evidence Reviewer's opinion, or `None` if it was voided.

    Returns:
        `TallyRoute.CLEAR` only when both opinions survived voiding, agree
        on stance, and are both at or above `CONFIDENCE_THRESHOLD`.
        `TallyRoute.SPLIT` otherwise.
    """
    if clause is None or evidence is None:
        return TallyRoute.SPLIT
    if clause.stance != evidence.stance:
        return TallyRoute.SPLIT
    if clause.confidence < CONFIDENCE_THRESHOLD or evidence.confidence < CONFIDENCE_THRESHOLD:
        return TallyRoute.SPLIT
    return TallyRoute.CLEAR
