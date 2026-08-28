"""Tests for setback.evidence.grounding: the spike-proven bounding-box
grounding client usage.

Fully offline: model calls go through a fake `genai.Client` shaped exactly
like the one `tests/models/test_client.py` uses (only
`.aio.models.generate_content` is exercised) injected into a real
`ModelClient`. No network, no ADC.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from PIL import Image

from setback.config import BENCH, INTERVIEW
from setback.evidence.dossier import BoundingBox, RenderedPage, render_photo
from setback.evidence.grounding import (
    GroundedBox,
    GroundedElement,
    GroundingResponse,
    ground_contested_elements,
    ground_elements,
    render_overlay,
)
from setback.models.client import ModelClient


class _FakeUsage:
    def __init__(self, prompt_tokens: int = 100, output_tokens: int = 50) -> None:
        self.prompt_token_count = prompt_tokens
        self.candidates_token_count = output_tokens
        self.thoughts_token_count = 0


class _FakeResponse:
    def __init__(self, parsed: Any) -> None:
        self.parsed = parsed
        self.usage_metadata = _FakeUsage()


class _FakeAsyncModels:
    def __init__(self, response: GroundingResponse) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, *, model: str, contents: Any, config: Any) -> _FakeResponse:
        self.calls.append({"model": model, "contents": contents, "config": config})
        return _FakeResponse(self._response)


class _FakeAio:
    def __init__(self, models: _FakeAsyncModels) -> None:
        self.models = models


class _FakeGenaiClient:
    def __init__(self, response: GroundingResponse) -> None:
        self.models = _FakeAsyncModels(response)
        self.aio = _FakeAio(self.models)


def _client_with(response: GroundingResponse) -> tuple[ModelClient, _FakeAsyncModels]:
    genai_client = _FakeGenaiClient(response)
    client = ModelClient(genai_client=genai_client, token_provider=lambda: "fake-token")  # type: ignore[arg-type]
    return client, genai_client.models


def _square_page(size_px: tuple[int, int] = (1200, 900)) -> RenderedPage:
    buf = io.BytesIO()
    Image.new("RGB", size_px, color="white").save(buf, format="PNG")
    return render_photo(buf.getvalue(), resize_width_px=600)


# --- defensive box parsing ------------------------------------------------------


@pytest.mark.asyncio
async def test_ground_elements_parses_box_2d_key() -> None:
    response = GroundingResponse(
        elements=[GroundedElement(label="window", box_2d=[100.0, 200.0, 300.0, 400.0])]
    )
    client, fake_models = _client_with(response)
    page = _square_page()

    result = await ground_elements(client, page, ["window"], tier=INTERVIEW)

    assert len(result.boxes) == 1
    assert result.boxes[0].label == "window"
    assert fake_models.calls[0]["model"] == INTERVIEW.model


@pytest.mark.asyncio
async def test_ground_elements_falls_back_to_box_key() -> None:
    response = GroundingResponse(
        elements=[GroundedElement(label="door", box=[10.0, 20.0, 30.0, 40.0], box_2d=None)]
    )
    client, _ = _client_with(response)
    page = _square_page()

    result = await ground_elements(client, page, ["door"], tier=INTERVIEW)

    assert len(result.boxes) == 1
    assert result.boxes[0].label == "door"


@pytest.mark.asyncio
async def test_ground_elements_resorts_swapped_ymin_ymax() -> None:
    # ymin > ymax: the ~5% malformed case measured in the spike.
    response = GroundingResponse(
        elements=[GroundedElement(label="window", box_2d=[300.0, 200.0, 100.0, 400.0])]
    )
    client, _ = _client_with(response)
    page = _square_page()

    result = await ground_elements(client, page, ["window"], tier=INTERVIEW)

    assert len(result.boxes) == 1
    box = result.boxes[0].bbox
    assert box.y0 < box.y1
    assert box.x0 < box.x1


@pytest.mark.asyncio
async def test_ground_elements_skips_an_element_with_no_usable_box() -> None:
    response = GroundingResponse(
        elements=[
            GroundedElement(label="unusable", box=None, box_2d=None),
            GroundedElement(label="usable", box_2d=[10.0, 10.0, 20.0, 20.0]),
        ]
    )
    client, _ = _client_with(response)
    page = _square_page()

    result = await ground_elements(client, page, ["unusable", "usable"], tier=INTERVIEW)

    assert [b.label for b in result.boxes] == ["usable"]


# --- coordinate mapping -----------------------------------------------------------


@pytest.mark.asyncio
async def test_ground_elements_maps_normalized_box_to_true_page_points() -> None:
    # A 1000x1000pt page (72 DPI => 1000x1000px full-res), resized to 500px
    # wide (scale 0.5). A box spanning the whole normalized 0-1000 range
    # should map back to the full 0..1000 point page, not the 500px resize.
    buf = io.BytesIO()
    Image.new("RGB", (1000, 1000), color="white").save(buf, format="PNG")
    page = render_photo(buf.getvalue(), resize_width_px=500)
    assert page.width_pts == 1000.0
    assert page.resize_scale == pytest.approx(0.5)

    response = GroundingResponse(
        elements=[GroundedElement(label="whole-page", box_2d=[0.0, 0.0, 1000.0, 1000.0])]
    )
    client, _ = _client_with(response)

    result = await ground_elements(client, page, ["whole-page"], tier=INTERVIEW)

    box = result.boxes[0].bbox
    assert box.x0 == pytest.approx(0.0, abs=1.0)
    assert box.x1 == pytest.approx(1000.0, abs=1.0)
    # y is flipped to bottom-left origin, but a full-page box maps to the
    # full range either way.
    assert box.y0 == pytest.approx(0.0, abs=1.0)
    assert box.y1 == pytest.approx(1000.0, abs=1.0)


@pytest.mark.asyncio
async def test_ground_elements_flips_y_to_bottom_left_origin() -> None:
    # A box in the top-left quadrant of a top-down image should map to the
    # *top* of a bottom-left-origin page (i.e. high y values).
    buf = io.BytesIO()
    Image.new("RGB", (1000, 1000), color="white").save(buf, format="PNG")
    page = render_photo(buf.getvalue(), resize_width_px=500)

    response = GroundingResponse(
        elements=[GroundedElement(label="top-left", box_2d=[0.0, 0.0, 100.0, 100.0])]
    )
    client, _ = _client_with(response)

    result = await ground_elements(client, page, ["top-left"], tier=INTERVIEW)

    box = result.boxes[0].bbox
    assert box.y0 > 800  # near the top in bottom-left-origin coordinates
    assert box.y1 > box.y0


# --- tier selection --------------------------------------------------------------


@pytest.mark.asyncio
async def test_ground_contested_elements_uses_the_bench_tier() -> None:
    response = GroundingResponse(elements=[GroundedElement(label="window", box_2d=[0, 0, 10, 10])])
    client, fake_models = _client_with(response)
    page = _square_page()

    await ground_contested_elements(client, page, ["window"])

    assert fake_models.calls[0]["model"] == BENCH.model


@pytest.mark.asyncio
async def test_ground_elements_defaults_to_the_interview_tier() -> None:
    response = GroundingResponse(elements=[GroundedElement(label="window", box_2d=[0, 0, 10, 10])])
    client, fake_models = _client_with(response)
    page = _square_page()

    await ground_elements(client, page, ["window"])

    assert fake_models.calls[0]["model"] == INTERVIEW.model


@pytest.mark.asyncio
async def test_ground_elements_returns_usage_for_ledger_booking() -> None:
    response = GroundingResponse(elements=[GroundedElement(label="window", box_2d=[0, 0, 10, 10])])
    client, _ = _client_with(response)
    page = _square_page()

    result = await ground_elements(client, page, ["window"])

    assert result.usage.prompt_tokens == 100
    assert result.model == INTERVIEW.model


# --- overlay rendering -------------------------------------------------------------


def test_render_overlay_returns_a_valid_png_at_full_resolution() -> None:
    page = _square_page((800, 600))
    boxes = [
        GroundedBox(label="window", bbox=BoundingBox(x0=50.0, y0=60.0, x1=200.0, y1=180.0)),
    ]

    overlay_bytes = render_overlay(page, boxes)

    image = Image.open(io.BytesIO(overlay_bytes))
    assert image.format == "PNG"
    assert image.width == 800
    assert image.height == 600


def test_render_overlay_with_no_boxes_still_returns_the_base_image() -> None:
    page = _square_page((400, 300))

    overlay_bytes = render_overlay(page, [])

    image = Image.open(io.BytesIO(overlay_bytes))
    assert image.width == 400
    assert image.height == 300
