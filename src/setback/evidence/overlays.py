"""Semantic evidence overlays: colour-and-label rendering by role/status.

Replaces :func:`setback.evidence.grounding.render_overlay`'s single
arbitrary colour for every grounded element (proven correct for the pure
grounding spike, but flat and unreadable once a page carries anchors
serving *different* grounds at *different* gate outcomes) with a rendering
that answers the resident's actual question on sight: "what is this box,
and what happened to it?" This change came directly out of user testing on
the spike's output, not a stylistic preference.

Colour carries exactly four meanings, fixed by role/status -- never a
per-query or per-label palette, and **pinned to the same hex values as
`console/static/style.css`'s light-theme `--status-*-border` custom
properties** (founder requirement #5: one semantic-colour source of truth,
never a locally-invented shade). This module cannot read a CSS file at
runtime, so the values are duplicated here, literally, as constants --
`tests/evidence/test_overlays.py::test_overlay_colours_match_the_consoles_
semantic_status_tokens` is the guard that catches the two drifting apart.
Before this fix, this module drew its own independently-invented
blue/green/red palette with only three roles (no distinct FLAGGED colour),
which shared no colour with `console/static/app.js`'s `.doc-viewer__legend`
chrome (green/gold/orange) drawn around the same image -- reported live as
"the legend advertises colours the overlay never uses; every box on screen
is blue":

* **grey**, `--status-pending-border` (:data:`OverlayRole.EVIDENCE_ANCHOR`)
  -- an anchor not (yet) tied to a ground the gate has decided, e.g. a
  fresh grounding pass before the court/gate stage has run.
* **green**, `--status-shipped-border` (:data:`OverlayRole.SUPPORTS_SHIPPED`)
  -- the anchor is cited by a ground the gate SHIPPED.
* **gold**, `--status-flagged-border` (:data:`OverlayRole.NEEDS_MORE_EVIDENCE`)
  -- the anchor is cited by a ground the gate FLAGGED for human review
  (citations kept failing to resolve) -- a distinct, less-final outcome
  from an outright refusal, exactly as the console's own tag/card
  vocabulary (`.tag--flagged`, `--status-flagged`) already distinguishes
  it everywhere else in the product.
* **orange**, `--status-refused-border` (:data:`OverlayRole.ANCHOR_OF_REFUSED`)
  -- the anchor is cited by a ground the gate REFUSED (irrelevant or
  unsubstantiated).

Each box also gets a short plain-English label chip -- the resident's own
anchor caption, verbatim, plus a role-specific suffix this module adds --
rendered as a filled tag beneath the box. This module no longer bakes a
legend strip into the image itself: `console/app.py`'s
`_render_annotated_overlay_item` and `console/static/app.js`'s
`handleAnnotatedOverlay` both wrap the returned PNG in the identical
`.doc-viewer` + `.doc-viewer__legend` chrome (colour-discipline rule 4: any
time >=1 overlay colour is on screen, its legend must be too), which stays
legible in dark mode and never drifts out of sync with a second,
image-baked copy the way the old approach could.

**Chip legibility scales with image width, not a fixed pixel size.** The
fine-grained bbox anchors this module draws boxes/chips for are typically
located on a full-resolution rendered PDF page (several thousand pixels
wide), which a caller (`job/pipeline.py::_shrink_png_for_storage`) then
downscales for Firestore's document-size limit before storage/display. A
fixed-size chip font read fine on this module's own small offline test
fixtures but shrank to ~7-10px tall -- illegible at normal viewing zoom --
once that real-world downscale was applied (SMOKE.md v5). `_label_font_
size_for_width` sizes the chip font as a ratio of whatever image
`render_semantic_overlay` is actually asked to draw on, so the ratio (and
therefore the readable size) survives any later *uniform* resize a caller
applies, without this module needing to know that resize ever happens.

**Full-resolution output, by contract (wave 9 requirement: a click-to-open
high-res variant).** `render_semantic_overlay` always returns a PNG at
exactly its input `page`'s own full-resolution dimensions (pinned by
`test_render_semantic_overlay_returns_a_png_matching_the_source_dimensions`)
-- it never shrinks its own output. `job/pipeline.py::_shrink_png_for_storage`
downscaling that same return value for Firestore's document-size limit is
a separate, caller-applied step over an *already-full-resolution* image,
not something this module does or needs to know about. In other words:
the full-res bytes this module guarantees already exist at every call
site, in full, before any shrink -- a lightbox/click-to-open feature needs
a place to *persist and serve* that pre-shrink return value (this module
has no storage or routing of its own, out of its lane), not a new render
path here. See `notesForOrchestrator` for the exact one-line integration
point this implies in `job/pipeline.py`.

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

import functools
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from PIL import Image, ImageDraw, ImageFont

from setback.evidence.dossier import BoundingBox, RenderedPage
from setback.gate.validator import GateStatus


class OverlayRole(StrEnum):
    """The one fact each box's colour communicates. Declaration order is
    also the legend's left-to-right order (matching
    `console/static/app.js`'s `.doc-viewer__legend` markup order:
    shipped, flagged, refused, pending)."""

    SUPPORTS_SHIPPED = "supports_shipped"
    NEEDS_MORE_EVIDENCE = "needs_more_evidence"
    ANCHOR_OF_REFUSED = "anchor_of_refused"
    EVIDENCE_ANCHOR = "evidence_anchor"


OVERLAY_COLOR: Final[Mapping[OverlayRole, tuple[int, int, int]]] = {
    # Every value below is the *light-theme* hex from console/static/
    # style.css's --status-*-border tokens (a static PNG cannot itself be
    # theme-aware; the app is filmed/screenshotted with ?theme=light
    # forced, see console/app.py, so this is the palette that is actually
    # ever seen). Keep in sync with style.css by hand -- see this module's
    # docstring and tests/evidence/test_overlays.py's guard test.
    OverlayRole.SUPPORTS_SHIPPED: (0x0F, 0x6B, 0x3F),  # --status-shipped-border, green
    OverlayRole.NEEDS_MORE_EVIDENCE: (0xB8, 0x90, 0x1A),  # --status-flagged-border, gold
    OverlayRole.ANCHOR_OF_REFUSED: (0xB8, 0x57, 0x1C),  # --status-refused-border, orange
    OverlayRole.EVIDENCE_ANCHOR: (0x8A, 0x94, 0xA1),  # --status-pending-border, grey
}

_ROLE_LEGEND_TEXT: Final[Mapping[OverlayRole, str]] = {
    # Verbatim match to console/static/app.js's `.doc-viewer__legend` chip
    # text, so a server-rendered page (console/app.py) and a live SSE
    # client-rendered one (app.js) always read identically.
    OverlayRole.SUPPORTS_SHIPPED: "Supports a shipped ground",
    OverlayRole.NEEDS_MORE_EVIDENCE: "Needs more evidence",
    OverlayRole.ANCHOR_OF_REFUSED: "Cited in a refused ground",
    OverlayRole.EVIDENCE_ANCHOR: "Evidence anchor, not yet decided",
}

_REFUSED_STATUSES: Final[frozenset[GateStatus]] = frozenset(
    {GateStatus.REFUSED_IRRELEVANT, GateStatus.REFUSED_UNSUBSTANTIATED}
)
"""Every `GateStatus` that reads as an outright refusal to the resident.
`GateStatus.FLAGGED` (citations kept failing to resolve; needs a human, not
necessarily a "no") is deliberately **not** included here -- it gets its
own `OverlayRole.NEEDS_MORE_EVIDENCE` colour, matching the console's own
`.tag--flagged`/`--status-flagged` distinction from `.tag--refused`
everywhere else in the product."""

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
    if status is GateStatus.FLAGGED:
        return OverlayRole.NEEDS_MORE_EVIDENCE
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
    if role is OverlayRole.NEEDS_MORE_EVIDENCE:
        return f"{caption} — needs more evidence"
    return caption


DEFAULT_MAX_OVERLAY_BOXES: Final[int] = 8
"""Hard ceiling on how many boxes one overlay ever draws (founder
requirement #2, wave 9: "cap the box count -- pick a sensible N, e.g. 6-8,
keep most-relevant"). Chosen at the top of that range: generous enough that
today's real fixture (5 grounded elements) is never trimmed (see
`test_build_overlay_boxes_default_cap_is_generous_enough_for_a_real_page`),
while still bounding a future page that grounds far more elements than a
resident could usefully read at once."""


def build_overlay_boxes(
    elements: Sequence[AnchoredElement],
    ground_status: Mapping[str, GateStatus],
    *,
    max_boxes: int = DEFAULT_MAX_OVERLAY_BOXES,
) -> list[OverlayBox]:
    """Classify and label every element in `elements`, then cap the result
    at `max_boxes`.

    "Most-relevant" (the cap's own tie-break rule) means: a box with a real,
    decided outcome to report -- shipped, flagged, or cited by a refused
    ground -- is always kept ahead of a neutral `EVIDENCE_ANCHOR` (grounded,
    but not yet tied to any decided ground) once the ceiling is reached. A
    resident gains far more from seeing what happened to five decided
    elements than from six one of which is still an undecided grey box.
    Every decided box sorts ahead of every neutral one in the returned
    list once trimming happens; within each of those two groups, original
    input order is preserved -- the cap trims from the *back* of each
    group, it never otherwise reorders what survives.
    """
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
    if len(boxes) <= max_boxes:
        return boxes

    decided = [b for b in boxes if b.role is not OverlayRole.EVIDENCE_ANCHOR]
    neutral = [b for b in boxes if b.role is OverlayRole.EVIDENCE_ANCHOR]
    kept_decided = decided[:max_boxes]
    remaining_slots = max_boxes - len(kept_decided)
    kept_neutral = neutral[:remaining_slots] if remaining_slots > 0 else []
    return [*kept_decided, *kept_neutral]


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
_CHIP_TEXT_COLOR: Final[tuple[int, int, int]] = (255, 255, 255)

_LABEL_FONT_MIN_SIZE_PX: Final[int] = 16
_LABEL_FONT_WIDTH_RATIO: Final[float] = 0.02
"""`render_semantic_overlay` sizes the label-chip font as
``max(_LABEL_FONT_MIN_SIZE_PX, round(image_width_px * _LABEL_FONT_WIDTH_RATIO))``
-- see `_label_font_size_for_width`'s docstring for why this must be a
function of the image actually being drawn on, not a fixed pixel size."""
_LABEL_FONT_PATHS: Final[tuple[str, ...]] = (
    # macOS (dev boxes, screenshots taken locally).
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    # Linux (Debian/Ubuntu-family container base images, e.g. this repo's
    # own `python:3.12-slim` -- present only if a font package such as
    # `fonts-dejavu-core`/`fonts-liberation` is installed in the image,
    # see `Dockerfile`).
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)


@functools.lru_cache(maxsize=8)
def _label_font(
    size_px: int = _LABEL_FONT_MIN_SIZE_PX,
) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    """A real TTF/TTC font at `size_px` for label-chip captions, tried in
    order down `_LABEL_FONT_PATHS`.

    Fixes a live-reported bug: `ImageDraw.text`/`textbbox` called with no
    `font=` (this module's previous behaviour) falls back to PIL's own
    ``ImageFont.load_default()`` -- a tiny fixed-size bitmap font whose
    space glyph measures only ~2px wide, which disappears entirely once
    the annotated overlay is downscaled for storage
    (`job/pipeline.py::_shrink_png_for_storage`) or simply anti-aliased on
    screen. A multi-word caption like ``"This element"`` reads as
    ``"Thiselement"`` -- reproduced independently in a 3-line PIL script
    and confirmed against every real multi-word evidence caption, not
    just this module's own fixtures (see `SMOKE.md`, wave 6).

    Falls back to `ImageFont.load_default()` only if none of
    `_LABEL_FONT_PATHS` exists on this machine at all, so this function
    never raises -- a missing font package on a given deploy target
    degrades the chip's legibility, it must never crash the overlay
    render. Cached (`lru_cache`, keyed by `size_px`) since every call for a
    given size in one process resolves to the same font object --
    filesystem probing on every box drawn would be wasteful over a page
    with many anchors.
    """
    for path in _LABEL_FONT_PATHS:
        try:
            return ImageFont.truetype(path, size_px)
        except OSError:
            continue
    return ImageFont.load_default()


def _label_font_size_for_width(width_px: int) -> int:
    """The label-chip font size to use when drawing on an image `width_px`
    pixels wide.

    Fixes a second, live-reported legibility bug (SMOKE.md v5): the
    fine-grained bbox anchors `_ground_annotated_evidence` locates are
    drawn onto the full-resolution rendered PDF page (routinely several
    thousand pixels wide for a real drawing), at a *fixed* 16px chip font
    -- reasonable on this module's own small offline test fixtures, but
    once `job/pipeline.py::_shrink_png_for_storage` downscales that same
    page ~4x for Firestore's document-size limit before it is ever stored
    or displayed, the chip shrinks along with it to ~7-10px tall,
    illegible at normal viewing/screenshot zoom.

    This module has no visibility into that downstream resize (its own
    lane boundary -- see the module docstring): it never imports
    `job.pipeline`. Instead, the font is sized as a fixed *ratio* of
    whatever image `render_semantic_overlay` is actually asked to draw
    on. Since `_shrink_png_for_storage`'s resize is uniform (both
    dimensions scaled by the same factor), a chip drawn at
    `width_px * _LABEL_FONT_WIDTH_RATIO` keeps that same ratio -- and
    therefore roughly the same *readable* size -- however much the image
    is downscaled afterwards. `_LABEL_FONT_MIN_SIZE_PX` is the floor, so
    this module's own small fixtures (well under the ~800px width where
    the ratio alone would already exceed it) render exactly as before.
    """
    return max(_LABEL_FONT_MIN_SIZE_PX, round(width_px * _LABEL_FONT_WIDTH_RATIO))


_ChipRect = tuple[float, float, float, float]


def _rects_overlap(a: _ChipRect, b: _ChipRect) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0)


_CHIP_STACK_GAP_PX: Final[int] = 3
"""Extra vertical gap `_draw_label_chip` opens between two chips it stacks
to avoid a collision -- purely cosmetic breathing room so two stacked
chips read as visibly separate labels rather than merely touching edges."""

def _vertically_overlaps(a: _ChipRect, b: _ChipRect) -> bool:
    _, a_top, _, a_bottom = a
    _, b_top, _, b_bottom = b
    return not (a_bottom <= b_top or b_bottom <= a_top)


def _shift_clear_of_avoid(
    rect: _ChipRect, avoid: Sequence[_ChipRect], max_x: float | None
) -> _ChipRect:
    """Place `rect` on a "shelf" immediately beside whichever already-placed
    rects in `avoid` share its row (vertically overlap it) -- immediately
    right of the rightmost one, or immediately left of the leftmost, never
    past the image's left edge (`0.0`) or, when `max_x` is given, its right
    edge either. This is the two-row fallback `_draw_label_chip` reaches
    for only once vertical stacking has already run out of headroom at the
    image's top edge and the chip still collides -- live-reported (wave 12,
    FILM2): three adjacent boxes near a page's top edge left every one of
    their chips pinned to `chip_top == 0.0` with nowhere left to stack
    upward, so they silently overlapped instead. Returns `rect` unchanged
    if neither side has room, so the caller's "accept the overlap rather
    than loop forever" behaviour still applies as the final fallback."""
    left, top, right, bottom = rect
    width = right - left
    same_row = [placed for placed in avoid if _vertically_overlaps(rect, placed)]
    if not same_row:
        return rect
    candidates: list[_ChipRect] = []
    right_edge = max(placed[2] for placed in same_row) + _CHIP_STACK_GAP_PX
    if max_x is None or right_edge + width <= max_x:
        candidates.append((right_edge, top, right_edge + width, bottom))
    left_edge = min(placed[0] for placed in same_row) - _CHIP_STACK_GAP_PX - width
    if left_edge >= 0.0:
        candidates.append((left_edge, top, left_edge + width, bottom))
    for candidate in candidates:
        if not any(_rects_overlap(candidate, placed) for placed in avoid):
            return candidate
    return rect


