"""Tests for setback.models.client: the sole model call site.

Fully offline: Gemini calls go through a fake object shaped like
`genai.Client` (only `.aio.models.generate_content` is exercised); Gemma
MaaS calls go through respx against the real httpx transport. No network,
no ADC, no real credentials — a fake `token_provider` stands in for auth.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
import respx
from google.genai import errors
from pydantic import BaseModel

from setback.config import BENCH, CLERK, INTERVIEW
from setback.models.client import (
    ModelCallError,
    ModelClient,
    RetryPolicy,
    TokenUsage,
    _maas_base_url,
)


class SampleOutput(BaseModel):
    """A minimal structured-output shape used only by these tests."""

    answer: str


class _FakeUsage:
    def __init__(self, prompt_tokens: int, output_tokens: int, thinking_tokens: int) -> None:
        self.prompt_token_count = prompt_tokens
        self.candidates_token_count = output_tokens
        self.thoughts_token_count = thinking_tokens


class _FakeResponse:
    def __init__(self, parsed: Any, usage: _FakeUsage | None) -> None:
        self.parsed = parsed
        self.usage_metadata = usage


class _FakeAsyncModels:
    """Records every call and replays queued responses/exceptions in order."""

    def __init__(self, items: list[Any]) -> None:
        self._items = list(items)
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, *, model: str, contents: Any, config: Any) -> _FakeResponse:
        self.calls.append({"model": model, "contents": contents, "config": config})
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _FakeAio:
    def __init__(self, models: _FakeAsyncModels) -> None:
        self.models = models


class _FakeGenaiClient:
    def __init__(self, models: _FakeAsyncModels) -> None:
        self.aio = _FakeAio(models)


def _no_sleep_recorder() -> tuple[Callable[[float], Awaitable[None]], list[float]]:
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    return sleep, delays


# --- Gemini dispatch ---------------------------------------------------------


async def test_generate_routes_interview_tier_through_gemini_and_parses_output() -> None:
    fake_models = _FakeAsyncModels([_FakeResponse(SampleOutput(answer="ok"), _FakeUsage(10, 5, 0))])
    client = ModelClient(genai_client=_FakeGenaiClient(fake_models))

    result = await client.generate(INTERVIEW, "hello", SampleOutput)

    assert result.output == SampleOutput(answer="ok")
    assert result.model == INTERVIEW.model
    assert result.usage == TokenUsage(prompt_tokens=10, output_tokens=5, thinking_tokens=0)
    assert fake_models.calls[0]["model"] == "gemini-3.5-flash-lite"
    assert fake_models.calls[0]["config"].thinking_config.thinking_level == INTERVIEW.thinking_level


async def test_generate_omits_temperature_from_gemini_config_by_default() -> None:
    """Unset by default -- preserves the model's own default temperature,
    matching this client's behaviour before the `temperature` parameter
    existed (no existing caller should see any change)."""
    fake_models = _FakeAsyncModels([_FakeResponse(SampleOutput(answer="ok"), _FakeUsage(1, 1, 0))])
    client = ModelClient(genai_client=_FakeGenaiClient(fake_models))

    await client.generate(INTERVIEW, "hello", SampleOutput)

    assert fake_models.calls[0]["config"].temperature is None


async def test_generate_passes_explicit_temperature_to_gemini_config() -> None:
    """Grounding (`evidence/grounding.py`) needs `temperature=0.0` per the
    spike; this is the parameter that closes that documented gap."""
    fake_models = _FakeAsyncModels([_FakeResponse(SampleOutput(answer="ok"), _FakeUsage(1, 1, 0))])
    client = ModelClient(genai_client=_FakeGenaiClient(fake_models))

    await client.generate(INTERVIEW, "hello", SampleOutput, temperature=0.0)

    assert fake_models.calls[0]["config"].temperature == 0.0


async def test_generate_counts_thinking_tokens_for_bench_tier() -> None:
    fake_models = _FakeAsyncModels(
        [_FakeResponse(SampleOutput(answer="ok"), _FakeUsage(20, 8, 486))]
    )
    client = ModelClient(genai_client=_FakeGenaiClient(fake_models))

    result = await client.generate(BENCH, "hello", SampleOutput)

    assert result.usage.thinking_tokens == 486
    assert result.usage.billable_output_tokens == 8 + 486


async def test_generate_raises_when_response_not_parseable() -> None:
    fake_models = _FakeAsyncModels([_FakeResponse(None, _FakeUsage(1, 1, 0))])
    client = ModelClient(genai_client=_FakeGenaiClient(fake_models))

    with pytest.raises(ModelCallError):
        await client.generate(INTERVIEW, "hello", SampleOutput)


# --- multimodal (image) content ----------------------------------------------
#
# Grounding (`evidence/grounding.py`) is a vision task -- both its describe
# and ground stages must actually send the rendered page's image bytes to
# the model, not text alone. Before this parameter existed, no call site in
# the codebase ever attached image content, so a "grounding" call was pure
# text -- the model was never shown the page it was supposedly locating
# elements on.


async def test_generate_with_no_images_sends_the_prompt_as_plain_text() -> None:
    """Every existing (non-vision) call site must see zero behaviour change:
    `contents` stays a bare string when `images` is omitted."""
    fake_models = _FakeAsyncModels([_FakeResponse(SampleOutput(answer="ok"), _FakeUsage(1, 1, 0))])
    client = ModelClient(genai_client=_FakeGenaiClient(fake_models))

    await client.generate(INTERVIEW, "hello", SampleOutput)

    assert fake_models.calls[0]["contents"] == "hello"


async def test_generate_with_images_sends_multimodal_parts_including_the_prompt_text() -> None:
    from google.genai import types

    fake_models = _FakeAsyncModels([_FakeResponse(SampleOutput(answer="ok"), _FakeUsage(1, 1, 0))])
    client = ModelClient(genai_client=_FakeGenaiClient(fake_models))

    await client.generate(
        INTERVIEW, "locate the window", SampleOutput, images=[(b"\x89PNG...", "image/png")]
    )

    contents = fake_models.calls[0]["contents"]
    assert isinstance(contents, list)
    assert all(isinstance(part, types.Part) for part in contents)
    image_parts = [p for p in contents if p.inline_data is not None]
    text_parts = [p for p in contents if p.text is not None]
    assert len(image_parts) == 1
    assert image_parts[0].inline_data.data == b"\x89PNG..."
    assert image_parts[0].inline_data.mime_type == "image/png"
    assert text_parts == [types.Part(text="locate the window")]


async def test_generate_with_multiple_images_sends_every_one() -> None:
    fake_models = _FakeAsyncModels([_FakeResponse(SampleOutput(answer="ok"), _FakeUsage(1, 1, 0))])
    client = ModelClient(genai_client=_FakeGenaiClient(fake_models))

    await client.generate(
        INTERVIEW,
        "compare",
        SampleOutput,
        images=[(b"one", "image/png"), (b"two", "image/png")],
    )

    contents = fake_models.calls[0]["contents"]
    image_parts = [p for p in contents if p.inline_data is not None]
    assert [p.inline_data.data for p in image_parts] == [b"one", b"two"]


async def test_generate_rejects_images_on_the_maas_tier() -> None:
    """Gemma MaaS is the OpenAI-compatible text endpoint used only by
    `setback.clerk` -- it never carries image content, so passing `images`
    to it is a caller bug, not something to silently ignore."""
    client = ModelClient(
        maas_http_client=httpx.AsyncClient(base_url="https://example.invalid"),
        token_provider=lambda: "fake-token",
    )

    with pytest.raises(ModelCallError):
        await client.generate(CLERK, "hello", SampleOutput, images=[(b"x", "image/png")])


# --- Retry behaviour ---------------------------------------------------------


async def test_retries_once_on_429_then_succeeds() -> None:
    fake_models = _FakeAsyncModels(
        [
            errors.ClientError(429, {"message": "rate limited", "status": "RESOURCE_EXHAUSTED"}),
            _FakeResponse(SampleOutput(answer="ok"), _FakeUsage(1, 1, 0)),
        ]
    )
    sleep, delays = _no_sleep_recorder()
    client = ModelClient(
        genai_client=_FakeGenaiClient(fake_models),
        retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=1.0, jitter_seconds=0.0),
        sleep=sleep,
    )

    result = await client.generate(INTERVIEW, "hello", SampleOutput)

    assert result.output.answer == "ok"
    assert len(fake_models.calls) == 2
    assert delays == [1.0]


async def test_retries_on_transient_5xx() -> None:
    fake_models = _FakeAsyncModels(
        [
            errors.ServerError(503, {"message": "unavailable", "status": "UNAVAILABLE"}),
            _FakeResponse(SampleOutput(answer="ok"), _FakeUsage(1, 1, 0)),
        ]
    )
    sleep, _ = _no_sleep_recorder()
    client = ModelClient(genai_client=_FakeGenaiClient(fake_models), sleep=sleep)

    result = await client.generate(INTERVIEW, "hello", SampleOutput)

    assert result.output.answer == "ok"


async def test_exhausting_retries_raises_model_call_error() -> None:
    fake_models = _FakeAsyncModels(
        [
            errors.ServerError(500, {"message": "err", "status": "INTERNAL"}),
            errors.ServerError(500, {"message": "err", "status": "INTERNAL"}),
        ]
    )
    sleep, delays = _no_sleep_recorder()
    client = ModelClient(
        genai_client=_FakeGenaiClient(fake_models),
        retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0.1, jitter_seconds=0.0),
        sleep=sleep,
    )

    with pytest.raises(ModelCallError):
        await client.generate(INTERVIEW, "hello", SampleOutput)

    assert len(fake_models.calls) == 2
    assert len(delays) == 1


async def test_non_retryable_error_propagates_without_retry() -> None:
    fake_models = _FakeAsyncModels(
        [errors.ClientError(400, {"message": "bad request", "status": "INVALID_ARGUMENT"})]
    )
    sleep, delays = _no_sleep_recorder()
    client = ModelClient(genai_client=_FakeGenaiClient(fake_models), sleep=sleep)

    with pytest.raises(errors.ClientError):
        await client.generate(INTERVIEW, "hello", SampleOutput)

    assert len(fake_models.calls) == 1
    assert delays == []


def test_retry_policy_backoff_is_exponential_and_capped() -> None:
    policy = RetryPolicy(
        max_attempts=5, base_delay_seconds=1.0, max_delay_seconds=4.0, jitter_seconds=0.0
    )

    assert policy.delay_for(0) == 1.0
    assert policy.delay_for(1) == 2.0
    assert policy.delay_for(2) == 4.0
    assert policy.delay_for(3) == 4.0  # capped


def test_retry_policy_adds_bounded_jitter() -> None:
    policy = RetryPolicy(base_delay_seconds=1.0, jitter_seconds=0.5)

    delays = {policy.delay_for(0) for _ in range(50)}

    assert all(1.0 <= d < 1.5 for d in delays)
    assert len(delays) > 1  # jitter actually varies


# --- Gemma MaaS dispatch (OpenAI-compatible endpoint, via httpx/respx) -------


async def test_maas_base_url_for_global_location() -> None:
    assert _maas_base_url("my-proj", "global") == (
        "https://aiplatform.googleapis.com/v1/projects/my-proj/locations/global/endpoints/openapi"
    )


async def test_maas_base_url_for_regional_location() -> None:
    assert _maas_base_url("my-proj", "us-central1") == (
        "https://us-central1-aiplatform.googleapis.com/v1/projects/my-proj/"
        "locations/us-central1/endpoints/openapi"
    )


@respx.mock
async def test_generate_routes_clerk_tier_through_maas_openai_endpoint() -> None:
    url = _maas_base_url("test-project", "global") + "/chat/completions"
    route = respx.post(url).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"answer": "ok"}'}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4},
            },
        )
    )
    client = ModelClient(
        project="test-project",
        location="global",
        token_provider=lambda: "fake-token",
    )

    result = await client.generate(CLERK, "hello", SampleOutput)

    assert result.output == SampleOutput(answer="ok")
    assert result.usage == TokenUsage(prompt_tokens=12, output_tokens=4, thinking_tokens=0)
    assert route.calls.last.request.headers["authorization"] == "Bearer fake-token"
    assert "temperature" not in route.calls.last.request.content.decode()


@respx.mock
async def test_generate_sends_publisher_qualified_model_id_to_maas_payload() -> None:
    """Vertex's OpenAI-compatible endpoint 400s on a bare model id
    ("Malformed publisher model (`model`: gemma-4-26b-a4b-it-maas) ...
    expected '<publisher>/<model>'") -- the payload must carry the
    publisher-qualified form, even though `config.CLERK.model` and
    `ModelResult.model` (used for ledger cost lookup) stay unqualified."""
    url = _maas_base_url("test-project", "global") + "/chat/completions"
    route = respx.post(url).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"answer": "ok"}'}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )
    )
    client = ModelClient(
        project="test-project",
        location="global",
        token_provider=lambda: "fake-token",
    )

    result = await client.generate(CLERK, "hello", SampleOutput)

    import json as _json

    body = _json.loads(route.calls.last.request.content.decode())
    assert body["model"] == "google/gemma-4-26b-a4b-it-maas"
    # The unqualified id is still what ledger.cost_for's pricing table keys on.
    assert result.model == "gemma-4-26b-a4b-it-maas"


@respx.mock
async def test_generate_passes_explicit_temperature_to_maas_payload() -> None:
    url = _maas_base_url("test-project", "global") + "/chat/completions"
    route = respx.post(url).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"answer": "ok"}'}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )
    )
    client = ModelClient(
        project="test-project",
        location="global",
        token_provider=lambda: "fake-token",
    )

    await client.generate(CLERK, "hello", SampleOutput, temperature=0.0)

    import json as _json

    body = _json.loads(route.calls.last.request.content.decode())
    assert body["temperature"] == 0.0


@respx.mock
async def test_maas_retries_on_429_then_succeeds() -> None:
    url = _maas_base_url("test-project", "global") + "/chat/completions"
    route = respx.post(url).mock(
        side_effect=[
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": '{"answer": "ok"}'}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            ),
        ]
    )
    sleep, _ = _no_sleep_recorder()
    client = ModelClient(
        project="test-project",
        location="global",
        token_provider=lambda: "fake-token",
        sleep=sleep,
    )

    result = await client.generate(CLERK, "hello", SampleOutput)

    assert result.output.answer == "ok"
    assert route.call_count == 2


def test_token_usage_billable_output_includes_thinking() -> None:
    usage = TokenUsage(prompt_tokens=100, output_tokens=50, thinking_tokens=25)

    assert usage.billable_output_tokens == 75
    assert usage.total_tokens == 175


def test_token_usage_defaults_to_not_estimated() -> None:
    """A real, model-reported usage figure is the default assumption --
    `estimated=True` is something a caller has to opt into explicitly when
    it had to fall back to a token-count guess (see `court.graph`)."""
    usage = TokenUsage(prompt_tokens=1, output_tokens=1)

    assert usage.estimated is False
