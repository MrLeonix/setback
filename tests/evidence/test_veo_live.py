"""Tests for setback.evidence.veo_live: judge-gated LIVE Veo 3.1
illustration generation (wave 13, founder-authorized).

Fully offline: `build_conditioning_image` is a pure image-processing
function exercised against synthetic PIL images plus the real checked-in
elevations fixture; `VertexVeoLiveClient` is exercised only against
injected fake `genai`/GCS-client doubles -- **the real Vertex Veo API is
never called by any test in this module or anywhere else in this suite**,
per the wave's explicit "never call the real API from tests" instruction.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from setback.evidence.veo_live import (
    VEO_LIVE_COST_NOTE,
    VEO_LIVE_DOCUMENT_ID,
    VEO_LIVE_GENERATING_MESSAGE,
    VeoGenerationError,
    VertexVeoLiveClient,
    _env_flag,
    _parse_gcs_uri,
    build_conditioning_image,
    veo_live_enabled,
    veo_live_max_generations,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "nsw" / "docs"
ELEVATIONS_PDF = FIXTURES / "elevations.pdf"


def _png(width: int, height: int, color: tuple[int, int, int] = (255, 255, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


# --- founder-approved constants (mirrors evidence.illustration's own pins) --


def test_veo_live_cost_note_is_the_founder_approved_text() -> None:
    assert VEO_LIVE_COST_NOTE == (
        "Generated live with Veo 3.1 · US$1.60 · not part of this case's run cost"
    )


def test_veo_live_generating_message_matches_the_brief() -> None:
    assert VEO_LIVE_GENERATING_MESSAGE == (
        "Your illustration is being generated — give it a couple of minutes and refresh."
    )


def test_veo_live_document_id_is_stable() -> None:
    assert VEO_LIVE_DOCUMENT_ID == "veo-live-illustration"


# --- env parsing -------------------------------------------------------------


def test_env_flag_defaults_true_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOME_FLAG", raising=False)
    assert _env_flag("SOME_FLAG", default=True) is True


@pytest.mark.parametrize("raw", ["0", "false", "False", "no", "off", ""])
def test_env_flag_recognises_falsey_strings(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("SOME_FLAG", raw)
    assert _env_flag("SOME_FLAG", default=True) is False


@pytest.mark.parametrize("raw", ["1", "true", "True", "yes", "on"])
def test_env_flag_recognises_truthy_strings(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("SOME_FLAG", raw)
    assert _env_flag("SOME_FLAG", default=False) is True


def test_veo_live_enabled_defaults_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VEO_LIVE_ENABLED", raising=False)
    assert veo_live_enabled() is True


def test_veo_live_enabled_reads_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEO_LIVE_ENABLED", "false")
    assert veo_live_enabled() is False


def test_veo_live_max_generations_defaults_to_ten(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VEO_LIVE_MAX_GENERATIONS", raising=False)
    assert veo_live_max_generations() == 10


def test_veo_live_max_generations_reads_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEO_LIVE_MAX_GENERATIONS", "3")
    assert veo_live_max_generations() == 3


# --- build_conditioning_image: pure crop/resize -------------------------------


def test_build_conditioning_image_returns_png_mime_type() -> None:
    _png_bytes, mime_type = build_conditioning_image(_png(2000, 1400))
    assert mime_type == "image/png"


def test_build_conditioning_image_output_is_close_to_16_9() -> None:
    png_bytes, _mime = build_conditioning_image(_png(2000, 3000))
    image = Image.open(io.BytesIO(png_bytes))
    aspect = image.width / image.height
    assert aspect == pytest.approx(16 / 9, rel=0.02)


def test_build_conditioning_image_output_is_close_to_16_9_for_a_wide_input() -> None:
    png_bytes, _mime = build_conditioning_image(_png(4000, 1200))
    image = Image.open(io.BytesIO(png_bytes))
    aspect = image.width / image.height
    assert aspect == pytest.approx(16 / 9, rel=0.02)


def test_build_conditioning_image_caps_output_width() -> None:
    png_bytes, _mime = build_conditioning_image(_png(6000, 4200))
    image = Image.open(io.BytesIO(png_bytes))
    assert image.width <= 1600


def test_build_conditioning_image_crops_off_a_bottom_title_block_strip() -> None:
    """The bottom strip (a distinct colour standing in for a title block)
    must not survive into the output -- sampling the bottom-most row of the
    cropped result should never see the marker colour."""
    width, height = 2000, 1400
    image = Image.new("RGB", (width, height), (255, 255, 255))
    title_block_height = round(height * 0.20)
    marker = (10, 20, 30)
    for y in range(height - title_block_height, height):
        for x in range(0, width, 50):  # sparse sample write is enough
            image.putpixel((x, y), marker)
    buf = io.BytesIO()
    image.save(buf, format="PNG")

    png_bytes, _mime = build_conditioning_image(buf.getvalue())
    result = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    bottom_row_colors = {
        result.getpixel((x, result.height - 1)) for x in range(0, result.width, 10)
    }
    assert marker not in bottom_row_colors


def test_build_conditioning_image_handles_the_real_elevations_fixture() -> None:
    """Integration-shaped sanity check against the real, checked-in DA
    elevations drawing (the actual conditioning source in production) --
    still fully offline, no model/network call, just PDF rendering + PIL."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(ELEVATIONS_PDF.read_bytes())
    try:
        bitmap = pdf[0].render(scale=300 / 72)
        page_image = bitmap.to_pil()
        buf = io.BytesIO()
        page_image.save(buf, format="PNG")
        page_png_bytes = buf.getvalue()
    finally:
        pdf.close()

    png_bytes, mime_type = build_conditioning_image(page_png_bytes)

    assert mime_type == "image/png"
    result = Image.open(io.BytesIO(png_bytes))
    assert result.width > 0
    assert result.height > 0
    assert (result.width / result.height) == pytest.approx(16 / 9, rel=0.02)


