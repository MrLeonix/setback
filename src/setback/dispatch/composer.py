"""Composes the final submission and the refused-grounds explainer.

Combines the grounds that survived the citation gate into a submission
document, and separately composes a plain-language explanation of any
grounds that were refused and why.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Submission:
    """The composed objection submission, ready for the resident to lodge."""

    body: str
    refused_grounds_explainer: str


def compose_submission(accepted_ground_ids: list[str], refused_ground_ids: list[str]) -> Submission:
    """Compose the submission document and the refusal explainer.

    Args:
        accepted_ground_ids: Grounds that survived the citation gate.
        refused_ground_ids: Grounds that were refused, and why.

    Returns:
        The composed submission.

    Raises:
        NotImplementedError: Submission composition is not yet implemented.
    """
    raise NotImplementedError
