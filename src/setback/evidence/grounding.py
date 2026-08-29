"""Bounding-box grounding: locates named evidence elements on a rendered page.

**Two-stage describe-then-ground pipeline (wave 11).** The production entry
point is :func:`describe_then_ground`, which replaces a single hardcoded
elevation-shaped label list (``"window W.1"``, ``"door D.1"``, ``"9m height
limit datum line"``, previously baked into `job/pipeline.py` regardless of
what kind of drawing was actually being grounded) with two calls:

1. :func:`describe_drawing` (stage 1) -- one cheap vision call whose sole
   job is inventory: given the page image, what kind of drawing is this
   (:class:`DrawingType`: a site plan, an elevation, a section, a floor
   plan, a photo, or other), and what real, visible elements does it
   actually contain (:class:`DescribedElement`)?
2. :func:`ground_described_elements` (stage 2) -- requests bounding boxes
   *only* for the elements stage 1's inventory said exist, labelled with
   stage 1's own element names, never a fixed list.

This fixes a real, confirmed-live defect (`CASES.md`'s Blocker 1, the real
`5e791203...` case): a **top-down Site Plan** was being asked for
window/door/height-datum boxes -- elevation-only concepts that are not
visible on a top-down drawing at all -- because the old single-stage call
never knew what kind of drawing it had been handed. Now a site plan is
described (and then grounded) in its own vocabulary: building footprint,
boundary setbacks, the neighbouring lot, a north arrow -- and an elevation
is still described in the vocabulary that always worked for it (windows,
doors, a height datum line), so the flagship elevation shot is unaffected.

**Root-cause fix, same wave**: every vision call in this module (both
stages, plus the original single-label-list :func:`ground_elements`, still
kept as the general "locate these labels" primitive
:func:`ground_contested_elements` wraps) now actually attaches the page's
own image bytes as real multimodal content (`ModelClient.generate`'s
`images` parameter). Before this fix, `ground_elements` sent only a text
prompt describing what to look for -- the rendered page image was never
attached at all, so every "grounding" call was, in effect, the model
guessing plausible-sounding box coordinates for a label's own words
without ever seeing the page. This is consistent with Blocker 1's observed
symptom (window/door boxes landing mid a cover *letter*, not mid a
drawing): the model was never shown either document, so switching which
document was selected never changed anything about how the boxes were
placed. `ModelClient.generate`'s `images` parameter is new this wave too
(`models/client.py`, exercised by `tests/models/test_client.py`) --
outside this module's own lane, but required for either stage of this
module's pipeline to be a real vision call at all, so it is fixed here
rather than left as dead-on-arrival infrastructure.

Proven live in the spike (`spike-grounding.md`, verified against the real
DA2026/0359 elevations drawing and an embedded aerial photo, 5 calls, 19/20
correct localizations): `gemini-3.5-flash-lite` at `ThinkingLevel.MINIMAL`
(:data:`setback.config.INTERVIEW`) is the default grounding call, with
`gemini-3.7-flash` at `ThinkingLevel.LOW` (:data:`setback.config.BENCH`) as
the second-opinion pass for adjudicator-contested citations. Both tiers are
reused as-is from :mod:`setback.config` rather than duplicated here — they
are the same "cheap default worker" / "adjudication escalation" tiers used
everywhere else in the system.

Every call goes through :class:`setback.models.client.ModelClient`, the
system's sole model call site — this module never constructs its own
`genai.Client`. Boxes come back normalized 0-1000 against the *resized*
image actually sent (see :class:`setback.evidence.dossier.RenderedPage`);
:func:`ground_elements` maps every box back to true page points (origin
bottom-left, matching :mod:`setback.gate.validator`'s convention) before
handing anything back to a caller, tracking the page's recorded resize
factor rather than assuming the model's normalization matches the
full-resolution render.

Defensive parsing (`_extract_normalized_box`) accepts both the ``box`` and
``box_2d`` keys and re-sorts each axis pair, per the spike's measured ~5%
malformed rate (wrong key name, reversed min/max) — a resolvable citation
must never be wrongly refused, or worse, silently mis-boxed, because of a
parseable-but-malformed model reply.

Every call pins `temperature=0.0` (`ModelClient.generate`'s optional
`temperature` parameter, wave 4), matching the spike's own low-variance
setup exactly rather than relying on `ThinkingLevel.MINIMAL`/`LOW` alone.

**Default tier re-evaluated, kept as `INTERVIEW` (wave 9).** The founder
asked that switching the default grounding call to the more capable
`BENCH` tier (`gemini-3.7-flash`, `ThinkingLevel.LOW`) be evaluated, cost
being irrelevant at these sizes, and adopted if it measurably improved box
placement. Tested live against the committed film-case fixture
(`tests/fixtures/nsw/docs/elevations.pdf` page 1, the same five production
labels): both tiers located all five elements, but `BENCH` placed all four
of the window/door boxes on the *wrong* elevation entirely (the South
elevation drawing, y in [319, 363]pt) while `INTERVIEW` placed the same
four correctly on the North elevation (y in [408, 477]pt) -- the elevation
that actually carries the architect's own "W.1"/"W.2"/"D.1" callouts in
this fixture. `BENCH` is measurably *worse* here, not better, so the
default stays `INTERVIEW`; `ground_contested_elements`'s existing
second-opinion escalation to `BENCH` for adjudicator-contested citations
is untouched by this finding (a genuinely contested citation is a
different situation from a first-pass default). Re-run this comparison
(the exact two calls, no more) if a future, larger real-DA fixture makes
the picture different -- this conclusion is fixture-specific evidence, not
a permanent verdict on either model.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from PIL import Image, ImageDraw
from pydantic import BaseModel

from setback.config import BENCH, INTERVIEW, ModelConfig
from setback.evidence.dossier import BoundingBox, RenderedPage
from setback.models.client import ModelClient, TokenUsage

_GROUNDING_INSTRUCTION: Final[str] = (
    "You are locating specific labelled elements on an architectural drawing or "
    "site photograph. For each of the following elements, if it is visible in the "
    "image, return its tight bounding box. Respond with a JSON object matching the "
    "given schema and nothing else. Each box must be `box_2d: [ymin, xmin, ymax, "
    "xmax]`, normalized to the range 0-1000 against the image's own width and "
    "height. If an element is not visible, omit it rather than guessing."
)


def _image_part(page: RenderedPage) -> tuple[bytes, str]:
    """The `(bytes, mime_type)` pair every vision call in this module sends
    alongside its prompt (`ModelClient.generate`'s `images` parameter) --
    the resized image actually shown to the model, matching
    :func:`_map_to_page_points`'s own assumption about what was sent."""
    return page.resized_png_bytes, "image/png"


class GroundedElement(BaseModel):
    """One raw element as the model returns it.

    Both `box` and `box_2d` are accepted (the spike measured the model
    occasionally using the wrong key name) and either may be malformed
    (`None`, missing, or the wrong length) — see :func:`_extract_normalized_box`.
    """

    label: str
    box: list[float] | None = None
    box_2d: list[float] | None = None


class GroundingResponse(BaseModel):
    """The structured-output schema requested from the model.

    Wrapped in an object (rather than a bare JSON array, as the spike's
    direct `google-genai` call used) because
    :meth:`ModelClient.generate` requires a Pydantic `BaseModel` response
    type, not a bare list.
    """

    elements: list[GroundedElement]


@dataclass(frozen=True, slots=True)
class GroundedBox:
    """One grounded element's location, already mapped to true page points
    (origin bottom-left) — ready to become a
    :class:`setback.gate.validator.Citation` bbox or an
    :class:`setback.evidence.dossier.EvidenceAnchor`."""

    label: str
    bbox: BoundingBox


@dataclass(frozen=True, slots=True)
class GroundingResult:
    """A grounding call's boxes plus its token usage, so the caller (the
    court/tribunal package, not this one) can book the cost against its
    :class:`setback.state.ledger.Ledger`."""

    boxes: list[GroundedBox]
    usage: TokenUsage
    model: str


def _extract_normalized_box(item: GroundedElement) -> tuple[float, float, float, float] | None:
    """Defensively parse one raw element's box, accepting either key and
    re-sorting each axis pair, or `None` if no usable box is present.

    Returns:
        `(ymin, xmin, ymax, xmax)`, normalized 0-1000, each pair sorted so
        `ymin <= ymax` and `xmin <= xmax` even if the model returned them
        reversed (the malformed case measured in the spike).
    """
    raw = item.box_2d if item.box_2d is not None else item.box
    if raw is None or len(raw) != 4:
        return None
    ymin, xmin, ymax, xmax = (float(v) for v in raw)
    if ymin > ymax:
        ymin, ymax = ymax, ymin
    if xmin > xmax:
        xmin, xmax = xmax, xmin
    return ymin, xmin, ymax, xmax


def _map_to_page_points(
    normalized: tuple[float, float, float, float], page: RenderedPage
) -> BoundingBox:
    """Map a `(ymin, xmin, ymax, xmax)` box normalized 0-1000 against the
    resized image actually sent to the model back to true page points,
    origin bottom-left.

    Three steps, each tracked on `page` rather than assumed:
    1. normalized 0-1000 -> resized-image pixel space.
    2. resized pixel -> full-resolution pixel, dividing by `page.resize_scale`
       (the ratio the resize was actually rendered at, not a nominal one).
    3. full-resolution pixel (top-down) -> page points (top-down), then flip
       to bottom-left origin using the page's true height.
    """
    ymin, xmin, ymax, xmax = normalized

    xmin_resized_px = xmin / 1000.0 * page.resized_width_px
    xmax_resized_px = xmax / 1000.0 * page.resized_width_px
    ymin_resized_px = ymin / 1000.0 * page.resized_height_px
    ymax_resized_px = ymax / 1000.0 * page.resized_height_px

    scale = page.resize_scale
    xmin_full_px = xmin_resized_px / scale
    xmax_full_px = xmax_resized_px / scale
    ymin_full_px = ymin_resized_px / scale
    ymax_full_px = ymax_resized_px / scale

    px_to_pt = 72.0 / page.dpi
    x0 = xmin_full_px * px_to_pt
    x1 = xmax_full_px * px_to_pt
    ymin_topdown_pt = ymin_full_px * px_to_pt
    ymax_topdown_pt = ymax_full_px * px_to_pt

    # Flip top-down image coordinates to bottom-left-origin page points.
    y0 = page.height_pts - ymax_topdown_pt
    y1 = page.height_pts - ymin_topdown_pt

    return BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1)


