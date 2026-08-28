"""Client for the NSW ePlanning spatial services (address -> propId -> layers).

Resolves the planning controls governing a site -- zone code and name,
height-of-buildings limit, floor space ratio, lot size, and any heritage
flags -- by walking the ePlanning spatial API's three-step chain: an address
lookup for the property id, a layer intersection for the LEP development
standards, and a lookup of the applicable Development Control Plans. Every
resolved value carries the source LEP name and its legislation.nsw.gov.au
URL, so a caller can cite it directly. The API is keyless and public; see
``docs/data-sources.md`` for the exact request/response shapes this client
implements.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import httpx

ADDRESS_URL: Final[str] = "https://api.apps1.nsw.gov.au/planning/viewersf/V1/ePlanningApi/address"
LAYERINTERSECT_URL: Final[str] = (
    "https://api.apps1.nsw.gov.au/planning/viewersf/V1/ePlanningApi/layerintersect"
)
DCP_URL: Final[str] = "https://api.apps1.nsw.gov.au/planning/viewersf/V1/ePlanningApi/dcp"

_REQUEST_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(10.0, connect=5.0)
_TRANSIENT_STATUS_CODES: Final[frozenset[int]] = frozenset({500, 502, 503, 504})

_ZONING_LAYER: Final[str] = "Land Zoning Map"
_HEIGHT_LAYER: Final[str] = "Height of Buildings Map"
_FSR_LAYER: Final[str] = "Floor Space Ratio Map"
_LOT_SIZE_LAYER: Final[str] = "Lot Size Map"
_HERITAGE_LAYERS: Final[frozenset[str]] = frozenset(
    {"Heritage Map", "Heritage Conservation Area Map"}
)


class SpatialApiError(RuntimeError):
    """Raised when an ePlanning spatial API call fails or returns an unusable payload."""


class AddressNotFoundError(SpatialApiError):
    """Raised when the address lookup matches no property."""


class PlanningControlNotFoundError(SpatialApiError):
    """Raised when a required planning-control layer (e.g. zoning) is absent
    from the layerintersect response."""


@dataclass(frozen=True, slots=True)
class SourcedValue[T]:
    """A value paired with the LEP it was read from, for direct citation."""

    value: T
    lep_name: str
    legislation_url: str


@dataclass(frozen=True, slots=True)
class PlanningControls:
    """The zoning and development-standard layers intersecting a single lot."""

    prop_id: int
    zone_code: SourcedValue[str]
    zone_name: SourcedValue[str]
    height_limit_metres: SourcedValue[float] | None
    floor_space_ratio: SourcedValue[float] | None
    lot_size_sqm: SourcedValue[float] | None
    heritage_flags: tuple[SourcedValue[str], ...]


@dataclass(frozen=True, slots=True)
class DcpDocument:
    """One Development Control Plan document applicable to a property."""

    plan_name: str
    plan_url: str


async def _get_with_retry(
    client: httpx.AsyncClient, url: str, *, params: dict[str, Any]
) -> httpx.Response:
    """GET `url`, retrying exactly once more on a transient failure (a
    connection/timeout error, or a 500/502/503/504 response)."""
    last_exc: Exception | None = None
    for _attempt in range(2):
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in _TRANSIENT_STATUS_CODES:
                raise SpatialApiError(
                    f"ePlanning spatial API returned {exc.response.status_code} for {url}"
                ) from exc
            last_exc = exc
        except httpx.TransportError as exc:
            last_exc = exc
    raise SpatialApiError(
        f"ePlanning spatial API request to {url} did not succeed after retry"
    ) from last_exc


def _parse_prop_id(raw: object) -> int:
    if not isinstance(raw, list) or not raw or not isinstance(raw[0], dict):
        raise AddressNotFoundError("address lookup matched no property")
    prop_id = raw[0].get("propId")
    if not isinstance(prop_id, int):
        raise SpatialApiError("address lookup response is missing an integer propId")
    return prop_id


def _first_result(layer: dict[str, Any]) -> dict[str, Any] | None:
    results = layer.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        return results[0]
    return None


def _sourced_str(result: dict[str, Any], field: str) -> SourcedValue[str] | None:
    value = result.get(field)
    if value is None:
        return None
    return SourcedValue(
        value=str(value),
        lep_name=str(result.get("EPI Name", "")),
        legislation_url=str(result.get("legislationUrl", "")),
    )


def _sourced_float(result: dict[str, Any], field: str) -> SourcedValue[float] | None:
    value = result.get(field)
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return SourcedValue(
        value=parsed,
        lep_name=str(result.get("EPI Name", "")),
        legislation_url=str(result.get("legislationUrl", "")),
    )


def _parse_planning_controls(prop_id: int, raw: object) -> PlanningControls:
    """Extract the planning controls this client cares about from a raw
    layerintersect response.

    Raises:
        PlanningControlNotFoundError: The zoning layer is absent or unusable.
    """
    if not isinstance(raw, list):
        raise SpatialApiError("layerintersect response was not a JSON array")

    layers: dict[str, dict[str, Any]] = {}
    for layer in raw:
        if not isinstance(layer, dict):
            continue
        layer_name = layer.get("layerName")
        result = _first_result(layer)
        if isinstance(layer_name, str) and result is not None:
            layers[layer_name] = result

    zoning = layers.get(_ZONING_LAYER)
    zone_code = _sourced_str(zoning, "Zone") if zoning else None
    zone_name = _sourced_str(zoning, "Land Use") if zoning else None
    if zone_code is None or zone_name is None:
        raise PlanningControlNotFoundError(
            f"no usable {_ZONING_LAYER!r} layer in layerintersect response"
        )

    height = layers.get(_HEIGHT_LAYER)
    fsr = layers.get(_FSR_LAYER)
    lot_size = layers.get(_LOT_SIZE_LAYER)

    heritage_flags: list[SourcedValue[str]] = []
    for layer_name in _HERITAGE_LAYERS:
        heritage_result = layers.get(layer_name)
        if heritage_result is None:
            continue
        sourced = _sourced_str(heritage_result, "title")
        if sourced is not None:
            heritage_flags.append(sourced)

    return PlanningControls(
        prop_id=prop_id,
        zone_code=zone_code,
        zone_name=zone_name,
        height_limit_metres=_sourced_float(height, "Maximum Building Height") if height else None,
        floor_space_ratio=_sourced_float(fsr, "Floor Space Ratio") if fsr else None,
        lot_size_sqm=_sourced_float(lot_size, "Lot Size") if lot_size else None,
        heritage_flags=tuple(heritage_flags),
    )


def _parse_dcp_documents(raw: object) -> list[DcpDocument]:
    if not isinstance(raw, list) or not raw or not isinstance(raw[0], dict):
        return []
    dcp_results = raw[0].get("dcpResults", [])
    if not isinstance(dcp_results, list):
        return []
    return [
        DcpDocument(plan_name=str(plan["planName"]), plan_url=str(plan["planURL"]))
        for plan in dcp_results
        if isinstance(plan, dict)
    ]


async def resolve_property_id(address: str, *, client: httpx.AsyncClient | None = None) -> int:
    """Resolve a street address to its ePlanning property id (``propId``).

    Raises:
        AddressNotFoundError: The address matches no property.
        SpatialApiError: The request failed (after one retry on a transient
            failure) or returned an unparseable payload.
    """
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=_REQUEST_TIMEOUT)
    try:
        response = await _get_with_retry(http_client, ADDRESS_URL, params={"a": address})
        try:
            raw = response.json()
        except ValueError as exc:
            raise SpatialApiError("address lookup response was not valid JSON") from exc
        return _parse_prop_id(raw)
    finally:
        if owns_client:
            await http_client.aclose()


async def fetch_planning_controls(
    prop_id: int, *, client: httpx.AsyncClient | None = None
) -> PlanningControls:
    """Fetch and parse the LEP planning controls intersecting `prop_id`.

    Raises:
        PlanningControlNotFoundError: The zoning layer is absent or unusable.
        SpatialApiError: The request failed (after one retry on a transient
            failure) or returned an unparseable payload.
    """
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=_REQUEST_TIMEOUT)
    try:
        response = await _get_with_retry(
            http_client,
            LAYERINTERSECT_URL,
            params={"type": "property", "id": prop_id, "layers": "epi"},
        )
        try:
            raw = response.json()
        except ValueError as exc:
            raise SpatialApiError("layerintersect response was not valid JSON") from exc
        return _parse_planning_controls(prop_id, raw)
    finally:
        if owns_client:
            await http_client.aclose()


async def fetch_dcp_documents(
    prop_id: int, *, client: httpx.AsyncClient | None = None
) -> list[DcpDocument]:
    """Fetch the Development Control Plan documents applicable to `prop_id`.

    Raises:
        SpatialApiError: The request failed (after one retry on a transient
            failure) or returned an unparseable payload.
    """
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=_REQUEST_TIMEOUT)
    try:
        response = await _get_with_retry(
            http_client, DCP_URL, params={"id": prop_id, "Type": "property"}
        )
        try:
            raw = response.json()
        except ValueError as exc:
            raise SpatialApiError("dcp response was not valid JSON") from exc
        return _parse_dcp_documents(raw)
    finally:
        if owns_client:
            await http_client.aclose()


async def resolve_site(
    address: str, *, client: httpx.AsyncClient | None = None
) -> tuple[PlanningControls, list[DcpDocument]]:
    """Run the full address -> propId -> layerintersect chain plus applicable DCPs.

    Reuses a single HTTP client across all three requests when the caller
    injects one, or owns one for the duration of the whole chain otherwise.
    """
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=_REQUEST_TIMEOUT)
    try:
        prop_id = await resolve_property_id(address, client=http_client)
        controls = await fetch_planning_controls(prop_id, client=http_client)
        dcp_documents = await fetch_dcp_documents(prop_id, client=http_client)
        return controls, dcp_documents
    finally:
        if owns_client:
            await http_client.aclose()