def _draw_label_chip(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    text: str,
    color: tuple[int, int, int],
    *,
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont | None = None,
    avoid: Sequence[_ChipRect] | None = None,
    max_x: float | None = None,
) -> _ChipRect:
    """Draw a filled, coloured tag with white text, anchored just above
    `(x, y)` (a box's top-left corner) -- clamped so it never draws above
    the image's top edge. Returns the drawn chip's own rectangle
    `(left, top, right, bottom)`.

    Uses a real TTF (`font`, or `_label_font()`'s default if omitted --
    never PIL's implicit bitmap default) so a multi-word caption's spaces
    render as real, visible gaps -- see `_label_font`'s docstring.
    `render_semantic_overlay` always passes an explicit, width-scaled
    `font` (`_label_font_size_for_width`); the default here exists so this
    function's own direct callers/tests can draw a chip without picking a
    size themselves.

    `avoid`, if given, is every chip rectangle already drawn earlier in
    this render. When two boxes sit close together, their captions'
    natural chip width can collide -- live-reported (wave 9) as several
    boxes' labels overwriting each other into one illegible run-on string
    ("window W.1 -- ciwindow W.2 -- cit..."), since each chip is an opaque
    filled rectangle drawn on top of whatever was there before. Rather than
    let a later chip silently paint over an earlier one, this chip is
    pushed straight up (stacked, one chip-height plus `_CHIP_STACK_GAP_PX`
    at a time) until it no longer overlaps anything in `avoid`, or until it
    would be pushed off the top of the image entirely (`chip_top` already
    at `0.0`). Reaching the top edge still overlapping doesn't give up
    immediately: `_shift_clear_of_avoid` tries a horizontal offset next
    (the two-row fallback, wave 12 -- boxes anchored near a page's own top
    edge have no vertical headroom to stack into at all, live-reported
    against FILM2 as several adjacent chips overlapping right at the page
    top). Only if neither direction finds a clear spot is the last,
    closest-fitting position accepted rather than looping forever -- a
    still-cramped chip beats one silently dropped or an infinite loop.
    `max_x`, if given, bounds the horizontal search to the image's own
    width so a shifted chip is never drawn off the right edge either.
    """
    font = font or _label_font()
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    width = (right - left) + 2 * _CHIP_PADDING_PX
    height = (bottom - top) + 2 * _CHIP_PADDING_PX
    chip_top = max(0.0, y - height)
    rect: _ChipRect = (x, chip_top, x + width, chip_top + height)

    if avoid:
        while any(_rects_overlap(rect, placed) for placed in avoid):
            new_top = rect[1] - height - _CHIP_STACK_GAP_PX
            if new_top < 0.0:
                if rect[1] <= 0.0:
                    # Already pinned to the top edge with no vertical
                    # headroom left -- try a horizontal shift before
                    # accepting the overlap as the final fallback.
                    rect = _shift_clear_of_avoid(rect, avoid, max_x)
                    break
                new_top = 0.0
            rect = (x, new_top, x + width, new_top + height)

    draw.rectangle(rect, fill=color)
    draw.text(
        (rect[0] + _CHIP_PADDING_PX, rect[1] + _CHIP_PADDING_PX),
        text,
        fill=_CHIP_TEXT_COLOR,
        font=font,
    )
    return rect