def _page_points_to_full_res_pixels(
    bbox: BoundingBox, page: RenderedPage
) -> tuple[float, float, float, float]:
    """Invert :func:`_map_to_page_points`: map a page-points box back to
    full-resolution, top-down pixel coordinates, for overlay drawing."""
    pt_to_px = page.dpi / 72.0
    x0_px = bbox.x0 * pt_to_px
    x1_px = bbox.x1 * pt_to_px
    ymin_topdown_pt = page.height_pts - bbox.y1
    ymax_topdown_pt = page.height_pts - bbox.y0
    y0_px = ymin_topdown_pt * pt_to_px
    y1_px = ymax_topdown_pt * pt_to_px
    return x0_px, y0_px, x1_px, y1_px


async def _run_grounding_call(
    client: ModelClient, page: RenderedPage, prompt: str, tier: ModelConfig
) -> GroundingResult:
    """Shared plumbing behind every box-locating call this module makes
    (:func:`ground_elements` and :func:`ground_described_elements`):
    attach `page`'s own resized image as real multimodal content
    (:func:`_image_part` — the wave-11 root-cause fix, see module
    docstring), pin `temperature=0.0` per the spike, defensively parse each
    returned box, and map every usable one back to true page points."""
    result = await client.generate(
        tier, prompt, GroundingResponse, temperature=0.0, images=[_image_part(page)]
    )

    boxes: list[GroundedBox] = []
    for element in result.output.elements:
        normalized = _extract_normalized_box(element)
        if normalized is None:
            continue
        boxes.append(GroundedBox(label=element.label, bbox=_map_to_page_points(normalized, page)))

    return GroundingResult(boxes=boxes, usage=result.usage, model=result.model)


