"""Bounding-box grounding: locates named evidence elements on a rendered page.

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

**Known gap**: :meth:`ModelClient.generate` has no `temperature` parameter,
so this module cannot set `temperature=0.0` the way the spike did calling
`google-genai` directly. Reported to the integrator; `models/client.py` is
off this package's lane. `ThinkingLevel.MINIMAL`/`LOW` plus a low-variance
prompt is the mitigation available without that change.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass
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


async def ground_elements(
    client: ModelClient,
    page: RenderedPage,
    labels: Sequence[str],
    *,
    tier: ModelConfig = INTERVIEW,
) -> GroundingResult:
    """Ask the model to locate each of `labels` on `page`'s resized image.

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
    result = await client.generate(tier, prompt, GroundingResponse)

    boxes: list[GroundedBox] = []
    for element in result.output.elements:
        normalized = _extract_normalized_box(element)
        if normalized is None:
            continue
        boxes.append(GroundedBox(label=element.label, bbox=_map_to_page_points(normalized, page)))

    return GroundingResult(boxes=boxes, usage=result.usage, model=result.model)


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
