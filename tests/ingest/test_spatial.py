"""Tests for setback.ingest.spatial: the NSW ePlanning spatial services chain
(address -> propId -> layerintersect -> dcp).

Fully offline: every test replays the frozen fixtures under
``tests/fixtures/nsw/`` (or deliberately broken variants) through respx
against the real httpx transport. No network.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from setback.ingest.spatial import (
    ADDRESS_URL,
    DCP_URL,
    LAYERINTERSECT_URL,
    AddressNotFoundError,
    PlanningControlNotFoundError,
    SpatialApiError,
    fetch_dcp_documents,
    fetch_planning_controls,
    resolve_property_id,
    resolve_site,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "nsw"

DEMO_ADDRESS = "65A Vista Street Sans Souci 2219"
PROP_ID = 6038209
LEP_NAME = "Georges River Local Environmental Plan 2021"
LEGISLATION_URL = "https://legislation.nsw.gov.au/view/html/inforce/current/epi-2021-0587"


def _load_json(name: str) -> object:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


# --- resolve_property_id -----------------------------------------------------


@respx.mock
async def test_resolve_property_id_returns_the_demo_prop_id() -> None:
    respx.get(ADDRESS_URL).mock(
        return_value=httpx.Response(200, json=_load_json("address_65a-vista-street.json"))
    )

    prop_id = await resolve_property_id(DEMO_ADDRESS)

    assert prop_id == PROP_ID


@respx.mock
async def test_resolve_property_id_sends_the_address_as_a_query_param() -> None:
    route = respx.get(ADDRESS_URL).mock(
        return_value=httpx.Response(200, json=_load_json("address_65a-vista-street.json"))
    )

    await resolve_property_id(DEMO_ADDRESS)

    assert route.calls.last.request.url.params["a"] == DEMO_ADDRESS


@respx.mock
async def test_resolve_property_id_raises_not_found_on_empty_matches() -> None:
    respx.get(ADDRESS_URL).mock(return_value=httpx.Response(200, json=[]))

    with pytest.raises(AddressNotFoundError):
        await resolve_property_id("nonexistent address")


@respx.mock
async def test_resolve_property_id_retries_once_on_transient_failure() -> None:
    route = respx.get(ADDRESS_URL).mock(
        side_effect=[
            httpx.Response(502, text="bad gateway"),
            httpx.Response(200, json=_load_json("address_65a-vista-street.json")),
        ]
    )

    prop_id = await resolve_property_id(DEMO_ADDRESS)

    assert route.call_count == 2
    assert prop_id == PROP_ID


# --- fetch_planning_controls --------------------------------------------------


@respx.mock
async def test_fetch_planning_controls_parses_zoning_height_fsr_and_lot_size() -> None:
    respx.get(LAYERINTERSECT_URL).mock(
        return_value=httpx.Response(200, json=_load_json(f"layerintersect_propid-{PROP_ID}.json"))
    )

    controls = await fetch_planning_controls(PROP_ID)

    assert controls.prop_id == PROP_ID
    assert controls.zone_code.value == "R2"
    assert controls.zone_code.lep_name == LEP_NAME
    assert controls.zone_code.legislation_url == LEGISLATION_URL
    assert controls.zone_name.value == "Low Density Residential"
    assert controls.height_limit_metres is not None
    assert controls.height_limit_metres.value == 9.0
    assert controls.floor_space_ratio is not None
    assert controls.floor_space_ratio.value == 0.55
    assert controls.lot_size_sqm is not None
    assert controls.lot_size_sqm.value == 700.0
    # The demo site carries no heritage layer.
    assert controls.heritage_flags == ()


@respx.mock
async def test_fetch_planning_controls_raises_on_missing_zoning_layer() -> None:
    respx.get(LAYERINTERSECT_URL).mock(return_value=httpx.Response(200, json=[]))

    with pytest.raises(PlanningControlNotFoundError):
        await fetch_planning_controls(PROP_ID)


@respx.mock
async def test_fetch_planning_controls_captures_heritage_flags_when_present() -> None:
    raw = [
        {
            "id": "19",
            "layerName": "Land Zoning Map",
            "results": [
                {
                    "Zone": "R2",
                    "Land Use": "Low Density Residential",
                    "EPI Name": LEP_NAME,
                    "legislationUrl": LEGISLATION_URL,
                }
            ],
        },
        {
            "id": "99",
            "layerName": "Heritage Map",
            "results": [
                {
                    "title": "Heritage Conservation Area C1",
                    "EPI Name": LEP_NAME,
                    "legislationUrl": LEGISLATION_URL,
                }
            ],
        },
    ]
    respx.get(LAYERINTERSECT_URL).mock(return_value=httpx.Response(200, json=raw))

    controls = await fetch_planning_controls(PROP_ID)

    assert len(controls.heritage_flags) == 1
    assert controls.heritage_flags[0].value == "Heritage Conservation Area C1"


@respx.mock
async def test_fetch_planning_controls_retries_once_on_transient_failure() -> None:
    route = respx.get(LAYERINTERSECT_URL).mock(
        side_effect=[
            httpx.Response(503, text="unavailable"),
            httpx.Response(200, json=_load_json(f"layerintersect_propid-{PROP_ID}.json")),
        ]
    )

    controls = await fetch_planning_controls(PROP_ID)

    assert route.call_count == 2
    assert controls.zone_code.value == "R2"


@respx.mock
async def test_fetch_planning_controls_does_not_retry_on_permanent_failure() -> None:
    route = respx.get(LAYERINTERSECT_URL).mock(return_value=httpx.Response(404, text="not found"))

    with pytest.raises(SpatialApiError):
        await fetch_planning_controls(PROP_ID)

    assert route.call_count == 1


# --- fetch_dcp_documents ------------------------------------------------------


@respx.mock
async def test_fetch_dcp_documents_lists_applicable_plans() -> None:
    respx.get(DCP_URL).mock(
        return_value=httpx.Response(200, json=_load_json(f"dcp_propid-{PROP_ID}.json"))
    )

    documents = await fetch_dcp_documents(PROP_ID)

    assert len(documents) == 5
    plan_names = {doc.plan_name for doc in documents}
    assert "Kogarah DCP 2013" in plan_names
    for doc in documents:
        assert doc.plan_url.startswith("https://")


@respx.mock
async def test_fetch_dcp_documents_returns_empty_list_when_none_applicable() -> None:
    respx.get(DCP_URL).mock(return_value=httpx.Response(200, json=[]))

    documents = await fetch_dcp_documents(PROP_ID)

    assert documents == []


# --- resolve_site (full chain) ------------------------------------------------


@respx.mock
async def test_resolve_site_walks_the_full_chain() -> None:
    respx.get(ADDRESS_URL).mock(
        return_value=httpx.Response(200, json=_load_json("address_65a-vista-street.json"))
    )
    respx.get(LAYERINTERSECT_URL).mock(
        return_value=httpx.Response(200, json=_load_json(f"layerintersect_propid-{PROP_ID}.json"))
    )
    respx.get(DCP_URL).mock(
        return_value=httpx.Response(200, json=_load_json(f"dcp_propid-{PROP_ID}.json"))
    )

    controls, documents = await resolve_site(DEMO_ADDRESS)

    assert controls.prop_id == PROP_ID
    assert controls.zone_code.value == "R2"
    assert len(documents) == 5