async def ground_elements(
    client: ModelClient,
    page: RenderedPage,
    labels: Sequence[str],
    *,
    tier: ModelConfig = INTERVIEW,
) -> GroundingResult:
    """Ask the model to locate each of `labels` on `page`'s resized image.

    The general "locate these exact labels" primitive -- kept as-is for
    :func:`ground_contested_elements`'s adjudication-escalation use and any
    caller that already knows exactly what it wants located. The
    production annotated-overlay pipeline (`job/pipeline.py`) no longer
    calls this directly with a fixed label list; see
    :func:`describe_then_ground` for the describe-then-ground pipeline that
    replaced that fixed list with a per-drawing-type inventory.

    Args:
        client: The sole model call site.
        page: The rendered page (or photo) to ground elements on.
        labels: The element labels to look for (e.g. `["W.3", "9m height
            datum line"]`); an element not visible is simply omitted from
            the result rather than guessed at.
        tier: The model tier to call — defaults to the cheap default worker
            tier; pass :data:`setback.config.BENCH` (or use
            :func:`ground_contested_elements`) for a contested citation's
            second opinion.

    Returns:
        Every successfully parsed grounded box (mapped to true page
        points), plus the call's token usage for ledger booking.
    """
    prompt = f"{_GROUNDING_INSTRUCTION}\n\nElements to locate: {', '.join(labels)}"
    return await _run_grounding_call(client, page, prompt, tier)


