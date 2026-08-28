"""Offline tests for the NSW fixture-parsing helpers in ``tools.fetch_fixtures``.

These tests read the frozen JSON/HTML fixtures under ``tests/fixtures/nsw/``
(produced by a manual ``make fixtures`` run against the live NSW government
APIs) and never touch the network themselves.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.fetch_fixtures import (
    EtrackDocument,
    find_document,
    parse_dcp_plans,
    parse_etrack_documents,
    parse_onlineda_record,
    parse_prop_id,
    parse_zoning_layers,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "nsw"


def _load_json(name: str) -> object:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def test_parse_onlineda_record_extracts_the_demo_application() -> None:
    raw = _load_json("onlineda_pan-661190.json")

    record = parse_onlineda_record(raw)

    assert record["PlanningPortalApplicationNumber"] == "PAN-661190"
    assert record["CouncilApplicationNumber"] == "DA2026/0359"
    assert record["Council"]["CouncilName"] == "Georges River Council"


def test_parse_onlineda_record_rejects_empty_application_list() -> None:
    with pytest.raises(ValueError, match="no Application records"):
        parse_onlineda_record({"Application": []})


def test_parse_prop_id_extracts_the_demo_property() -> None:
    raw = _load_json("address_65a-vista-street.json")

    assert parse_prop_id(raw) == 6038209


def test_parse_prop_id_rejects_no_matches() -> None:
    with pytest.raises(ValueError, match="no matches"):
        parse_prop_id([])


def test_parse_zoning_layers_includes_height_of_buildings() -> None:
    raw = _load_json("layerintersect_propid-6038209.json")

    layers = parse_zoning_layers(raw)

    assert layers["Height of Buildings Map"] == "9 m"
    assert "Scenic Protection Land" in layers


def test_parse_dcp_plans_lists_applicable_plans() -> None:
    raw = _load_json("dcp_propid-6038209.json")

    plans = parse_dcp_plans(raw)

    assert any("Hurstville DCP" in plan for plan in plans)
    assert any("Kogarah DCP" in plan for plan in plans)


def test_parse_dcp_plans_handles_no_property_match() -> None:
    assert parse_dcp_plans([]) == []


def test_parse_etrack_documents_finds_statement_of_environmental_effects() -> None:
    html = (FIXTURES_DIR / "etrack_documents_da2026-0359.html").read_text(encoding="utf-8")

    documents = parse_etrack_documents(html)

    assert len(documents) >= 2
    see = find_document(documents, "Statement of Environmental Effects")
    assert see.ext == "PDF"
    assert see.filesize > 0


def test_parse_etrack_documents_deduplicates_repeated_rows() -> None:
    html = """
    <tr><td><a href="FileDownload.ashx?id=1&amp;ext=PDF&amp;filesize=100">Some Report</a></td></tr>
    <tr><td><a href="FileDownload.ashx?id=1&amp;ext=PDF&amp;filesize=100">Some Report</a></td></tr>
    """

    documents = parse_etrack_documents(html)

    expected = EtrackDocument(
        file_id=1, ext="PDF", filesize=100, description=documents[0].description
    )
    assert documents == [expected]


def test_find_document_raises_when_nothing_matches() -> None:
    with pytest.raises(ValueError, match="no eTrack document matched"):
        find_document([], "Traffic Report")
