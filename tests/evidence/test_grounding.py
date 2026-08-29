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
    DescribedElement,
    DrawingDescription,
    DrawingType,
    GroundedBox,
    GroundedElement,
    GroundingResponse,
    describe_drawing,
    describe_then_ground,
    ground_contested_elements,
    ground_described_elements,
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


class _ScriptedFakeAsyncModels:
    """Like `_FakeAsyncModels` above, but replays one response per call in
    order -- needed for the two-stage describe-then-ground pipeline, whose
    two calls return different response shapes (`DrawingDescription`, then
    `GroundingResponse`)."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, *, model: str, contents: Any, config: Any) -> _FakeResponse:
        self.calls.append({"model": model, "contents": contents, "config": config})
        return _FakeResponse(self._responses.pop(0))


def _scripted_client(*responses: Any) -> tuple[ModelClient, _ScriptedFakeAsyncModels]:
    fake_models = _ScriptedFakeAsyncModels(list(responses))
    genai_client = _FakeGenaiClientShell(fake_models)
    client = ModelClient(genai_client=genai_client, token_provider=lambda: "fake-token")  # type: ignore[arg-type]
    return client, fake_models


class _FakeGenaiClientShell:
    def __init__(self, models: _ScriptedFakeAsyncModels) -> None:
        self.models = models
        self.aio = _FakeAio(models)  # type: ignore[arg-type]


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


# --- root-cause fix: grounding calls must actually send the page image ----------
#
# Before this fix, `ground_elements` asked `client.generate` with a plain
# text prompt -- the rendered page's bytes were never attached as image
# content, so a "grounding" call was really the model guessing plausible
# box positions for a label's own words, never actually looking at the
# page. This is consistent with CASES.md's Blocker 1 symptom (boxes for
# "window W.1" etc. landing mid a cover *letter*, not mid a drawing): the
# model was never shown the letter (or the drawing) either way.


@pytest.mark.asyncio
async def test_ground_elements_sends_the_page_image_as_real_multimodal_content() -> None:
    response = GroundingResponse(
        elements=[GroundedElement(label="window", box_2d=[0.0, 0.0, 10.0, 10.0])]
    )
    client, fake_models = _client_with(response)
    page = _square_page()

    await ground_elements(client, page, ["window"])

    from google.genai import types

    contents = fake_models.calls[0]["contents"]
    assert isinstance(contents, list)
    image_parts = [p for p in contents if isinstance(p, types.Part) and p.inline_data is not None]
    assert len(image_parts) == 1
    assert image_parts[0].inline_data.data == page.resized_png_bytes
    assert image_parts[0].inline_data.mime_type == "image/png"


# --- stage 1: describe -----------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_drawing_parses_drawing_type_and_elements() -> None:
    description = DrawingDescription(
        drawing_type=DrawingType.SITE_PLAN,
        elements=[
            DescribedElement(
                name="building footprint",
                approx_location="centre",
                relevant_to=["height_bulk"],
            )
        ],
        orientation_cues="north arrow top-left",
    )
    client, fake_models = _scripted_client(description)
    page = _square_page()

    result = await describe_drawing(client, page)

    assert result.description.drawing_type is DrawingType.SITE_PLAN
    assert result.description.elements[0].name == "building footprint"
    assert result.description.orientation_cues == "north arrow top-left"
    assert fake_models.calls[0]["model"] == INTERVIEW.model


@pytest.mark.asyncio
async def test_describe_drawing_sends_the_page_image() -> None:
    from google.genai import types

    description = DrawingDescription(drawing_type=DrawingType.ELEVATION, elements=[])
    client, fake_models = _scripted_client(description)
    page = _square_page()

    await describe_drawing(client, page)

    contents = fake_models.calls[0]["contents"]
    image_parts = [p for p in contents if isinstance(p, types.Part) and p.inline_data is not None]
    assert len(image_parts) == 1
    assert image_parts[0].inline_data.data == page.resized_png_bytes


@pytest.mark.asyncio
async def test_describe_drawing_pins_temperature_zero() -> None:
    description = DrawingDescription(drawing_type=DrawingType.OTHER, elements=[])
    client, fake_models = _scripted_client(description)
    page = _square_page()

    await describe_drawing(client, page)

    assert fake_models.calls[0]["config"].temperature == 0.0


@pytest.mark.asyncio
async def test_describe_drawing_defaults_to_the_interview_tier() -> None:
    description = DrawingDescription(drawing_type=DrawingType.OTHER, elements=[])
    client, fake_models = _scripted_client(description)
    page = _square_page()

    await describe_drawing(client, page)

    assert fake_models.calls[0]["model"] == INTERVIEW.model


# --- stage 2: ground the described elements ---------------------------------------


@pytest.mark.asyncio
async def test_ground_described_elements_uses_stage_one_element_names_as_labels() -> None:
    """The core regression test for the founder's diagnosis: a site plan's
    grounded labels must come from stage 1's own element inventory, never
    the old hardcoded elevation-only label list (window/door/height datum)."""
    description = DrawingDescription(
        drawing_type=DrawingType.SITE_PLAN,
        elements=[
            DescribedElement(
                name="building footprint", approx_location="centre", relevant_to=["height_bulk"]
            ),
            DescribedElement(
                name="north boundary setback",
                approx_location="left edge",
                relevant_to=["overshadowing"],
            ),
        ],
    )
    response = GroundingResponse(
        elements=[
            GroundedElement(label="building footprint", box_2d=[100.0, 100.0, 400.0, 400.0]),
            GroundedElement(label="north boundary setback", box_2d=[0.0, 0.0, 1000.0, 50.0]),
        ]
    )
    client, fake_models = _scripted_client(response)
    page = _square_page()

    result = await ground_described_elements(client, page, description)

    assert {b.label for b in result.boxes} == {"building footprint", "north boundary setback"}
    from google.genai import types

    prompt_text = next(
        p.text for p in fake_models.calls[0]["contents"] if isinstance(p, types.Part) and p.text
    )
    assert "window" not in prompt_text.lower()
    assert "door" not in prompt_text.lower()
    assert "building footprint" in prompt_text
    assert "north boundary setback" in prompt_text


@pytest.mark.asyncio
async def test_ground_described_elements_canonicalizes_a_verbose_echoed_label() -> None:
    """Measured live (wave-11 verification against the real elevations
    fixture): the model sometimes echoes back stage 2's own prompt hint
    (``"window W.1 (roughly upper-left)"``) as the box's `label` instead of
    copying just the bare element name it was given -- left uncorrected,
    that verbose text becomes the overlay chip's caption, exactly the
    "legible chip" quality bar this wave must not regress. A returned
    label that starts with one of stage 1's own element names must be
    normalized back down to that exact name."""
    description = DrawingDescription(
        drawing_type=DrawingType.ELEVATION,
        elements=[DescribedElement(name="window W.1", approx_location="upper-left")],
    )
    response = GroundingResponse(
        elements=[
            GroundedElement(
                label="window W.1 (roughly upper-left of North Elevation)",
                box_2d=[0.0, 0.0, 10.0, 10.0],
            )
        ]
    )
    client, _ = _scripted_client(response)
    page = _square_page()

    result = await ground_described_elements(client, page, description)

    assert [b.label for b in result.boxes] == ["window W.1"]


@pytest.mark.asyncio
async def test_ground_described_elements_keeps_an_unrecognized_label_verbatim() -> None:
    """A returned label that doesn't match any described element's name
    (not even as a prefix) is kept as-is rather than dropped -- the box's
    *position* is still real grounding work; only the caption-fidelity
    fix above changes anything about it."""
    description = DrawingDescription(
        drawing_type=DrawingType.ELEVATION,
        elements=[DescribedElement(name="window W.1", approx_location="upper-left")],
    )
    response = GroundingResponse(
        elements=[GroundedElement(label="something else entirely", box_2d=[0.0, 0.0, 10.0, 10.0])]
    )
    client, _ = _scripted_client(response)
    page = _square_page()

    result = await ground_described_elements(client, page, description)

    assert [b.label for b in result.boxes] == ["something else entirely"]


@pytest.mark.asyncio
async def test_ground_described_elements_sends_the_page_image() -> None:
    from google.genai import types

    description = DrawingDescription(
        drawing_type=DrawingType.ELEVATION,
        elements=[DescribedElement(name="window W.1", approx_location="upper-left")],
    )
    response = GroundingResponse(
        elements=[GroundedElement(label="window W.1", box_2d=[0.0, 0.0, 10.0, 10.0])]
    )
    client, fake_models = _scripted_client(response)
    page = _square_page()

    await ground_described_elements(client, page, description)

    contents = fake_models.calls[0]["contents"]
    image_parts = [p for p in contents if isinstance(p, types.Part) and p.inline_data is not None]
    assert len(image_parts) == 1


@pytest.mark.asyncio
async def test_ground_described_elements_skips_the_call_when_nothing_was_described() -> None:
    """No point spending a call asking the model to locate zero elements --
    also means a page stage 1 found nothing on (e.g. a blank cover sheet)
    never reaches the model a second time."""
    description = DrawingDescription(drawing_type=DrawingType.OTHER, elements=[])
    client, fake_models = _scripted_client()
    page = _square_page()

    result = await ground_described_elements(client, page, description)

    assert result.boxes == []
    assert fake_models.calls == []


@pytest.mark.asyncio
async def test_ground_described_elements_defaults_to_the_interview_tier() -> None:
    description = DrawingDescription(
        drawing_type=DrawingType.ELEVATION,
        elements=[DescribedElement(name="window W.1", approx_location="left")],
    )
    response = GroundingResponse(
        elements=[GroundedElement(label="window W.1", box_2d=[0.0, 0.0, 10.0, 10.0])]
    )
    client, fake_models = _scripted_client(response)
    page = _square_page()

    await ground_described_elements(client, page, description)

    assert fake_models.calls[0]["model"] == INTERVIEW.model


# --- describe-then-ground: the production two-stage orchestration ----------------


@pytest.mark.asyncio
async def test_describe_then_ground_grounds_site_plan_elements_not_elevation_labels() -> None:
    description = DrawingDescription(
        drawing_type=DrawingType.SITE_PLAN,
        elements=[
            DescribedElement(
                name="building footprint", approx_location="centre", relevant_to=["height_bulk"]
            ),
            DescribedElement(
                name="neighbouring lot boundary",
                approx_location="right edge",
                relevant_to=["overshadowing"],
            ),
        ],
        orientation_cues="north arrow top-left",
    )
    ground_response = GroundingResponse(
        elements=[
            GroundedElement(label="building footprint", box_2d=[100.0, 100.0, 400.0, 400.0]),
            GroundedElement(label="neighbouring lot boundary", box_2d=[0.0, 900.0, 1000.0, 1000.0]),
        ]
    )
    client, fake_models = _scripted_client(description, ground_response)
    page = _square_page()

    result = await describe_then_ground(client, page)

    assert len(fake_models.calls) == 2
    labels = {b.label for b in result.boxes}
    assert labels == {"building footprint", "neighbouring lot boundary"}
    assert not any(word in " ".join(labels).lower() for word in ("window", "door"))


@pytest.mark.asyncio
async def test_describe_then_ground_still_grounds_elevation_elements() -> None:
    """Elevations must look as good as before the rework."""
    description = DrawingDescription(
        drawing_type=DrawingType.ELEVATION,
        elements=[
            DescribedElement(name="window W.1", approx_location="upper-left"),
            DescribedElement(name="door D.1", approx_location="lower-centre"),
        ],
    )
    ground_response = GroundingResponse(
        elements=[
            GroundedElement(label="window W.1", box_2d=[100.0, 100.0, 200.0, 200.0]),
            GroundedElement(label="door D.1", box_2d=[500.0, 500.0, 700.0, 600.0]),
        ]
    )
    client, fake_models = _scripted_client(description, ground_response)
    page = _square_page()

    result = await describe_then_ground(client, page)

    assert {b.label for b in result.boxes} == {"window W.1", "door D.1"}


@pytest.mark.asyncio
async def test_describe_then_ground_skips_stage_two_when_nothing_was_described() -> None:
    description = DrawingDescription(drawing_type=DrawingType.OTHER, elements=[])
    client, fake_models = _scripted_client(description)
    page = _square_page()

    result = await describe_then_ground(client, page)

    assert result.boxes == []
    assert len(fake_models.calls) == 1


@pytest.mark.asyncio
async def test_describe_then_ground_sums_usage_across_both_calls() -> None:
    description = DrawingDescription(
        drawing_type=DrawingType.ELEVATION,
        elements=[DescribedElement(name="window W.1", approx_location="left")],
    )
    ground_response = GroundingResponse(
        elements=[GroundedElement(label="window W.1", box_2d=[0.0, 0.0, 10.0, 10.0])]
    )
    client, _ = _scripted_client(description, ground_response)
    page = _square_page()

    result = await describe_then_ground(client, page)

    # _FakeUsage defaults to prompt=100/output=50 per call; two real calls
    # were made (describe, then ground), so the totals must reflect both.
    assert result.usage.prompt_tokens == 200
    assert result.usage.output_tokens == 100
