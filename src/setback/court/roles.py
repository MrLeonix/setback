"""Role definitions for the two structurally disjoint reviewers.

The Clause Reviewer checks a candidate ground against the applicable planning
instrument clauses; the Evidence Reviewer checks it against the evidence
dossier. The two reviewers share no prompt context, by design.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewVerdict:
    """A single reviewer's verdict on a candidate objection ground."""

    reviewer: str
    ground_id: str
    supported: bool
    rationale: str


async def run_clause_reviewer(ground_id: str) -> ReviewVerdict:
    """Review a candidate ground against the applicable planning clauses.

    Args:
        ground_id: The identifier of the candidate objection ground.

    Returns:
        The Clause Reviewer's verdict.

    Raises:
        NotImplementedError: The Clause Reviewer is not yet implemented.
    """
    raise NotImplementedError


async def run_evidence_reviewer(ground_id: str) -> ReviewVerdict:
    """Review a candidate ground against the evidence dossier.

    Args:
        ground_id: The identifier of the candidate objection ground.

    Returns:
        The Evidence Reviewer's verdict.

    Raises:
        NotImplementedError: The Evidence Reviewer is not yet implemented.
    """
    raise NotImplementedError
