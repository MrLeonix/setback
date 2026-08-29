"""Tests for setback.evidence.overlays: semantic evidence-overlay rendering.

Fully offline -- no model calls, no PDF fixtures. Every `RenderedPage` here
is built from a fresh in-memory PNG via `evidence.dossier.render_photo`
(the same pattern `tests/evidence/test_grounding.py` uses), and colour
assertions sample actual pixels from the rendered PNG so a regression in
the geometry or the colour table fails loudly rather than only on a visual
inspection.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from setback.evidence.dossier import BoundingBox, render_photo
from setback.evidence.overlays import (
    OVERLAY_COLOR,
    AnchoredElement,
    OverlayBox,
    OverlayRole,
    build_overlay_boxes,
    classify_role,
    label_for,
    render_semantic_overlay,
)
from setback.gate.validator import GateStatus


def _page(size_px: tuple[int, int] = (800, 600)):
    buf = io.BytesIO()
    Image.new("RGB", size_px, color="white").save(buf, format="PNG")
    return render_photo(buf.getvalue(), resize_width_px=400)


def _element(
    anchor_id: str = "anchor-1",
    *,
    bbox: BoundingBox | None = None,
    caption: str = "the wall that shadows your window",
    ground_id: str | None = None,
) -> AnchoredElement:
    return AnchoredElement(
        anchor_id=anchor_id,
        bbox=bbox or BoundingBox(x0=10, y0=10, x1=100, y1=100),
        caption=caption,
        ground_id=ground_id,
    )


# --- classify_role ----------------------------------------------------------


def test_classify_role_with_no_ground_is_evidence_anchor() -> None:
    element = _element(ground_id=None)
    assert classify_role(element, {}) is OverlayRole.EVIDENCE_ANCHOR


def test_classify_role_with_unknown_ground_id_defaults_to_evidence_anchor() -> None:
    element = _element(ground_id="ground-x")
    assert classify_role(element, {}) is OverlayRole.EVIDENCE_ANCHOR


def test_classify_role_shipped_ground_is_supports_shipped() -> None:
    element = _element(ground_id="ground-1")
    status = {"ground-1": GateStatus.SHIPPED}
    assert classify_role(element, status) is OverlayRole.SUPPORTS_SHIPPED


@pytest.mark.parametrize(
    "status",
    [GateStatus.REFUSED_IRRELEVANT, GateStatus.REFUSED_UNSUBSTANTIATED, GateStatus.FLAGGED],
)
def test_classify_role_refused_or_flagged_ground_is_anchor_of_refused(
    status: GateStatus,
) -> None:
    element = _element(ground_id="ground-1")
    assert classify_role(element, {"ground-1": status}) is OverlayRole.ANCHOR_OF_REFUSED


# --- label_for ----------------------------------------------------------------


def test_label_for_neutral_anchor_keeps_caption_verbatim() -> None:
    element = _element(caption="window W.1")
    assert label_for(element, OverlayRole.EVIDENCE_ANCHOR) == "window W.1"


def test_label_for_shipped_appends_plain_english_suffix() -> None:
    element = _element(caption="window W.1")
    label = label_for(element, OverlayRole.SUPPORTS_SHIPPED)
    assert label.startswith("window W.1")
    assert "submission" in label.lower()


def test_label_for_refused_appends_plain_english_suffix() -> None:
    element = _element(caption="window W.1")
    label = label_for(element, OverlayRole.ANCHOR_OF_REFUSED)
    assert label.startswith("window W.1")
    assert "refused" in label.lower()


def test_label_for_blank_caption_falls_back_to_generic_text() -> None:
    element = _element(caption="   ")
    label = label_for(element, OverlayRole.EVIDENCE_ANCHOR)
    assert label.strip()
    assert label != ""


# --- build_overlay_boxes -------------------------------------------------------


def test_build_overlay_boxes_maps_every_element_with_its_role_and_colour() -> None:
    elements = [
        _element("a1", ground_id=None),
        _element("a2", ground_id="g-shipped"),
        _element("a3", ground_id="g-refused"),
    ]
    status = {"g-shipped": GateStatus.SHIPPED, "g-refused": GateStatus.REFUSED_UNSUBSTANTIATED}
    boxes = build_overlay_boxes(elements, status)

    assert [b.anchor_id for b in boxes] == ["a1", "a2", "a3"]
    assert boxes[0].role is OverlayRole.EVIDENCE_ANCHOR
    assert boxes[1].role is OverlayRole.SUPPORTS_SHIPPED
    assert boxes[2].role is OverlayRole.ANCHOR_OF_REFUSED
    assert boxes[0].color == OVERLAY_COLOR[OverlayRole.EVIDENCE_ANCHOR]
    assert boxes[1].color == OVERLAY_COLOR[OverlayRole.SUPPORTS_SHIPPED]
    assert boxes[2].color == OVERLAY_COLOR[OverlayRole.ANCHOR_OF_REFUSED]


def test_build_overlay_boxes_on_empty_input_returns_empty_list() -> None:
    assert build_overlay_boxes([], {}) == []


# --- render_semantic_overlay ----------------------------------------------------


def test_render_semantic_overlay_returns_a_valid_png_wider_and_taller_than_source() -> None:
    page = _page((400, 300))
    png_bytes = render_semantic_overlay(page, [])
    image = Image.open(io.BytesIO(png_bytes))
    assert image.format == "PNG"
    assert image.width == 400
    # The legend strip is appended below the source image.
    assert image.height > 300


def test_render_semantic_overlay_with_no_boxes_still_renders_a_legend() -> None:
    page = _page((400, 300))
    png_bytes_empty = render_semantic_overlay(page, [])
    image = Image.open(io.BytesIO(png_bytes_empty))
    # The bottom-left legend swatch pixel should be the first role's colour
    # even with no boxes drawn -- the legend is unconditional.
    first_role = next(iter(OverlayRole))
    pixel = image.convert("RGB").getpixel((18, 300 + 16))
    assert pixel == OVERLAY_COLOR[first_role]


def test_render_semantic_overlay_draws_each_box_in_its_own_colour() -> None:
    page = _page((400, 300))
    boxes = [
        OverlayBox(
            anchor_id="a1",
            bbox=BoundingBox(x0=10, y0=10, x1=60, y1=60),
            role=OverlayRole.SUPPORTS_SHIPPED,
            color=OVERLAY_COLOR[OverlayRole.SUPPORTS_SHIPPED],
            label="a shipped box",
        ),
        OverlayBox(
            anchor_id="a2",
            bbox=BoundingBox(x0=200, y0=200, x1=250, y1=250),
            role=OverlayRole.ANCHOR_OF_REFUSED,
            color=OVERLAY_COLOR[OverlayRole.ANCHOR_OF_REFUSED],
            label="a refused box",
        ),
    ]
    png_bytes = render_semantic_overlay(page, boxes)
    image = Image.open(io.BytesIO(png_bytes)).convert("RGB")

    # page-points (origin bottom-left) -> full-res top-down pixels: a 72-DPI
    # photo has pt == px, and y flips against the page's true height (300).
    # box 1 (x0=10,y0=10,x1=60,y1=60) -> top edge at pixel y=300-60=240,
    # spanning x in [10,60]; sample its midpoint to avoid corner pixels.
    assert image.getpixel((35, 240)) == OVERLAY_COLOR[OverlayRole.SUPPORTS_SHIPPED]
    # box 2 (x0=200,y0=200,x1=250,y1=250) -> top edge at pixel y=300-250=50,
    # spanning x in [200,250].
    assert image.getpixel((225, 50)) == OVERLAY_COLOR[OverlayRole.ANCHOR_OF_REFUSED]
