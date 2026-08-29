"""Tests for setback.evidence.overlays: semantic evidence-overlay rendering.

Fully offline -- no model calls, no PDF fixtures. Every `RenderedPage` here
is built from a fresh in-memory PNG via `evidence.dossier.render_photo`
(the same pattern `tests/evidence/test_grounding.py` uses), or constructed
directly for the non-72-DPI cases below, and colour assertions sample
actual pixels from the rendered PNG so a regression in the geometry or the
colour table fails loudly rather than only on a visual inspection.

**Why a dpi != 72 fixture matters** (see the "at a real PDF's DPI" tests
below): every `render_photo`-based fixture has `dpi == 72`, where
`page_points_to_full_res_pixels`'s `pt_to_px = page.dpi / 72.0` factor is
exactly `1.0` -- a real bug in that conversion (the exact "boxes drawn
against the wrong image dimensions" failure mode reported against the live
gallery shot, which used a 300-DPI-rendered PDF page, not a 72-DPI photo)
would pass every photo-based test here and only surface at a real PDF's
render DPI. At least one test below uses a `RenderedPage` built the way
`evidence.dossier.render_pdf_pages` actually builds one (dpi=300, a
distinct resize scale) so this class of regression cannot hide behind the
72-DPI identity conversion again.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from setback.evidence.dossier import BoundingBox, RenderedPage, render_photo
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


def _pdf_like_page(width_px: int = 1200, height_px: int = 900, dpi: int = 300) -> RenderedPage:
    """A `RenderedPage` shaped like a real `render_pdf_pages` output --
    `dpi` distinct from 72 and a `resize_scale` that is not `1.0` -- unlike
    `_page` above (a 72-DPI photo), so tests using this fixture actually
    exercise `page.dpi / 72.0` and `page.resize_scale` rather than silently
    passing because both factors happen to be trivial.
    """
    full = Image.new("RGB", (width_px, height_px), color="white")
    full_buf = io.BytesIO()
    full.save(full_buf, format="PNG")
    resized_width_px = 400
    resized_height_px = round(height_px * resized_width_px / width_px)
    resized = Image.new("RGB", (resized_width_px, resized_height_px), color="white")
    resized_buf = io.BytesIO()
    resized.save(resized_buf, format="PNG")
    return RenderedPage(
        page_number=1,
        width_pts=width_px * 72.0 / dpi,
        height_pts=height_px * 72.0 / dpi,
        dpi=dpi,
        png_bytes=full_buf.getvalue(),
        resized_png_bytes=resized_buf.getvalue(),
        resized_width_px=resized_width_px,
        resized_height_px=resized_height_px,
    )


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
    "status", [GateStatus.REFUSED_IRRELEVANT, GateStatus.REFUSED_UNSUBSTANTIATED]
)
def test_classify_role_refused_ground_is_anchor_of_refused(status: GateStatus) -> None:
    element = _element(ground_id="ground-1")
    assert classify_role(element, {"ground-1": status}) is OverlayRole.ANCHOR_OF_REFUSED


def test_classify_role_flagged_ground_is_its_own_needs_more_evidence_role() -> None:
    """`FLAGGED` ("needs a human, citations failed repeatedly") is a
    distinct, resolvable-sounding outcome from an outright refusal -- the
    console's own `.doc-viewer__legend` (`console/static/app.js`) has
    always advertised it as its own category ("Needs more evidence",
    `legend-swatch--flagged`), separate from "Cited in a refused ground".
    Folding it into `ANCHOR_OF_REFUSED` (as this module previously did) is
    exactly the "legend promises a colour the overlay never draws" bug:
    the legend chip existed, but no role ever produced its colour."""
    element = _element(ground_id="ground-1")
    role = classify_role(element, {"ground-1": GateStatus.FLAGGED})
    assert role is OverlayRole.NEEDS_MORE_EVIDENCE
    assert role is not OverlayRole.ANCHOR_OF_REFUSED


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


def test_label_for_needs_more_evidence_appends_plain_english_suffix() -> None:
    element = _element(caption="window W.1")
    label = label_for(element, OverlayRole.NEEDS_MORE_EVIDENCE)
    assert label.startswith("window W.1")
    assert "more evidence" in label.lower()


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
        _element("a4", ground_id="g-flagged"),
    ]
    status = {
        "g-shipped": GateStatus.SHIPPED,
        "g-refused": GateStatus.REFUSED_UNSUBSTANTIATED,
        "g-flagged": GateStatus.FLAGGED,
    }
    boxes = build_overlay_boxes(elements, status)

    assert [b.anchor_id for b in boxes] == ["a1", "a2", "a3", "a4"]
    assert boxes[0].role is OverlayRole.EVIDENCE_ANCHOR
    assert boxes[1].role is OverlayRole.SUPPORTS_SHIPPED
    assert boxes[2].role is OverlayRole.ANCHOR_OF_REFUSED
    assert boxes[3].role is OverlayRole.NEEDS_MORE_EVIDENCE
    assert boxes[0].color == OVERLAY_COLOR[OverlayRole.EVIDENCE_ANCHOR]
    assert boxes[1].color == OVERLAY_COLOR[OverlayRole.SUPPORTS_SHIPPED]
    assert boxes[2].color == OVERLAY_COLOR[OverlayRole.ANCHOR_OF_REFUSED]
    assert boxes[3].color == OVERLAY_COLOR[OverlayRole.NEEDS_MORE_EVIDENCE]


def test_build_overlay_boxes_on_empty_input_returns_empty_list() -> None:
    assert build_overlay_boxes([], {}) == []


# --- colour discipline: the overlay's own palette must equal the app's ----------


def test_overlay_colours_match_the_consoles_semantic_status_tokens() -> None:
    """`console/static/style.css`'s light-theme `--status-*-border` custom
    properties are the ONE semantic-colour source of truth for this
    product (founder requirement #5) -- shipped green, flagged/needs-more-
    evidence gold, refused orange. This module cannot read a CSS file at
    runtime, so the hex values are pinned here, literally, as a guard: if
    this test ever goes red, `style.css`'s tokens changed and
    `evidence/overlays.py`'s `OVERLAY_COLOR` must be updated to match, not
    the other way around. Before this fix, this module drew an unrelated,
    independently-invented blue/green/red palette that shared no colour
    with the console's own `.doc-viewer__legend` chrome -- the exact
    "legend advertises colours the image never draws, and vice versa" bug
    reported against the live gallery shot."""
    # --status-shipped-border, --status-flagged-border, --status-refused-
    # border, --status-pending-border (light theme), console/static/style.css.
    assert OVERLAY_COLOR[OverlayRole.SUPPORTS_SHIPPED] == (0x0F, 0x6B, 0x3F)
    assert OVERLAY_COLOR[OverlayRole.NEEDS_MORE_EVIDENCE] == (0xB8, 0x90, 0x1A)
    assert OVERLAY_COLOR[OverlayRole.ANCHOR_OF_REFUSED] == (0xB8, 0x57, 0x1C)
    assert OVERLAY_COLOR[OverlayRole.EVIDENCE_ANCHOR] == (0x8A, 0x94, 0xA1)


# --- render_semantic_overlay ----------------------------------------------------


def test_render_semantic_overlay_returns_a_png_matching_the_source_dimensions() -> None:
    """No baked-in legend strip is appended any more -- the console's own
    `.doc-viewer__legend` (server- and client-rendered identically, see
    `console/app.py`/`app.js`) is now the single legend a viewer sees,
    always docked next to the image rather than baked into pixels that
    can't adapt to the viewer's theme. `render_semantic_overlay`'s output
    is exactly the source page's own dimensions."""
    page = _page((400, 300))
    png_bytes = render_semantic_overlay(page, [])
    image = Image.open(io.BytesIO(png_bytes))
    assert image.format == "PNG"
    assert image.width == 400
    assert image.height == 300


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
        OverlayBox(
            anchor_id="a3",
            bbox=BoundingBox(x0=300, y0=10, x1=350, y1=60),
            role=OverlayRole.NEEDS_MORE_EVIDENCE,
            color=OVERLAY_COLOR[OverlayRole.NEEDS_MORE_EVIDENCE],
            label="a flagged box",
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
    # box 3 (x0=300,y0=10,x1=350,y1=60) -> top edge at pixel y=300-60=240,
    # spanning x in [300,350].
    assert image.getpixel((325, 240)) == OVERLAY_COLOR[OverlayRole.NEEDS_MORE_EVIDENCE]


def test_render_semantic_overlay_at_a_real_pdf_pages_dpi_draws_inside_the_image_bounds() -> None:
    """The golden-ish geometry regression: a box computed at a real PDF
    render's DPI (300, not the 72-DPI identity conversion every other test
    here uses) must land fully inside the rendered bitmap's own bounds --
    not shifted off-canvas, not scaled against the resized (model-input)
    dimensions instead of the full-resolution ones. This is the exact
    failure mode ("boxes drawn against the wrong image dimensions") a
    judge reported live: hollow rectangles floating in blank space beside
    the drawing rather than on top of it."""
    page = _pdf_like_page(width_px=1200, height_px=900, dpi=300)
    # A box roughly in the middle third of the page, in true page points
    # (72 pt/inch; this page is 1200px/300dpi = 4in = 288pt wide).
    bbox = BoundingBox(x0=96.0, y0=72.0, x1=144.0, y1=120.0)
    box = OverlayBox(
        anchor_id="a1",
        bbox=bbox,
        role=OverlayRole.SUPPORTS_SHIPPED,
        color=OVERLAY_COLOR[OverlayRole.SUPPORTS_SHIPPED],
        label="window W.1",
    )
    png_bytes = render_semantic_overlay(page, [box])
    image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    assert image.width == 1200
    assert image.height == 900

    # Full-res pixels: pt_to_px = 300/72. x in [96,144]*4.1667 = [400,600];
    # y (top-down, flipped against height_pts=216pt/900px) in [(216-120),
    # (216-72)]*4.1667 = [400,600].
    expected_x0, expected_x1 = 400, 600
    expected_y0, expected_y1 = 400, 600

    # The drawn rectangle must sit strictly inside the full-resolution
    # canvas -- if it were computed against the *resized* (400x300)
    # dimensions instead, or against the wrong DPI, it would land far
    # outside this expected region (or outside the image entirely).
    assert 0 <= expected_x0 < expected_x1 <= image.width
    assert 0 <= expected_y0 < expected_y1 <= image.height
    sample_point = ((expected_x0 + expected_x1) // 2, expected_y0)
    assert image.getpixel(sample_point) == OVERLAY_COLOR[OverlayRole.SUPPORTS_SHIPPED]

    # Outside the box (a corner of the page far from it) must NOT carry the
    # box's colour -- guards against a degenerate transform that paints the
    # whole canvas or clamps every box to one corner.
    assert image.getpixel((5, 5)) != OVERLAY_COLOR[OverlayRole.SUPPORTS_SHIPPED]
