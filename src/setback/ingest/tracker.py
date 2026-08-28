"""Council document tracker client (eTrack / ePathway) for exhibited documents.

Fetches the exhibited plans, statements of environmental effects, and other
documents attached to a Development Application on a council's public tracker.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TrackedDocument:
    """A single document exhibited on a council's public DA tracker."""

    title: str
    url: str


async def fetch_exhibited_documents(da_number: str, council: str) -> list[TrackedDocument]:
    """Fetch the list of exhibited documents for a Development Application.

    Args:
        da_number: The council-assigned DA number.
        council: The council name whose tracker hosts the documents.

    Returns:
        The exhibited documents currently listed for the DA.

    Raises:
        NotImplementedError: Tracker ingestion is not yet implemented.
    """
    raise NotImplementedError