async def ground_contested_elements(
    client: ModelClient,
    page: RenderedPage,
    labels: Sequence[str],
) -> GroundingResult:
    """Re-run grounding at the higher-confidence `BENCH` tier
    (`gemini-3.7-flash`, `ThinkingLevel.LOW`) for a citation the Adjudicator
    has seen a clause/evidence split on — a thin, explicit wrapper over
    :func:`ground_elements` so a court-package call site never has to spell
    out which tier "contested" means.
    """
    return await ground_elements(client, page, labels, tier=BENCH)


# --- two-stage describe-then-ground pipeline (wave 11) -----------------------------


class DrawingType(StrEnum):
    """What kind of DA evidence page stage 1 (:func:`describe_drawing`)
    thinks it is looking at -- deliberately a different, coarser vocabulary
    than :class:`setback.clerk.DocumentKind` (which classifies a whole
    *document* from its filename/first-page text, for document *selection*)
    since this classifies one rendered *page image* from what is actually
    visible in it, purely to pick which grounding vocabulary applies."""

    SITE_PLAN = "site_plan"
    ELEVATION = "elevation"
    SECTION = "section"
    FLOOR_PLAN = "floor_plan"
    PHOTO = "photo"
    OTHER = "other"


class DescribedElement(BaseModel):
    """One real, visible element stage 1's inventory found on the page.

    `relevant_to` is the model's own judgement of which of a resident's
    planning-objection categories (the same vocabulary as
    :class:`setback.clerk.ConcernType`'s values, e.g. ``"overshadowing"``,
    ``"height_bulk"``) this element could help assess -- informational
    only in this wave (stage 2 grounds every described element regardless
    of it), carried through so a future caller can filter or prioritise by
    it without another vision call.
    """

    name: str
    approx_location: str
    relevant_to: list[str] = []


class DrawingDescription(BaseModel):
    """The structured-output schema requested from stage 1
    (:func:`describe_drawing`)."""

    drawing_type: DrawingType
    elements: list[DescribedElement]
    orientation_cues: str | None = None


@dataclass(frozen=True, slots=True)
class DescriptionResult:
    """Stage 1's parsed description plus its token usage, mirroring
    :class:`GroundingResult`'s shape for the call that precedes it."""

    description: DrawingDescription
    usage: TokenUsage
    model: str


_DESCRIBE_INSTRUCTION: Final[str] = (
    "You are inventorying one page of evidence submitted with a NSW development "
    "application objection. It could be an architectural drawing (a top-down site "
    "plan, a building elevation, a vertical section, or an internal floor plan) or "
    "a real site photograph. Respond with a JSON object matching the given schema "
    "and nothing else.\n\n"
    "First, classify the page's `drawing_type`: `site_plan` (a top-down plan "
    "showing the block, boundaries, and building footprint), `elevation` (a "
    "side-on view of the building's facade), `section` (a vertical cross-section), "
    "`floor_plan` (a top-down internal room layout), `photo` (a real photograph, "
    "not a drawing), or `other`.\n\n"
    "Then list every real, visible `elements` entry actually shown in the image -- "
    "never invent or guess at one that is not really there; omit it instead. Each "
    'entry needs a short `name` (e.g. "window W.1", "north boundary", "the '
    'overhanging balcony"), an `approx_location` in plain English (e.g. '
    '"upper-right"), and `relevant_to`: zero or more of a resident\'s '
    "planning-objection categories this element could help assess (choose from: "
    "height_bulk, privacy_overlooking, overshadowing, trees_landscape, "
    "traffic_parking, heritage_character, view_loss, property_value, noise). What "
    "is actually worth listing depends on the drawing type: for an elevation, "
    "list each individual window and door opening as its OWN separate element "
    '(use the drawing\'s own callout label, e.g. "window W.1"/"door D.1", if one '
    "is printed next to it; otherwise a location-based name) plus any "
    "height-limit datum line drawn on it -- never describe an entire elevation "
    'view (e.g. "the whole north elevation") as a single element; for a site '
    "plan, prioritise the building footprint, boundary setback lines, the "
    "neighbouring lot, and any north arrow or shadow-direction indicator; for a "
    "photograph, prioritise the specific area(s) a resident's objection would "
    "actually be about. Finally, note any `orientation_cues` visible (e.g. a north "
    "arrow, a compass rose, a labelled elevation direction) as a short string, or "
    "omit it if none are visible."
)


