"""Client for the NSW OnlineDA (ePlanning) Development Application API.

Fetches DA metadata (applicant, description, exhibition dates, associated
documents) for a given DA number and council. The API is keyless and public.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DevelopmentApplication:
    """A minimal, verified snapshot of a Development Application record."""

    da_number: str
    council: str
    address: str
    lot_dp: str


async def fetch_development_application(da_number: str, council: str) -> DevelopmentApplication:
    """Fetch a Development Application record from the NSW OnlineDA API.

    Args:
        da_number: The council-assigned DA number, e.g. "PAN-661190".
        council: The council name as registered with OnlineDA.

    Returns:
        The verified DA record.

    Raises:
        NotImplementedError: Ingestion is not yet implemented.
    """
    raise NotImplementedError
