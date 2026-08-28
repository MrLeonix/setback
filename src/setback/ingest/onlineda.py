"""Client for the NSW OnlineDA (ePlanning) Development Application API.

Fetches Development Application metadata (application/council numbers,
address, lot/DP, description, status, exhibition window, cost) for a given
Planning Portal Application Number and council. The API is keyless and
public, but rejects a bare query string: the filter contract is carried
entirely in three HEADERS (``filters`` JSON, ``PageSize``, ``PageNumber``).
See ``docs/data-sources.md`` for the exact, empirically discovered shape
this client implements.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

import httpx

ONLINEDA_URL: Final[str] = "https://api.apps1.nsw.gov.au/eplanning/data/v0/OnlineDA"

_REQUEST_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(10.0, connect=5.0)
_TRANSIENT_STATUS_CODES: Final[frozenset[int]] = frozenset({500, 502, 503, 504})


class OnlineDAError(RuntimeError):
    """Raised when an OnlineDA request fails or returns an unusable payload."""


class ApplicationNotFoundError(OnlineDAError):
    """Raised when OnlineDA returns zero matching Application records."""


@dataclass(frozen=True, slots=True)
class DevelopmentApplicationRecord:
    """A verified snapshot of a single Development Application from OnlineDA."""

    planning_portal_application_number: str
    council_application_number: str
    council: str
    address: str
    lot_dp: str
    description: str
    status: str
    exhibition_start: date | None
    exhibition_end: date | None
    cost_of_development: float | None


def _build_headers(pan_number: str, council: str) -> dict[str, str]:
    """The three-header filter contract OnlineDA requires in place of a query string."""
    filters = {
        "filters": {
            "ApplicationStatus": [],
            "CouncilName": [council],
            "PlanningPortalApplicationNumber": [pan_number],
        }
    }
    return {
        "filters": json.dumps(filters),
        "PageSize": "10",
        "PageNumber": "1",
    }


async def _get_with_retry(client: httpx.AsyncClient, *, headers: dict[str, str]) -> httpx.Response:
    """GET the OnlineDA endpoint, retrying exactly once more on a transient failure
    (a connection/timeout error, or a 500/502/503/504 response)."""
    last_exc: Exception | None = None
    for _attempt in range(2):
        try:
            response = await client.get(ONLINEDA_URL, headers=headers)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in _TRANSIENT_STATUS_CODES:
                raise OnlineDAError(
                    f"OnlineDA returned {exc.response.status_code}: {exc.response.text}"
                ) from exc
            last_exc = exc
        except httpx.TransportError as exc:
            last_exc = exc
    raise OnlineDAError("OnlineDA request did not succeed after retry") from last_exc


def _lot_dp(location: dict[str, Any]) -> str:
    lots = location.get("Lot", [])
    if not isinstance(lots, list) or not lots or not isinstance(lots[0], dict):
        return ""
    lot = lots[0]
    return f"Lot {lot.get('Lot', '')} {lot.get('PlanLabel', '')}".strip()


def _description(record: dict[str, Any]) -> str:
    development_types = record.get("DevelopmentType", [])
    if not isinstance(development_types, list):
        return ""
    names = [
        str(item["DevelopmentType"])
        for item in development_types
        if isinstance(item, dict) and "DevelopmentType" in item
    ]
    return "; ".join(names)


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    return date.fromisoformat(value[:10])


def _parse_record(raw: object) -> DevelopmentApplicationRecord:
    """Extract and validate the single Application record from a raw OnlineDA payload.

    Raises:
        ApplicationNotFoundError: The response contains no Application records.
        OnlineDAError: The response is missing fields this client relies on.
    """
    if not isinstance(raw, dict):
        raise OnlineDAError("OnlineDA response was not a JSON object")
    applications = raw.get("Application", [])
    if not isinstance(applications, list) or not applications:
        raise ApplicationNotFoundError("OnlineDA response contains no Application records")
    record = applications[0]
    if not isinstance(record, dict):
        raise OnlineDAError("OnlineDA Application record is not an object")

    try:
        locations = record.get("Location", [])
        location = locations[0] if isinstance(locations, list) and locations else {}
        council = record.get("Council", {})
        return DevelopmentApplicationRecord(
            planning_portal_application_number=str(record["PlanningPortalApplicationNumber"]),
            council_application_number=str(record["CouncilApplicationNumber"]),
            council=str(council.get("CouncilName", "")) if isinstance(council, dict) else "",
            address=str(location.get("FullAddress", "")) if isinstance(location, dict) else "",
            lot_dp=_lot_dp(location) if isinstance(location, dict) else "",
            description=_description(record),
            status=str(record.get("ApplicationStatus", "")),
            exhibition_start=_parse_date(record.get("AssessmentExhibitionStartDate")),
            exhibition_end=_parse_date(record.get("AssessmentExhibitionEndDate")),
            cost_of_development=(
                float(record["CostOfDevelopment"])
                if record.get("CostOfDevelopment") is not None
                else None
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise OnlineDAError(
            f"OnlineDA Application record is missing expected fields: {exc}"
        ) from exc


async def fetch_development_application(
    pan_number: str,
    council: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> DevelopmentApplicationRecord:
    """Fetch and parse a single Development Application record from OnlineDA.

    Args:
        pan_number: The Planning Portal Application Number, e.g. "PAN-661190".
        council: The council name as registered with OnlineDA, sent as a
            server-side filter.
        client: An injectable httpx.AsyncClient (tests replay fixtures via
            respx against a default-constructed one). When omitted, a client
            is constructed and closed for the duration of this call.

    Returns:
        The verified DA record.

    Raises:
        ApplicationNotFoundError: OnlineDA returned zero matching records.
        OnlineDAError: The request failed (after one retry on a transient
            failure) or returned an unparseable payload.
    """
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=_REQUEST_TIMEOUT)
    try:
        response = await _get_with_retry(http_client, headers=_build_headers(pan_number, council))
        try:
            raw = response.json()
        except ValueError as exc:
            raise OnlineDAError("OnlineDA response was not valid JSON") from exc
        return _parse_record(raw)
    finally:
        if owns_client:
            await http_client.aclose()
