"""Semantic evidence overlays: colour-and-label rendering by role/status.

Replaces :func:`setback.evidence.grounding.render_overlay`'s single
arbitrary colour for every grounded element (proven correct for the pure
grounding spike, but flat and unreadable once a page carries anchors
serving *different* grounds at *different* gate outcomes) with a rendering
that answers the resident's actual question on sight: "what is this box,
and what happened to it?" This change came directly out of user testing on
the spike's output, not a stylistic preference.

Colour carries exactly three meanings, fixed by role/status -- never a
per-query or per-label palette:

* **accent blue** (:data:`OverlayRole.EVIDENCE_ANCHOR`) -- an anchor not
  (yet) tied to a ground the gate has decided, e.g. a fresh grounding pass
  before the court/gate stage has run.
* **green** (:data:`OverlayRole.SUPPORTS_SHIPPED`) -- the anchor is cited
  by a ground the gate SHIPPED.
* **red** (:data:`OverlayRole.ANCHOR_OF_REFUSED`) -- the anchor is cited by
  a ground the gate REFUSED (irrelevant or unsubstantiated) or FLAGGED for
  human review.

Each box also gets a short plain-English label chip -- the resident's own
anchor caption, verbatim, plus a role-specific suffix this module adds --
rendered as a filled tag beneath the box, and the whole image always gets
one legend strip explaining the three colours, so the image reads correctly
on its own without surrounding page text.

**Lane boundary.** Input is deliberately narrow: a
:class:`~setback.evidence.dossier.RenderedPage`, a sequence of
:class:`AnchoredElement` (this module's own minimal join of an anchor's
location+caption with the ground it backs, if any), and a mapping of
``ground_id -> GateStatus`` straight from
:class:`~setback.gate.validator.GateDecision`\\ s. This module never imports
``job.pipeline`` or ``console.app`` (both off this work package's lane) and
has no opinion on how a caller assembles that mapping -- see
``notesForOrchestrator`` for the exact integration point in
``job/pipeline.py``/``console/app.py``.
"""

from __future__ import annotations

import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from PIL import Image, ImageDraw

from setback.evidence.dossier import BoundingBox, RenderedPage
from setback.gate.validator import GateStatus


class OverlayRole(StrEnum):
    """The one fact each box's colour communicates. Declaration order is
    also the legend's left-to-right order."""

    EVIDENCE_ANCHOR = "evidence_anchor"
    SUPPORTS_SHIPPED = "supports_shipped"
    ANCHOR_OF_REFUSED = "anchor_of_refused"


OVERLAY_COLOR: Final[Mapping[OverlayRole, tuple[int, int, int]]] = {
    OverlayRole.EVIDENCE_ANCHOR: (37, 99, 235),  # accent blue
    OverlayRole.SUPPORTS_SHIPPED: (22, 163, 74),  # green
    OverlayRole.ANCHOR_OF_REFUSED: (220, 38, 38),  # red
}

_ROLE_LEGEND_TEXT: Final[Mapping[OverlayRole, str]] = {
    OverlayRole.EVIDENCE_ANCHOR: "Evidence anchor",
    OverlayRole.SUPPORTS_SHIPPED: "Supports a ground in your submission",
    OverlayRole.ANCHOR_OF_REFUSED: "Cited by a refused ground",
}

_REFUSED_STATUSES: Final[frozenset[GateStatus]] = frozenset(
    {GateStatus.REFUSED_IRRELEVANT, GateStatus.REFUSED_UNSUBSTANTIATED, GateStatus.FLAGGED}
)
"""Every `GateStatus` that reads as "red" to the resident: an outright
refusal either way, or a flag pending human review -- none of these should
look like a green, shipped anchor."""

_FALLBACK_CAPTION: Final[str] = "This element"


@dataclass(frozen=True, slots=True)
class AnchoredElement:
    """One evidence anchor located on a page, joined with the ground (if
    any) it is cited for. This module's own minimal input shape -- built by
    whatever caller already has both the anchor manifest and the gate's
    decisions."""

    anchor_id: str
    bbox: BoundingBox
    caption: str
    ground_id: str | None = None


@dataclass(frozen=True, slots=True)
class OverlayBox:
    """One box ready to draw: its page-points location, semantic role,
    colour, and plain-English label chip text."""

    anchor_id: str
    bbox: BoundingBox
    role: OverlayRole
    color: tuple[int, int, int]
    label: str


def classify_role(element: AnchoredElement, ground_status: Mapping[str, GateStatus]) -> OverlayRole:
    """The one rule this whole module hangs off.

    An anchor with no ground, or whose ground the gate hasn't decided yet
    (not present in `ground_status`), is a neutral evidence anchor. A
    SHIPPED ground turns its anchors green; a refused or flagged ground
    turns them red.
    """
    if element.ground_id is None:
        return OverlayRole.EVIDENCE_ANCHOR
    status = ground_status.get(element.ground_id)
    if status is None:
        return OverlayRole.EVIDENCE_ANCHOR
    if status is GateStatus.SHIPPED:
        return OverlayRole.SUPPORTS_SHIPPED
    if status in _REFUSED_STATUSES:
        return OverlayRole.ANCHOR_OF_REFUSED
    return OverlayRole.EVIDENCE_ANCHOR


def label_for(element: AnchoredElement, role: OverlayRole) -> str:
    """A short, plain-English chip caption for one box.

    The physical description (e.g. "the wall that shadows your window") is
    always the caller-supplied `caption`, used verbatim -- this function
    only appends a role-specific outcome suffix, never invents new physical
    description of its own. A blank caption falls back to a generic phrase
    so a chip is never rendered empty.
    """
    caption = element.caption.strip() or _FALLBACK_CAPTION
    if role is OverlayRole.SUPPORTS_SHIPPED:
        return f"{caption} — included in your submission"
    if role is OverlayRole.ANCHOR_OF_REFUSED:
        return f"{caption} — cited by a refused ground"
    return caption