_MAX_BOX_PAGE_AREA_FRACTION: Final[float] = 0.9
"""A box covering >= this fraction of the page's own area is never drawn --
defense-in-depth for founder requirement #1 ("NEVER draw page-level/
whole-document anchors as boxes"). `AnchoredElement.bbox` is already a
required, non-optional `BoundingBox`, so a *page-level* anchor (which the
dossier's own anchor manifest always stores with `bbox=None`) is already
structurally impossible to turn into a drawable box -- this guard instead
catches the geometrically equivalent case this module's own contract
cannot rule out by typing alone: a legitimately-constructed but
badly-mis-grounded box that happens to cover almost the entire page (e.g.
a model returning `[0, 0, 1000, 1000]`), which would read exactly like the
founder's live "one huge page-wide box" report regardless of which anchor
produced it. Keyed on *area*, not either dimension alone, so a real,
useful annotation that is legitimately wide-but-thin (a height-limit datum
line spanning most of the page's width) or tall-but-narrow is never
suppressed by this guard -- see the two "still draws" regression tests
beside this constant's own guard test."""


def _covers_almost_the_whole_page(bbox: BoundingBox, page: RenderedPage) -> bool:
    page_area = page.width_pts * page.height_pts
    if page_area <= 0:
        return False
    box_area = max(0.0, bbox.x1 - bbox.x0) * max(0.0, bbox.y1 - bbox.y0)
    return (box_area / page_area) >= _MAX_BOX_PAGE_AREA_FRACTION