async def describe_drawing(
    client: ModelClient,
    page: RenderedPage,
    *,
    tier: ModelConfig = INTERVIEW,
) -> DescriptionResult:
    """Stage 1 of the describe-then-ground pipeline: one cheap vision call
    whose sole job is inventory -- what kind of drawing is `page`, and what
    real elements does it actually contain? Always `temperature=0.0`
    (matching :func:`_run_grounding_call`'s own pin) and the cheap default
    worker tier, since this is a factual inventory pass, not a creative
    one.

    Returns the parsed :class:`DrawingDescription` plus this call's own
    token usage -- :func:`describe_then_ground` sums it with stage 2's own
    usage for the combined two-call cost.
    """
    result = await client.generate(
        tier, _DESCRIBE_INSTRUCTION, DrawingDescription, temperature=0.0, images=[_image_part(page)]
    )
    return DescriptionResult(description=result.output, usage=result.usage, model=result.model)


_DRAWING_TYPE_LABEL: Final[dict[DrawingType, str]] = {
    DrawingType.SITE_PLAN: "top-down site plan",
    DrawingType.ELEVATION: "building elevation drawing",
    DrawingType.SECTION: "building cross-section drawing",
    DrawingType.FLOOR_PLAN: "floor plan drawing",
    DrawingType.PHOTO: "site photograph",
    DrawingType.OTHER: "drawing or photograph",
}
"""Plain-English name for each :class:`DrawingType`, used only to orient
stage 2's prompt (:func:`ground_described_elements`) -- the actual choice
of *which* elements to locate always comes from stage 1's own inventory,
never from this mapping."""


def _stage_two_prompt(description: DrawingDescription) -> str:
    """Build stage 2's grounding prompt: locate exactly the elements stage
    1's inventory named, nothing else -- the hardcoded elevation-only label
    list (`window W.1`/`window W.2`/`window W.3`/`door D.1`/`9m height limit
    datum line`) this replaces is gone; every label here comes from
    `description.elements` regardless of drawing type."""
    element_lines = "\n".join(
        f"- {element.name} (roughly {element.approx_location})" for element in description.elements
    )
    drawing_type_label = _DRAWING_TYPE_LABEL[description.drawing_type]
    return (
        f"You are locating specific elements on a {drawing_type_label}, previously "
        "inventoried by an earlier pass over this exact image. Locate ONLY the "
        "following elements -- do not invent or guess at any element not in this "
        "list, and omit one from your answer if you cannot actually find it. "
        "Respond with a JSON object matching the given schema and nothing else. "
        "Each box must be `box_2d: [ymin, xmin, ymax, xmax]`, normalized to the "
        "range 0-1000 against the image's own width and height. Copy each "
        "located element's `label` EXACTLY, character for character, from the "
        "element names listed below -- never re-word it, and never include the "
        "location hint that follows it in parentheses.\n\n"
        f"Elements to locate:\n{element_lines}"
    )


def _canonicalize_label(returned_label: str, known_names: Sequence[str]) -> str:
    """Normalize a stage-2 box's returned `label` back to stage 1's own
    element name when it is (or starts with) one, case-insensitively.

    Measured live: despite :func:`_stage_two_prompt`'s explicit "copy
    exactly" instruction, the model sometimes echoes back its own location
    hint alongside the name (``"window W.1 (roughly upper-left)"``) rather
    than the bare name it was asked to copy. Left uncorrected, that verbose
    text becomes the overlay chip's caption -- exactly the legibility this
    wave must not regress. A label that doesn't match any known name (not
    even as a prefix) is returned unchanged rather than dropped -- the
    box's *position* is still real grounding work either way.
    """
    lowered = returned_label.strip().lower()
    for name in known_names:
        normalized_name = name.strip().lower()
        if lowered == normalized_name or lowered.startswith(normalized_name):
            return name
    return returned_label


