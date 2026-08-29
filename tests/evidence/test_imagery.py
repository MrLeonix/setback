"""Tests for setback.evidence.imagery: the Street View fallback.

Fully offline: HTTP calls go through respx against the real httpx
transport; the Secret Manager API key lookup is a fake callable injected as
`secret_accessor` — never a literal key, never a live Secret Manager call.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from setback.evidence.dossier import ProvenanceGrade
from setback.evidence.imagery import (
    STREET_VIEW_IMAGE_URL,
    STREET_VIEW_METADATA_URL,
    StreetViewUnavailableError,
    fetch_street_view_fallback,
)

_FAKE_KEY = "fake-test-key-not-real"  # noqa: S105 - not a real credential, test-only


def _fake_secret_accessor() -> str:
    return _FAKE_KEY


@pytest.mark.asyncio
@respx.mock
async def test_fetch_street_view_fallback_returns_grade_b_with_attribution() -> None:
    respx.get(STREET_VIEW_METADATA_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "OK",
                "pano_id": "s3YYyBVc6ohLRMk2Uh1EJQ",
                "date": "2022-02",
                "copyright": "© Google",
            },
        )
    )
    respx.get(STREET_VIEW_IMAGE_URL).mock(
        return_value=httpx.Response(200, content=b"\xff\xd8\xff-jpeg-bytes")
    )

    async with httpx.AsyncClient() as http_client:
        result = await fetch_street_view_fallback(
            "-33.9966876,151.1248784", client=http_client, secret_accessor=_fake_secret_accessor
        )

    assert result is not None
    assert result.provenance_grade is ProvenanceGrade.STREET_VIEW_SOLAR_FALLBACK
    assert result.image_bytes == b"\xff\xd8\xff-jpeg-bytes"
    assert result.attribution == "(c) Google Street View, 2022-02"
    assert result.metadata.pano_id == "s3YYyBVc6ohLRMk2Uh1EJQ"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_street_view_fallback_accepts_a_free_text_address() -> None:
    """Setback has no geocoding step of its own -- a resolved DA address
    string must be usable directly as `location`, exactly as the Street
    View Static API's own docs promise (it resolves a text address to a
    pano itself); this is the exact call shape `job.pipeline` uses for the
    real trigger (a case's DA record address, not lat/lng)."""
    metadata_route = respx.get(STREET_VIEW_METADATA_URL).mock(
        return_value=httpx.Response(200, json={"status": "OK", "pano_id": "p1", "date": "2024-05"})
    )
    respx.get(STREET_VIEW_IMAGE_URL).mock(return_value=httpx.Response(200, content=b"img"))

    async with httpx.AsyncClient() as http_client:
        result = await fetch_street_view_fallback(
            "65A Vista Street Sans Souci NSW 2219",
            client=http_client,
            secret_accessor=_fake_secret_accessor,
        )

    assert result is not None
    sent_location = dict(httpx.QueryParams(metadata_route.calls[0].request.url.params))["location"]
    assert sent_location == "65A Vista Street Sans Souci NSW 2219"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_street_view_fallback_returns_none_when_metadata_is_not_ok() -> None:
    respx.get(STREET_VIEW_METADATA_URL).mock(
        return_value=httpx.Response(200, json={"status": "ZERO_RESULTS"})
    )
    image_route = respx.get(STREET_VIEW_IMAGE_URL).mock(
        return_value=httpx.Response(200, content=b"unused")
    )

    async with httpx.AsyncClient() as http_client:
        result = await fetch_street_view_fallback(
            "0.0,0.0", client=http_client, secret_accessor=_fake_secret_accessor
        )

    assert result is None
    assert not image_route.called  # never spend on the image fetch without OK metadata


@pytest.mark.asyncio
@respx.mock
async def test_fetch_street_view_fallback_never_sends_the_key_in_a_readable_log_field() -> None:
    """The key must be a request query param (required by the API) but this
    test locks in that the client never does anything else with it, e.g.
    embed it in the attribution string or any other returned field."""
    respx.get(STREET_VIEW_METADATA_URL).mock(
        return_value=httpx.Response(200, json={"status": "OK", "pano_id": "p1", "date": "2021-01"})
    )
    respx.get(STREET_VIEW_IMAGE_URL).mock(return_value=httpx.Response(200, content=b"img"))

    async with httpx.AsyncClient() as http_client:
        result = await fetch_street_view_fallback(
            "1.0,2.0", client=http_client, secret_accessor=_fake_secret_accessor
        )

    assert result is not None
    assert _FAKE_KEY not in result.attribution
    assert _FAKE_KEY not in repr(result)


@pytest.mark.asyncio
@respx.mock
async def test_metadata_request_is_retried_once_on_a_transient_failure() -> None:
    route = respx.get(STREET_VIEW_METADATA_URL)
    route.side_effect = [
        httpx.Response(503),
        httpx.Response(200, json={"status": "OK", "pano_id": "p1", "date": "2021-01"}),
    ]
    respx.get(STREET_VIEW_IMAGE_URL).mock(return_value=httpx.Response(200, content=b"img"))

    async with httpx.AsyncClient() as http_client:
        result = await fetch_street_view_fallback(
            "1.0,2.0", client=http_client, secret_accessor=_fake_secret_accessor
        )

    assert result is not None
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_metadata_request_raises_after_repeated_failures() -> None:
    respx.get(STREET_VIEW_METADATA_URL).mock(return_value=httpx.Response(500))

    async with httpx.AsyncClient() as http_client:
        with pytest.raises(StreetViewUnavailableError):
            await fetch_street_view_fallback(
                "1.0,2.0", client=http_client, secret_accessor=_fake_secret_accessor
            )
