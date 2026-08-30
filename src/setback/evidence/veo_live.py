"""Judge-gated LIVE Veo 3.1 illustration generation (wave 13,
founder-authorized -- see ``odds-wave/impl-veo.md`` and the veo lane's own
``~/Desktop/setback-hackathon/veo/RECOMMENDATION.md``).

**What this wave adds over the earlier, wave-12 static-clip attachment**
(:mod:`setback.evidence.illustration`): that module attaches one
pre-generated demo clip to three hardcoded case ids, at zero runtime cost,
regardless of who is viewing. This module instead generates a genuinely new
Veo 3.1 clip, per case, live, from that specific case's own elevation
drawing -- but ONLY for a case a privileged (judge/founder) session created,
and ONLY up to a small, hard-capped total number of real generations across
the whole deployment (:data:`veo_live_max_generations`). The anonymous,
public-facing flow can never trigger a single dollar of Veo spend: nothing
here is reachable from a `public_origin` case (see `job.pipeline`'s
`_case_created_judge_origin` gate, which this module's own client is called
from).

**Conditioning image**: :func:`build_conditioning_image` adapts the veo
lane's own manual recipe (`PROMPTING-NOTES.md`'s "Conditioning input used"
section: crop the drawing to its elevation only, with the title block --
address, architect name/ABN/email -- cropped out, then to a true 16:9 frame)
into an automated, per-case transform: crop a fixed fraction off the bottom
of the rendered plan page (this build's NSW elevation drawings consistently
place their title block there -- a documented heuristic, not a layout
parser, see that function's own docstring) and then centre-crop to 16:9.

**Generation recipe**: :data:`_VEO_LIVE_PROMPT` is the veo lane's own
clip-3 prompt (`prompt-3.txt`), copied verbatim -- clip-3 was the lane's own
recommended, stop-bar-clearing result (RECOMMENDATION.md), and the prompt
itself is a generic style/motion instruction with no per-case content baked
in (the *conditioning image* is what varies per case, not the prompt text),
so "adapt the prompt to this case" is achieved entirely by conditioning on
that case's own cropped elevation -- there is nothing case-specific for the
prompt text itself to name. Same model tier, same video-only/1080p/16:9/8s
settings the lane's own spend ledger priced at $1.60/clip
(`SPEND-LEDGER.md`).

**Vertex location**: Veo 3.1 is only available in ``us-central1`` on this
project (`PROMPTING-NOTES.md`'s live model-card check: `200` in
`us-central1`, `404` in `australia-southeast1`, where the rest of this app
runs) -- :class:`VertexVeoLiveClient` talks to Vertex in `us-central1`
regardless of :data:`setback.config.VERTEX_LOCATION` (`"global"`), a
deliberate, one-off exception to this build's otherwise-single-region model
traffic.

**Storage**: the generated clip is written by Veo to a scratch prefix in
this deployment's own uploads bucket (`config.GCS_UPLOADS_BUCKET`), then
downloaded and handed back as plain bytes so the caller
(`job.pipeline.RealPipelineRunner`) can store it through the exact same
`EvidenceUploadStore` port every other case document already goes through
(`cases/{case_id}/uploads/{document_id}.mp4`) -- no new storage mechanism,
and the console's existing `GET /api/cases/{case_id}/documents/{document_id}`
route serves it back with no new route. The scratch object itself is
deleted immediately after download (housekeeping; never left as a stray
unbilled-for object in the shared bucket).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable
from io import BytesIO
from typing import Any, Final, Protocol

from google import genai

# `google-cloud-storage` ships no `py.typed` marker (see
# `evidence.storage`'s own identical suppression) -- mypy cannot resolve
# `storage` as an attribute of the `google.cloud` namespace package.
from google.cloud import storage  # type: ignore[attr-defined]
from google.genai import types
from PIL import Image

from setback import config

# --- founder-approved copy (mirrors evidence.illustration's own pins) ------

VEO_LIVE_COST_NOTE: Final[str] = (
    "Generated live with Veo 3.1 · US$1.60 · not part of this case's run cost"
)
"""The mandatory cost-disclosure line for a LIVE (judge-gated) illustration
card, distinct from `evidence.illustration.ILLUSTRATION_COST_NOTE`'s
"Pre-generated" wording -- this clip genuinely was generated live, for this
specific judge session's case, not ahead of time."""