def render_semantic_overlay(page: RenderedPage, boxes: Sequence[OverlayBox]) -> bytes:
    """Draw every box in `boxes` onto `page`'s full-resolution image, each
    with its role's colour and a plain-English label chip, and return the
    annotated PNG bytes -- exactly `page`'s own dimensions, no legend strip
    appended.

    No legend is baked into the pixels: `console/app.py`'s
    `_render_annotated_overlay_item` and `console/static/app.js`'s
    `handleAnnotatedOverlay` both wrap this image in the identical
    `.doc-viewer__legend` chrome using `ROLE_LEGEND_TEXT`/`OVERLAY_COLOR`'s
    own token names, which stays legible in dark mode and can never drift
    out of sync with a second, image-baked copy (the bug this module's
    docstring documents).

    Boxes are page-points (origin bottom-left, as
    :func:`~setback.evidence.grounding.ground_elements` and the anchor
    manifest store them) and are mapped back to the full-resolution image's
    top-down pixel space before drawing. A box covering almost the whole
    page (see `_covers_almost_the_whole_page`) is silently skipped rather
    than drawn -- founder requirement #1's geometric backstop. Every
    surviving box's label chip is drawn clear of every chip already placed
    for an earlier box in `boxes` (see `_draw_label_chip`'s `avoid`
    parameter) so adjacent boxes' captions stack rather than overwrite one
    another into an illegible run-on string.
    """
    image = Image.open(io.BytesIO(page.png_bytes)).convert("RGB")
    draw = ImageDraw.Draw(image)
    # One font, sized once from this image's own width (see
    # `_label_font_size_for_width`'s docstring) and reused for every box on
    # the page -- not recomputed per box, since it depends only on `image`.
    font = _label_font(_label_font_size_for_width(image.width))
    placed_chip_rects: list[_ChipRect] = []
    for box in boxes:
        if _covers_almost_the_whole_page(box.bbox, page):
            continue
        x0, y0, x1, y1 = _page_points_to_full_res_pixels(box.bbox, page)
        draw.rectangle((x0, y0, x1, y1), outline=box.color, width=_BOX_WIDTH_PX)
        chip_rect = _draw_label_chip(
            draw,
            x0,
            y0,
            box.label,
            box.color,
            font=font,
            avoid=placed_chip_rects,
            max_x=image.width,
        )
        placed_chip_rects.append(chip_rect)

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


