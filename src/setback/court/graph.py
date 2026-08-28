"""The ADK workflow graph wiring the disjoint reviewers and adjudication.

Builds the directed graph of nodes (Clause Reviewer, Evidence Reviewer,
adjudicator) that make up the adversarial review stage of the pipeline.
"""

from __future__ import annotations

from typing import Any


def build_review_workflow() -> Any:
    """Construct the ADK workflow graph for the adversarial review stage.

    Returns:
        The constructed workflow, ready to run against a candidate ground.

    Raises:
        NotImplementedError: The workflow graph is not yet implemented.
    """
    raise NotImplementedError
