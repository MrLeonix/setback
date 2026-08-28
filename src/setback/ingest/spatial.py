"""Client for the NSW Planning ePlanning spatial (layerintersect) services.

Resolves zoning, height-of-building, floor-space-ratio, and heritage layers
for a lot by intersecting the NSW Spatial Services ArcGIS endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ZoningIntersection:
    """The zoning and planning-control layers intersecting a single lot."""

    lot_dp: str
    zone_code: str


async def intersect_zoning_layers(lot_dp: str) -> ZoningIntersection:
    """Resolve the planning-control layers intersecting the given lot.

    Args:
        lot_dp: The lot/deposited-plan identifier, e.g. "Lot 4 DP232626".

    Returns:
        The resolved zoning intersection.

    Raises:
        NotImplementedError: Spatial ingestion is not yet implemented.
    """
    raise NotImplementedError