# --- _parse_gcs_uri ------------------------------------------------------------


def test_parse_gcs_uri_splits_bucket_and_object_path() -> None:
    bucket, path = _parse_gcs_uri("gs://my-bucket/some/nested/object.mp4")
    assert bucket == "my-bucket"
    assert path == "some/nested/object.mp4"


def test_parse_gcs_uri_rejects_a_non_gcs_uri() -> None:
    with pytest.raises(ValueError):
        _parse_gcs_uri("https://example.com/not-gcs")


# --- VertexVeoLiveClient: exercised only against injected fakes ---------------


class _FakeBlob:
    def __init__(self, name: str, *, content: bytes | None = None) -> None:
        self.name = name
        self._content = content
        self.deleted = False

    def download_as_bytes(self) -> bytes:
        assert self._content is not None
        return self._content

    def delete(self) -> None:
        self.deleted = True


class _FakeBucket:
    def __init__(self) -> None:
        self.blobs: dict[str, _FakeBlob] = {}

    def blob(self, path: str) -> _FakeBlob:
        return self.blobs.setdefault(path, _FakeBlob(path))


class _FakeStorageClient:
    def __init__(self) -> None:
        self.buckets: dict[str, _FakeBucket] = {}

    def bucket(self, name: str) -> _FakeBucket:
        return self.buckets.setdefault(name, _FakeBucket())

    def list_blobs(self, bucket_name: str, *, prefix: str) -> list[_FakeBlob]:
        bucket = self.buckets.get(bucket_name)
        if bucket is None:
            return []
        return [blob for path, blob in bucket.blobs.items() if path.startswith(prefix)]


class _FakeVideo:
    def __init__(self, uri: str) -> None:
        self.uri = uri


class _FakeGeneratedVideo:
    def __init__(self, uri: str) -> None:
        self.video = _FakeVideo(uri)


class _FakeGenerateVideosResponse:
    def __init__(self, uri: str) -> None:
        self.generated_videos = [_FakeGeneratedVideo(uri)]


