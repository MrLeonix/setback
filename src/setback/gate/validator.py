"""Deterministic citation gate and s4.15 relevance refusal.

Runs after adjudication and before dispatch. Every ground must cite a real,
resolvable source anchor to ship; grounds that are not planning-relevant
under EP&A Act s4.15 are refused with an explanation rather than dropped
silently.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GateResult:
    """The outcome of running the citation gate against a candidate ground."""

    ground_id: str
    accepted: bool
    reason: str


def validate_ground(ground_id: str) -> GateResult:
    """Deterministically validate a single candidate ground before dispatch.

    Args:
        ground_id: The identifier of the candidate objection ground.

    Returns:
        The gate's accept/refuse decision and its reason.

    Raises:
        NotImplementedError: The citation gate is not yet implemented.
    """
    raise NotImplementedError
