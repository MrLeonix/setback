"""The bench: model configuration for the adjudicating reviewer step.

Runs on the bench model (gemini-3.7-flash, LOW thinking) configured in
setback.config, kept distinct from the interview and drafting models.
"""

from __future__ import annotations

from typing import Any


async def adjudicate(ground_id: str, verdicts: list[Any]) -> Any:
    """Run the bench adjudication step for a split verdict.

    Args:
        ground_id: The identifier of the candidate objection ground.
        verdicts: The disjoint reviewers' verdicts to adjudicate between.

    Returns:
        The bench's adjudication decision.

    Raises:
        NotImplementedError: The bench is not yet implemented.
    """
    raise NotImplementedError