class _FakeOperation:
    def __init__(self, *, done: bool, uri: str | None, error: Any = None) -> None:
        self.done = done
        self.error = error
        self.response = _FakeGenerateVideosResponse(uri) if uri is not None else None


class _FakeAsyncModels:
    """Mimics `AsyncModels.generate_videos`: whichever `output_gcs_uri` the
    real call requests (a fresh random scratch prefix each time, per
    `VertexVeoLiveClient`), this fake writes the configured `clip_bytes` to
    exactly that location in `storage_client` -- so a test never has to
    predict the client's own internally-generated `uuid4()` prefix ahead of
    time, and download/cleanup are exercised against a real, consistent
    path."""

    def __init__(
        self,
        operations_sequence: list[_FakeOperation],
        *,
        storage_client: _FakeStorageClient | None = None,
        clip_bytes: bytes = b"clip",
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._operations_sequence = operations_sequence
        self._storage_client = storage_client
        self._clip_bytes = clip_bytes

    async def generate_videos(self, **kwargs: Any) -> _FakeOperation:
        self.calls.append(kwargs)
        if self._storage_client is not None:
            output_gcs_uri: str = kwargs["config"].output_gcs_uri
            video_uri = f"{output_gcs_uri.rstrip('/')}/sample_0.mp4"
            bucket_name, blob_path = _parse_gcs_uri(video_uri)
            self._storage_client.bucket(bucket_name).blobs[blob_path] = _FakeBlob(
                blob_path, content=self._clip_bytes
            )
            # Whichever operation in the sequence is the terminal, response-
            # carrying one (there is at most one) gets its placeholder URI
            # patched to the location this fake just populated -- the
            # earlier not-done operations in a multi-poll sequence carry no
            # response at all, so they are untouched.
            for op in self._operations_sequence:
                if op.response is not None:
                    op.response.generated_videos[0].video.uri = video_uri
        return self._operations_sequence[0]


class _FakeAsyncOperations:
    def __init__(self, operations_sequence: list[_FakeOperation]) -> None:
        self._operations_sequence = operations_sequence
        self._index = 0

    async def get(self, operation: _FakeOperation) -> _FakeOperation:
        self._index += 1
        return self._operations_sequence[min(self._index, len(self._operations_sequence) - 1)]


class _FakeAio:
    def __init__(
        self,
        operations_sequence: list[_FakeOperation],
        *,
        storage_client: _FakeStorageClient | None = None,
        clip_bytes: bytes = b"clip",
    ) -> None:
        self.models = _FakeAsyncModels(
            operations_sequence, storage_client=storage_client, clip_bytes=clip_bytes
        )
        self.operations = _FakeAsyncOperations(operations_sequence)


class _FakeGenaiClient:
    def __init__(
        self,
        operations_sequence: list[_FakeOperation],
        *,
        storage_client: _FakeStorageClient | None = None,
        clip_bytes: bytes = b"clip",
    ) -> None:
        self.aio = _FakeAio(
            operations_sequence, storage_client=storage_client, clip_bytes=clip_bytes
        )


async def _no_sleep(_seconds: float) -> None:
    return None


async def test_vertex_veo_live_client_returns_the_downloaded_clip_bytes() -> None:
    clip_bytes = b"fake-mp4-bytes"
    storage_client = _FakeStorageClient()
    genai_client = _FakeGenaiClient(
        [_FakeOperation(done=True, uri="placeholder")],
        storage_client=storage_client,
        clip_bytes=clip_bytes,
    )

    client = VertexVeoLiveClient(
        bucket_name="bucket-a",
        genai_client=genai_client,  # type: ignore[arg-type]
        storage_client=storage_client,  # type: ignore[arg-type]
        sleep=_no_sleep,
    )

    result = await client.generate_overshadowing_clip(
        conditioning_image=b"fake-png-bytes", conditioning_mime_type="image/png"
    )

    assert result == clip_bytes


async def test_vertex_veo_live_client_polls_until_the_operation_is_done() -> None:
    storage_client = _FakeStorageClient()
    operations_sequence = [
        _FakeOperation(done=False, uri=None),
        _FakeOperation(done=False, uri=None),
        _FakeOperation(done=True, uri="placeholder"),
    ]
    genai_client = _FakeGenaiClient(operations_sequence, storage_client=storage_client)

    client = VertexVeoLiveClient(
        bucket_name="bucket-a",
        genai_client=genai_client,  # type: ignore[arg-type]
        storage_client=storage_client,  # type: ignore[arg-type]
        sleep=_no_sleep,
    )

    result = await client.generate_overshadowing_clip(
        conditioning_image=b"fake-png-bytes", conditioning_mime_type="image/png"
    )

    assert result == b"clip"
    assert genai_client.aio.operations._index >= 2


async def test_vertex_veo_live_client_cleans_up_the_scratch_object() -> None:
    storage_client = _FakeStorageClient()
    genai_client = _FakeGenaiClient(
        [_FakeOperation(done=True, uri="placeholder")], storage_client=storage_client
    )

    client = VertexVeoLiveClient(
        bucket_name="bucket-a",
        genai_client=genai_client,  # type: ignore[arg-type]
        storage_client=storage_client,  # type: ignore[arg-type]
        sleep=_no_sleep,
    )

    await client.generate_overshadowing_clip(
        conditioning_image=b"fake-png-bytes", conditioning_mime_type="image/png"
    )

    ((_path, blob),) = storage_client.bucket("bucket-a").blobs.items()
    assert blob.deleted is True


async def test_vertex_veo_live_client_raises_veo_generation_error_on_an_operation_error() -> None:
    genai_client = _FakeGenaiClient(
        [_FakeOperation(done=True, uri=None, error={"message": "nope"})]
    )
    storage_client = _FakeStorageClient()

    client = VertexVeoLiveClient(
        bucket_name="bucket-a",
        genai_client=genai_client,  # type: ignore[arg-type]
        storage_client=storage_client,  # type: ignore[arg-type]
        sleep=_no_sleep,
    )

    with pytest.raises(VeoGenerationError):
        await client.generate_overshadowing_clip(
            conditioning_image=b"fake-png-bytes", conditioning_mime_type="image/png"
        )


async def test_vertex_veo_live_client_raises_veo_generation_error_on_no_generated_video() -> None:
    genai_client = _FakeGenaiClient([_FakeOperation(done=True, uri=None, error=None)])
    storage_client = _FakeStorageClient()

    client = VertexVeoLiveClient(
        bucket_name="bucket-a",
        genai_client=genai_client,  # type: ignore[arg-type]
        storage_client=storage_client,  # type: ignore[arg-type]
        sleep=_no_sleep,
    )

    with pytest.raises(VeoGenerationError):
        await client.generate_overshadowing_clip(
            conditioning_image=b"fake-png-bytes", conditioning_mime_type="image/png"
        )


async def test_vertex_veo_live_client_requests_video_only_1080p_16_9_8s() -> None:
    storage_client = _FakeStorageClient()
    genai_client = _FakeGenaiClient(
        [_FakeOperation(done=True, uri="placeholder")], storage_client=storage_client
    )

    client = VertexVeoLiveClient(
        bucket_name="bucket-a",
        genai_client=genai_client,  # type: ignore[arg-type]
        storage_client=storage_client,  # type: ignore[arg-type]
        sleep=_no_sleep,
    )

    await client.generate_overshadowing_clip(
        conditioning_image=b"fake-png-bytes", conditioning_mime_type="image/png"
    )

    ((call,),) = [(c,) for c in genai_client.aio.models.calls]
    assert call["model"] == "veo-3.1-generate-001"
    config = call["config"]
    assert config.aspect_ratio == "16:9"
    assert config.resolution == "1080p"
    assert config.duration_seconds == 8
    assert config.generate_audio is False