def build_overlay_boxes(
    elements: Sequence[AnchoredElement], ground_status: Mapping[str, GateStatus]
) -> list[OverlayBox]:
    """Classify and label every element in `elements`, in order."""
    boxes: list[OverlayBox] = []
    for element in elements:
        role = classify_role(element, ground_status)
        boxes.append(
            OverlayBox(
                anchor_id=element.anchor_id,
                bbox=element.bbox,
                role=role,
                color=OVERLAY_COLOR[role],
                label=label_for(element, role),
            )
        )
    return boxes


# --- geometry (page points, origin bottom-left -> full-res top-down pixels) ---


def _page_points_to_full_res_pixels(
    bbox: BoundingBox, page: RenderedPage
) -> tuple[float, float, float, float]:
    """The same page-points -> full-resolution-pixel inversion
    `evidence.grounding` implements for its own overlay drawing -- kept as
    an independent copy here (that helper is private to its own module) so
    this module has no import-time coupling to grounding's internals; both
    implement the identical mapping documented on `RenderedPage`."""
    pt_to_px = page.dpi / 72.0
    x0_px = bbox.x0 * pt_to_px
    x1_px = bbox.x1 * pt_to_px
    ymin_topdown_pt = page.height_pts - bbox.y1
    ymax_topdown_pt = page.height_pts - bbox.y0
    y0_px = ymin_topdown_pt * pt_to_px
    y1_px = ymax_topdown_pt * pt_to_px
    return x0_px, y0_px, x1_px, y1_px


_BOX_WIDTH_PX: Final[int] = 4
_CHIP_PADDING_PX: Final[int] = 4
_LEGEND_HEIGHT_PX: Final[int] = 40
_LEGEND_SWATCH_PX: Final[int] = 18
_LEGEND_MARGIN_PX: Final[int] = 12
_LEGEND_GAP_PX: Final[int] = 24
_LEGEND_BG: Final[tuple[int, int, int]] = (255, 255, 255)
_LEGEND_TEXT_COLOR: Final[tuple[int, int, int]] = (17, 24, 39)
_CHIP_TEXT_COLOR: Final[tuple[int, int, int]] = (255, 255, 255)


def _draw_label_chip(
    draw: ImageDraw.ImageDraw, x: float, y: float, text: str, color: tuple[int, int, int]
) -> None:
    """Draw a filled, coloured tag with white text, anchored just above
    `(x, y)` (a box's top-left corner) -- clamped so it never draws above
    the image's top edge."""
    left, top, right, bottom = draw.textbbox((0, 0), text)
    width = (right - left) + 2 * _CHIP_PADDING_PX
    height = (bottom - top) + 2 * _CHIP_PADDING_PX
    chip_top = max(0.0, y - height)
    draw.rectangle((x, chip_top, x + width, chip_top + height), fill=color)
    draw.text((x + _CHIP_PADDING_PX, chip_top + _CHIP_PADDING_PX), text, fill=_CHIP_TEXT_COLOR)


def _append_legend(image: Image.Image) -> Image.Image:
    """Return a new image with one legend strip appended beneath `image`:
    one colour swatch and its plain-English meaning per `OverlayRole`, so
    the image is self-explanatory without surrounding page text. Always
    appended, even when no boxes were drawn -- an overlay image should
    never ship unexplained."""
    canvas = Image.new("RGB", (image.width, image.height + _LEGEND_HEIGHT_PX), _LEGEND_BG)
    canvas.paste(image, (0, 0))
    draw = ImageDraw.Draw(canvas)
    x = _LEGEND_MARGIN_PX
    y = image.height + (_LEGEND_HEIGHT_PX - _LEGEND_SWATCH_PX) // 2
    for role in OverlayRole:
        color = OVERLAY_COLOR[role]
        draw.rectangle((x, y, x + _LEGEND_SWATCH_PX, y + _LEGEND_SWATCH_PX), fill=color)
        text = _ROLE_LEGEND_TEXT[role]
        text_x = x + _LEGEND_SWATCH_PX + 6
        draw.text((text_x, y + 2), text, fill=_LEGEND_TEXT_COLOR)
        left, _top, right, _bottom = draw.textbbox((0, 0), text)
        x = round(text_x + (right - left) + _LEGEND_GAP_PX)
    return canvas


def render_semantic_overlay(page: RenderedPage, boxes: Sequence[OverlayBox]) -> bytes:
    """Draw every box in `boxes` onto `page`'s full-resolution image, each
    with its role's colour and a plain-English label chip, plus an
    unconditional legend strip, and return the annotated PNG bytes.

    Boxes are page-points (origin bottom-left, as
    :func:`~setback.evidence.grounding.ground_elements` and the anchor
    manifest store them) and are mapped back to the full-resolution image's
    top-down pixel space before drawing.
    """
    image = Image.open(io.BytesIO(page.png_bytes)).convert("RGB")
    draw = ImageDraw.Draw(image)
    for box in boxes:
        x0, y0, x1, y1 = _page_points_to_full_res_pixels(box.bbox, page)
        draw.rectangle((x0, y0, x1, y1), outline=box.color, width=_BOX_WIDTH_PX)
        _draw_label_chip(draw, x0, y0, box.label, box.color)

    annotated = _append_legend(image)
    buf = io.BytesIO()
    annotated.save(buf, format="PNG")
    return buf.getvalue()


__all__ = [
    "OVERLAY_COLOR",
    "AnchoredElement",
    "OverlayBox",
    "OverlayRole",
    "build_overlay_boxes",
    "classify_role",
    "label_for",
    "render_semantic_overlay",
]