ROLE_LEGEND_TEXT: Final[Mapping[OverlayRole, str]] = _ROLE_LEGEND_TEXT
"""Public re-export of this module's legend copy, so `console/app.py`'s
server-rendered `.doc-viewer__legend` and `console/static/app.js`'s
client-rendered one can both source their text from here rather than each
maintaining their own (drifting) copy. Iterate `OverlayRole` for legend
order; see `ROLE_CSS_CLASS_SUFFIX` for the matching `legend-swatch--*`
CSS class each role should render with."""

ROLE_CSS_CLASS_SUFFIX: Final[Mapping[OverlayRole, str]] = {
    # console/static/style.css's existing `.legend-swatch--{suffix}`
    # classes (`--shipped`/`--flagged`/`--refused`/`--pending`), which are
    # already theme-aware (they resolve to the same --status-*-border
    # custom properties `OVERLAY_COLOR` pins the literal hex of, above).
    OverlayRole.SUPPORTS_SHIPPED: "shipped",
    OverlayRole.NEEDS_MORE_EVIDENCE: "flagged",
    OverlayRole.ANCHOR_OF_REFUSED: "refused",
    OverlayRole.EVIDENCE_ANCHOR: "pending",
}


__all__ = [
    "DEFAULT_MAX_OVERLAY_BOXES",
    "OVERLAY_COLOR",
    "ROLE_CSS_CLASS_SUFFIX",
    "ROLE_LEGEND_TEXT",
    "AnchoredElement",
    "OverlayBox",
    "OverlayRole",
    "build_overlay_boxes",
    "classify_role",
    "label_for",
    "render_semantic_overlay",
]
