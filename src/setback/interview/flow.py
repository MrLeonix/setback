"""The Collaborative Partner interview loop.

Interviews the resident about their concerns, prompts for supporting photos,
and captures feedback on drafted grounds before dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InterviewTurn:
    """A single question/answer turn in the resident interview."""

    question: str
    answer: str


async def run_interview() -> list[InterviewTurn]:
    """Run the Collaborative Partner interview loop with the resident.

    Returns:
        The completed interview transcript.

    Raises:
        NotImplementedError: The interview flow is not yet implemented.
    """
    raise NotImplementedError
