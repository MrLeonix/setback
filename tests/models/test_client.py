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