VEO_LIVE_GENERATING_MESSAGE: Final[str] = (
    "Your illustration is being generated — give it a couple of minutes and refresh."
)
"""The console's generating-placeholder copy (wave-13 design point 3),
shown on a judge_origin case whose illustration has started but not yet
finished."""

VEO_LIVE_DOCUMENT_ID: Final[str] = "veo-live-illustration"
"""Fixed per-case document id the generated clip is stored under (mirrors
`job.pipeline._STREET_VIEW_DOCUMENT_ID`'s own fixed-id convention) -- at
most one live illustration ever exists per case, so no per-generation
uniqueness is needed."""


# --- env-controlled gating (config.py's own "read once at import" idiom
# doesn't fit here: `job.pipeline.RealPipelineRunner`'s constructor already
# takes these as injectable, test-overridable parameters, defaulting to a
# *function* call rather than a module-level constant -- so a test can
# `monkeypatch.setenv` before constructing a runner without needing to
# reload this module) -------------------------------------------------------


def _env_flag(name: str, *, default: bool) -> bool:
    """A permissive boolean env-var reader: unset means `default`; anything
    case-insensitively matching `"0"`/`"false"`/`"no"`/`"off"`/`""` is
    `False`; everything else is `True`."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def veo_live_enabled() -> bool:
    """`VEO_LIVE_ENABLED`, default `True` (per the wave-13 brief: "env
    VEO_LIVE_ENABLED (default true)"). The one flag that can take the whole
    judge-gated live-generation feature offline instantly (e.g. mid-judging,
    if something is wrong) with no redeploy -- read fresh on every call."""
    return _env_flag("VEO_LIVE_ENABLED", default=True)


def veo_live_max_generations() -> int:
    """`VEO_LIVE_MAX_GENERATIONS`, default 10 -- the founder-authorized hard
    cap on total real Veo generations across the whole deployment (~US$16 at
    $1.60/clip), enforced by the atomic global counter in
    `state.guard_store.VeoLiveCounterStore`."""
    return int(os.environ.get("VEO_LIVE_MAX_GENERATIONS", "10"))


VEO_LIVE_TIMEOUT_SECONDS: Final[float] = float(
    os.environ.get("VEO_LIVE_TIMEOUT_SECONDS", str(6 * 60))
)
"""Hard wall-clock timeout (~6 minutes, per the brief) on the whole
judge-gated illustration post-step -- the veo lane's own generations each
took 90-120s end-to-end (RECOMMENDATION.md), so this leaves generous headroom
while still guaranteeing the post-step can never hang the job process
indefinitely on a stuck Vertex operation. A module constant (not a
per-call function like the two flags above) since `asyncio.wait_for`'s
timeout is read once, at the moment the post-step starts, not something a
test needs to vary via env after the fact -- tests instead pass an explicit
`veo_live_timeout_seconds` to `RealPipelineRunner`'s constructor."""

VEO_LIVE_MODEL: Final[str] = os.environ.get("VEO_LIVE_MODEL", "veo-3.1-generate-001")
"""The exact model id the veo lane's own recommended clip-3 used
(PROMPTING-NOTES.md/RECOMMENDATION.md) -- overridable, but never expected to
change before the hackathon deadline."""

VEO_LIVE_VERTEX_LOCATION: Final[str] = os.environ.get("VEO_LIVE_VERTEX_LOCATION", "us-central1")
"""Veo 3.1 is only available in `us-central1` on this project (see module
docstring) -- deliberately independent of `config.VERTEX_LOCATION`."""


# --- the generation recipe (veo lane's own clip-3, verbatim) ----------------

_VEO_LIVE_PROMPT: Final[str] = (
    "A flat, clean architectural blueprint-style line-art illustration of a proposed "
    "two-storey house extension, black outline strokes with pale blue wall infill on a "
    "plain white background, in the exact style of the conditioning elevation drawing. "
    "Static eye-level wide shot, camera completely locked off, absolutely no camera "
    "movement, no zoom, no pan. In the top-left of frame, a small simple flat-icon sun "
    "sits fixed in place near the horizon the whole time -- it does not move, morph, or "
    "animate in any way. To the right of the house is a simple diagrammatic neighbouring "
    "backyard in the same flat technical line-art style: a low timber paling fence, a "
    "single small stylised tree drawn as a plain outline, and a lawn area. The neighbour's "
    "lawn is tinted a distinct flat translucent red-orange the entire time, clearly marking "
    "it as the zone of concern, separate from the shadow's own colour. Do not render any "
    "text, letters, numbers, or labels anywhere in the new right-hand yard area -- keep "
    "that area purely graphical. Time-lapse effect: over the 8 seconds, a semi-transparent, "
    "dark blue-grey shadow cast by the tall second-storey section grows steadily longer, "
    "starting at the base of the house and extending rightward. The shadow is see-through "
    "enough that the window frames and door outline of the house remain visible underneath "
    "it the whole time, never turning into solid flat blocks. By the end of the clip the "
    "shadow has crossed onto the red-tinted neighbouring lawn, visibly darkening the red "
    "zone where the two overlap. Only the shadow animates -- the sun icon, the house "
    "windows and doors, the fence, and the tree stay perfectly still and undistorted "
    "throughout. The whole video must look like a technical architectural-visualisation "
    "diagram, not a photograph: flat colour fills, no textures, no photorealism, no "
    "gradients, consistent drafting-line aesthetic in every single frame."
)
"""Verbatim copy of the veo lane's `prompt-3.txt` -- the recommended,
stop-bar-clearing recipe (RECOMMENDATION.md). Never edited per-case: see
module docstring for why the conditioning image alone carries the
per-case variation."""

_VEO_LIVE_NEGATIVE_PROMPT: Final[str] = (
    "photorealistic rendering, live-action footage, CCTV or surveillance camera style, "
    "garbled or illegible on-screen text, morphing or melting geometry, camera movement, "
    "black letterbox bars"
)
"""Per the prompt guide's own recommendation (PROMPTING-NOTES.md section
(c)): describe what must not appear, rather than "no"/"don't" phrasing --
naming exactly the artifacts the lane's own iteration loop fixed across
clips 1-2 (morphing sun icon, letterbox bars) so they don't reappear."""


# --- build_conditioning_image: crop a case's own elevation to Veo's input --

_TITLE_BLOCK_CROP_FRACTION: Final[float] = 0.20
"""Fraction of the rendered plan page's height cropped off the bottom
before framing to 16:9 -- a documented heuristic (not a layout parser) for
this project's NSW elevation drawings (see `tests/fixtures/nsw/docs/
elevations.pdf`), which consistently place their title block (address,
architect ABN/email, revision table) along the bottom edge. Chosen against
that exact fixture: at 0.20, the remaining page is already very close to
16:9 on its own, so the further aspect-ratio crop below only trims a
little. Mirrors, in an automated per-case form, the veo lane's own manual
crop of `input-elevation-16x9.png` (PROMPTING-NOTES.md: "cropped to the
North Elevation drawing only, with the title block ... cropped out").
**Known limitation, honestly flagged**: this is a fixed-fraction heuristic,
not a title-block detector -- a plan document laid out differently (a
tighter single elevation, a page with no title block at all) would be
cropped by the same fraction regardless, unlike the lane's own
per-drawing manual crop. Acceptable for this build's one demo-DA-shaped
document family; a future wave ingesting materially different drawing
layouts should replace this with real layout detection."""

_CONDITIONING_ASPECT_RATIO: Final[float] = 16 / 9
_CONDITIONING_MAX_WIDTH_PX: Final[int] = 1600
"""Comfortably under Veo's 20MB image-to-video input limit (PROMPTING-
NOTES.md's model-card spec) while staying sharp enough for the model to
resolve window/door line-work -- matches the same order of magnitude as
`job.pipeline._OVERLAY_STORAGE_MAX_WIDTH_PX` (1280px) used for a
comparable purpose elsewhere in this codebase."""


def build_conditioning_image(page_png_bytes: bytes) -> tuple[bytes, str]:
    """Crop a rendered plan page (`evidence.dossier.RenderedPage.png_bytes`)
    into a 16:9 Veo image-to-video conditioning frame.

    Two steps, in order: (1) crop off the bottom `_TITLE_BLOCK_CROP_
    FRACTION` of the page (removes the title block/address region -- see
    that constant's docstring for the honesty caveat on this being a
    heuristic); (2) centre-crop the remainder to exactly 16:9 -- cropping
    width (centred) if the remaining page is wider than 16:9, or cropping
    height from the top down if it's taller (never re-introducing the
    bottom strip step (1) just removed). Finally downscales to at most
    `_CONDITIONING_MAX_WIDTH_PX` wide if larger.

    Returns `(png_bytes, "image/png")` -- a plain pure function, no I/O, so
    it is exercised directly against synthetic images in tests with zero
    model/network calls.
    """
    image = Image.open(BytesIO(page_png_bytes)).convert("RGB")
    width, height = image.size

    kept_height = max(1, round(height * (1 - _TITLE_BLOCK_CROP_FRACTION)))
    image = image.crop((0, 0, width, kept_height))
    width, height = image.size

    current_aspect = width / height
    if current_aspect > _CONDITIONING_ASPECT_RATIO:
        target_width = max(1, round(height * _CONDITIONING_ASPECT_RATIO))
        left = max(0, (width - target_width) // 2)
        image = image.crop((left, 0, left + target_width, height))
    elif current_aspect < _CONDITIONING_ASPECT_RATIO:
        target_height = max(1, round(width / _CONDITIONING_ASPECT_RATIO))
        image = image.crop((0, 0, width, min(height, target_height)))

    width, height = image.size
    if width > _CONDITIONING_MAX_WIDTH_PX:
        new_height = max(1, round(height * _CONDITIONING_MAX_WIDTH_PX / width))
        image = image.resize((_CONDITIONING_MAX_WIDTH_PX, new_height), Image.Resampling.LANCZOS)

    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue(), "image/png"


# --- the client port + production Vertex implementation ---------------------


class VeoGenerationError(RuntimeError):
    """Raised when a Vertex Veo 3.1 generation operation fails or returns no
    usable video -- always caught by `job.pipeline`'s fully-isolated
    post-step, never allowed to propagate into a tribunal run."""


class VeoLiveClient(Protocol):
    """The narrow port `job.pipeline.RealPipelineRunner`'s judge-gated
    post-step depends on. Tests inject a fake; production wires
    `VertexVeoLiveClient` (see `job.main`'s default pipeline factory)."""

    async def generate_overshadowing_clip(
        self, *, conditioning_image: bytes, conditioning_mime_type: str
    ) -> bytes:
        """Generate one 8s overshadowing-illustration clip conditioned on
        `conditioning_image`, and return its raw mp4 bytes."""
        ...


def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    """`"gs://bucket/some/object.mp4"` -> `("bucket", "some/object.mp4")`."""
    if not uri.startswith("gs://"):
        raise ValueError(f"not a gs:// URI: {uri!r}")
    without_scheme = uri[len("gs://") :]
    bucket, _sep, object_path = without_scheme.partition("/")
    if not bucket or not object_path:
        raise ValueError(f"not a well-formed gs:// URI: {uri!r}")
    return bucket, object_path


async def _real_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class VertexVeoLiveClient:
    """The production `VeoLiveClient`: calls Veo 3.1 via Vertex AI in
    `us-central1` (see module docstring), polling the long-running
    generation operation to completion, then downloading and returning the
    resulting clip's bytes.

    Every Google Cloud dependency is injectable (`genai_client`,
    `storage_client`) so tests exercise this class's own polling/download/
    cleanup logic fully offline against fakes -- see
    `tests/evidence/test_veo_live.py`. Never called by any test with real
    credentials or a real network call.
    """

    def __init__(
        self,
        *,
        project: str = config.GCP_PROJECT,
        location: str = VEO_LIVE_VERTEX_LOCATION,
        bucket_name: str | None = None,
        model: str = VEO_LIVE_MODEL,
        genai_client: genai.Client | None = None,
        storage_client: Any | None = None,
        poll_interval_seconds: float = 5.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._genai_client = genai_client or genai.Client(
            vertexai=True, project=project, location=location
        )
        self._bucket_name = bucket_name or config.GCS_UPLOADS_BUCKET
        self._storage_client = storage_client if storage_client is not None else storage.Client()
        self._model = model
        self._poll_interval_seconds = poll_interval_seconds
        self._sleep = sleep or _real_sleep

    async def generate_overshadowing_clip(
        self, *, conditioning_image: bytes, conditioning_mime_type: str
    ) -> bytes:
        scratch_prefix = f"veo-live-scratch/{uuid.uuid4().hex}"
        output_gcs_uri = f"gs://{self._bucket_name}/{scratch_prefix}/"

        operation = await self._genai_client.aio.models.generate_videos(
            model=self._model,
            prompt=_VEO_LIVE_PROMPT,
            image=types.Image(image_bytes=conditioning_image, mime_type=conditioning_mime_type),
            config=types.GenerateVideosConfig(
                aspect_ratio="16:9",
                resolution="1080p",
                duration_seconds=8,
                generate_audio=False,
                negative_prompt=_VEO_LIVE_NEGATIVE_PROMPT,
                output_gcs_uri=output_gcs_uri,
                number_of_videos=1,
            ),
        )
        while not operation.done:
            await self._sleep(self._poll_interval_seconds)
            operation = await self._genai_client.aio.operations.get(operation)

        if operation.error:
            raise VeoGenerationError(f"Veo generation operation failed: {operation.error!r}")
        response = operation.response
        if response is None or not response.generated_videos:
            raise VeoGenerationError("Veo generation operation returned no video")

        video = response.generated_videos[0].video
        video_uri = video.uri if video is not None else None
        if not video_uri:
            raise VeoGenerationError("Veo generation operation returned a video with no URI")

        clip_bytes = await asyncio.to_thread(self._download_gcs_uri, video_uri)
        await asyncio.to_thread(self._delete_gcs_prefix, scratch_prefix)
        return clip_bytes

    def _download_gcs_uri(self, gcs_uri: str) -> bytes:
        bucket_name, blob_path = _parse_gcs_uri(gcs_uri)
        bucket = self._storage_client.bucket(bucket_name)
        return bucket.blob(blob_path).download_as_bytes()  # type: ignore[no-any-return]

    def _delete_gcs_prefix(self, prefix: str) -> None:
        """Best-effort scratch cleanup -- never allowed to fail the overall
        generation, which has already succeeded by the time this runs; a
        stray few-MB scratch object left behind on a delete failure is a
        housekeeping nit, not a correctness or spend problem."""
        try:
            for blob in self._storage_client.list_blobs(self._bucket_name, prefix=prefix):
                blob.delete()
        except Exception:  # noqa: BLE001 -- cleanup-only, must never raise
            pass


__all__ = [
    "VEO_LIVE_COST_NOTE",
    "VEO_LIVE_DOCUMENT_ID",
    "VEO_LIVE_GENERATING_MESSAGE",
    "VEO_LIVE_MODEL",
    "VEO_LIVE_TIMEOUT_SECONDS",
    "VEO_LIVE_VERTEX_LOCATION",
    "VeoGenerationError",
    "VeoLiveClient",
    "VertexVeoLiveClient",
    "build_conditioning_image",
    "veo_live_enabled",
    "veo_live_max_generations",
]
