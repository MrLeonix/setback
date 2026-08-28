"""Tests for setback.ingest.onlineda: the NSW OnlineDA API client.

Fully offline: every test replays the frozen fixture at
``tests/fixtures/nsw/onlineda_pan-661190.json`` (or a deliberately broken
variant of it) through respx against the real httpx transport. No network.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from setback.ingest.onlineda import (
    ONLINEDA_URL,
    ApplicationNotFoundError,
    OnlineDAError,
    fetch_development_application,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "nsw"

PAN_NUMBER = "PAN-661190"
COUNCIL = "Georges River Council"


def _fixture() -> dict[str, object]:
    raw = json.loads((FIXTURES_DIR / "onlineda_pan-661190.json").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


@respx.mock
async def test_fetch_development_application_parses_the_demo_record() -> None:
    respx.get(ONLINEDA_URL).mock(return_value=httpx.Response(200, json=_fixture()))

    record = await fetch_development_application(PAN_NUMBER, COUNCIL)

    assert record.planning_portal_application_number == "PAN-661190"
    assert record.council_application_number == "DA2026/0359"
    assert record.council == "Georges River Council"
    assert record.address == "65A VISTA STREET SANS SOUCI 2219"
    assert record.lot_dp == "Lot 4 DP232626"
    assert "Dwelling house" in record.description
    assert record.status == "On Exhibition"
    assert record.exhibition_start is not None
    assert record.exhibition_start.isoformat() == "2026-08-20"
    assert record.exhibition_end is not None
    assert record.exhibition_end.isoformat() == "2026-09-03"
    assert record.cost_of_development == 825000.0


@respx.mock
async def test_fetch_development_application_sends_the_documented_header_contract() -> None:
    route = respx.get(ONLINEDA_URL).mock(return_value=httpx.Response(200, json=_fixture()))

    await fetch_development_application(PAN_NUMBER, COUNCIL)

    assert route.call_count == 1
    sent_headers = route.calls.last.request.headers
    assert sent_headers["PageSize"] == "10"
    assert sent_headers["PageNumber"] == "1"
    filters = json.loads(sent_headers["filters"])
    assert filters["filters"]["PlanningPortalApplicationNumber"] == [PAN_NUMBER]
    assert filters["filters"]["CouncilName"] == [COUNCIL]


@respx.mock
async def test_fetch_development_application_raises_not_found_on_empty_result() -> None:
    respx.get(ONLINEDA_URL).mock(
        return_value=httpx.Response(
            200, json={"Application": [], "PageNumber": 1, "PageSize": 10, "TotalCount": 0}
        )
    )

    with pytest.raises(ApplicationNotFoundError):
        await fetch_development_application(PAN_NUMBER, COUNCIL)


@respx.mock
async def test_fetch_development_application_retries_once_on_transient_failure() -> None:
    route = respx.get(ONLINEDA_URL).mock(
        side_effect=[
            httpx.Response(503, json={"ErrorMessage": "temporarily unavailable"}),
            httpx.Response(200, json=_fixture()),
        ]
    )

    record = await fetch_development_application(PAN_NUMBER, COUNCIL)

    assert route.call_count == 2
    assert record.planning_portal_application_number == "PAN-661190"


@respx.mock
async def test_fetch_development_application_does_not_retry_on_permanent_failure() -> None:
    route = respx.get(ONLINEDA_URL).mock(
        return_value=httpx.Response(
            400, json={"ErrorMessage": "Required parameters for OnlineDA endpoint is not met."}
        )
    )

    with pytest.raises(OnlineDAError):
        await fetch_development_application(PAN_NUMBER, COUNCIL)

    assert route.call_count == 1


@respx.mock
async def test_fetch_development_application_gives_up_after_two_transient_failures() -> None:
    route = respx.get(ONLINEDA_URL).mock(
        return_value=httpx.Response(503, json={"ErrorMessage": "temporarily unavailable"})
    )

    with pytest.raises(OnlineDAError):
        await fetch_development_application(PAN_NUMBER, COUNCIL)

    assert route.call_count == 2