async def ground_described_elements(
    client: ModelClient,
    page: RenderedPage,
    description: DrawingDescription,
    *,
    tier: ModelConfig = INTERVIEW,
) -> GroundingResult:
    """Stage 2 of the describe-then-ground pipeline: request boxes only for
    the elements `description` (stage 1's own inventory) said exist,
    labelled with stage 1's own element names -- never a fixed label list.

    Makes zero model calls (returns an empty result immediately) when
    `description.elements` is empty -- nothing to ask for, and a page
    stage 1 found nothing worth listing on (e.g. a blank cover sheet)
    should not spend a second call confirming that.
    """
    if not description.elements:
        return GroundingResult(
            boxes=[], usage=TokenUsage(prompt_tokens=0, output_tokens=0), model=tier.model
        )
    prompt = _stage_two_prompt(description)
    result = await _run_grounding_call(client, page, prompt, tier)
    known_names = [element.name for element in description.elements]
    canonical_boxes = [
        GroundedBox(label=_canonicalize_label(box.label, known_names), bbox=box.bbox)
        for box in result.boxes
    ]
    return GroundingResult(boxes=canonical_boxes, usage=result.usage, model=result.model)


def _sum_usage(first: TokenUsage, second: TokenUsage) -> TokenUsage:
    """Combine two real model calls' token usage into one total -- used by
    :func:`describe_then_ground` since it makes two real calls (unlike
    every other function in this module, which makes at most one) and a
    future ledger-booking caller needs the true combined cost, not just
    stage 2's own figure."""
    return TokenUsage(
        prompt_tokens=first.prompt_tokens + second.prompt_tokens,
        output_tokens=first.output_tokens + second.output_tokens,
        thinking_tokens=first.thinking_tokens + second.thinking_tokens,
        estimated=first.estimated or second.estimated,
    )


async def describe_then_ground(
    client: ModelClient,
    page: RenderedPage,
    *,
    tier: ModelConfig = INTERVIEW,
) -> GroundingResult:
    """The production annotated-overlay grounding pipeline (wave 11):
    describe `page` (stage 1), then ground only the elements the
    description says exist (stage 2) -- see the module docstring for why
    this replaces the old single hardcoded elevation-shaped label list.

    Two real model calls when stage 1 finds at least one element (their
    combined usage is returned, via :func:`_sum_usage`, model
    tagged as stage 2's own); exactly one call, and an empty
    :class:`GroundingResult`, when stage 1 finds nothing to ground
    (:func:`ground_described_elements`'s own short-circuit).
    """
    description_result = await describe_drawing(client, page, tier=tier)
    description = description_result.description
    if not description.elements:
        return GroundingResult(
            boxes=[], usage=description_result.usage, model=description_result.model
        )
    grounding_result = await ground_described_elements(client, page, description, tier=tier)
    return GroundingResult(
        boxes=grounding_result.boxes,
        usage=_sum_usage(description_result.usage, grounding_result.usage),
        model=grounding_result.model,
    )


_OVERLAY_COLOR: Final[tuple[int, int, int]] = (220, 30, 30)
_OVERLAY_WIDTH_PX: Final[int] = 4


def render_overlay(page: RenderedPage, boxes: Sequence[GroundedBox]) -> bytes:
    """Draw every box in `boxes` onto `page`'s full-resolution image and
    return the annotated PNG bytes, for the console and the demo.

    Boxes are page-points, origin bottom-left (as :func:`ground_elements`
    returns them) and are mapped back to the full-resolution image's
    top-down pixel space before drawing — the inverse of
    :func:`_map_to_page_points`.
    """
    image = Image.open(io.BytesIO(page.png_bytes)).convert("RGB")
    draw = ImageDraw.Draw(image)
    for box in boxes:
        x0, y0, x1, y1 = _page_points_to_full_res_pixels(box.bbox, page)
        draw.rectangle((x0, y0, x1, y1), outline=_OVERLAY_COLOR, width=_OVERLAY_WIDTH_PX)
        draw.text((x0 + 2, max(0.0, y0 - 14)), box.label, fill=_OVERLAY_COLOR)

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()
