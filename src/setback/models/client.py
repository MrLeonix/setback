"""The sole model call site for Setback.

Every model invocation in the system — interview, adjudication bench, and
clerical extraction — goes through :class:`ModelClient`. It dispatches to
one of two transports depending on the tier:

* Gemini tiers (``gemini-3.5-flash-lite``, ``gemini-3.7-flash``) go through
  the ``google-genai`` SDK against Vertex AI (ADC, ``location="global"``,
  no API keys).
* The Gemma MaaS tier (``gemma-4-26b-a4b-it-maas``, matched by the
  ``-maas`` model-name suffix) goes through Vertex's OpenAI-compatible
  chat-completions endpoint over plain ``httpx``, since it is not served by
  the genai SDK.

Both paths validate their reply into a caller-supplied Pydantic model and
retry 429s (Vertex Dynamic Shared Quota has no per-project quota — retry is
the only remedy) and transient 5xx with exponential backoff and jitter.
Thinking tokens are surfaced on :class:`TokenUsage` because they bill at the
output-token rate.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from setback.config import GCP_PROJECT, VERTEX_LOCATION, ModelConfig

T = TypeVar("T", bound=BaseModel)

_MAAS_MODEL_SUFFIX = "-maas"
"""Model names ending in this route through the OpenAI-compatible endpoint."""

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class ModelCallError(RuntimeError):
    """Raised when a model call fails after exhausting all retry attempts,
    or succeeds transport-wise but yields no valid structured output."""


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff with jitter for retryable model-call failures."""

    max_attempts: int = 5
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 20.0
    jitter_seconds: float = 0.5

    def delay_for(self, attempt: int) -> float:
        """The backoff delay, in seconds, before retrying after `attempt` (0-indexed)."""
        exponential = min(self.max_delay_seconds, self.base_delay_seconds * (2**attempt))
        return float(exponential + random.uniform(0, self.jitter_seconds))


@dataclass(frozen=True)
class TokenUsage:
    """Token accounting for one model call. Thinking tokens bill as output tokens."""

    prompt_tokens: int
    output_tokens: int
    thinking_tokens: int = 0
    estimated: bool = False
    """True when these figures are a text-length estimate rather than a
    model-reported count -- set by a caller (e.g. `court.graph`, when the
    ADK event stream carries no `usage_metadata` for a stage) that had no
    real usage figure to fall back on. `ModelClient` itself always reports
    real, non-estimated usage; this flag exists for callers layered on top
    of a transport that doesn't always expose it."""

    @property
    def billable_output_tokens(self) -> int:
        """Output tokens plus thinking tokens, which bill at the output rate."""
        return self.output_tokens + self.thinking_tokens

    @property
    def total_tokens(self) -> int:
        """Prompt tokens plus all billable output tokens."""
        return self.prompt_tokens + self.billable_output_tokens


@dataclass(frozen=True)
class ModelResult[T]:
    """A validated structured-output call result, paired with its token usage."""

    output: T
    usage: TokenUsage
    model: str


def _maas_base_url(project: str, location: str) -> str:
    """The Vertex OpenAI-compatible endpoint base URL for `project`/`location`."""
    if location == "global":
        host = "aiplatform.googleapis.com"
    else:
        host = f"{location}-aiplatform.googleapis.com"
    return f"https://{host}/v1/projects/{project}/locations/{location}/endpoints/openapi"


def _default_token_provider() -> Callable[[], str]:
    """Builds an ADC-backed bearer-token provider, refreshing lazily on use."""
    import google.auth
    from google.auth.transport.requests import Request

    credentials, _ = google.auth.default()

    def provider() -> str:
        if not credentials.valid:
            credentials.refresh(Request())  # type: ignore[no-untyped-call]
        token = credentials.token
        assert isinstance(token, str)
        return token

    return provider


def _status_from_exception(exc: BaseException) -> int | None:
    """The HTTP status code carried by `exc`, if it is a recognised transport error."""
    if isinstance(exc, errors.APIError):
        return exc.code
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    return None


async def _call_with_retry[R](
    call: Callable[[], Awaitable[R]],
    *,
    policy: RetryPolicy,
    sleep: Callable[[float], Awaitable[None]],
) -> R:
    """Runs `call`, retrying on retryable statuses per `policy` until it succeeds
    or exhausts its attempts, in which case :class:`ModelCallError` is raised."""
    last_exc: BaseException | None = None
    for attempt in range(policy.max_attempts):
        try:
            return await call()
        except BaseException as exc:
            status = _status_from_exception(exc)
            if status is None or status not in _RETRYABLE_STATUS_CODES:
                raise
            last_exc = exc
            if attempt == policy.max_attempts - 1:
                break
            await sleep(policy.delay_for(attempt))
    raise ModelCallError(
        f"model call did not succeed after {policy.max_attempts} attempts"
    ) from last_exc


class ModelClient:
    """The sole call site for Gemini (Vertex) and Gemma MaaS model invocations."""

    def __init__(
        self,
        *,
        project: str = GCP_PROJECT,
        location: str = VERTEX_LOCATION,
        genai_client: genai.Client | None = None,
        maas_http_client: httpx.AsyncClient | None = None,
        token_provider: Callable[[], str] | None = None,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """Construct a client. All dependencies are injectable so tests can run
        fully offline against fakes/respx instead of live Vertex AI / ADC."""
        self._genai_client = genai_client or genai.Client(
            vertexai=True, project=project, location=location
        )
        self._maas_http_client = maas_http_client or httpx.AsyncClient(
            base_url=_maas_base_url(project, location)
        )
        self._token_provider = token_provider or _default_token_provider()
        self._retry_policy = retry_policy or RetryPolicy()
        self._sleep = sleep or _real_sleep

    async def generate(
        self,
        tier: ModelConfig,
        prompt: str,
        response_model: type[T],
        *,
        system_instruction: str | None = None,
        temperature: float | None = None,
    ) -> ModelResult[T]:
        """Call `tier` with `prompt`, validating the reply as `response_model`.

        Routes to the Gemma MaaS OpenAI-compatible endpoint when `tier.model`
        ends in ``-maas``, otherwise to the Gemini Vertex SDK.

        `temperature` is left unset (the model's own default) unless a
        caller passes one explicitly -- e.g. `evidence/grounding.py`'s
        documented need for `temperature=0.0`, matching the proven
        grounding spike's direct `google-genai` call.
        """
        if tier.model.endswith(_MAAS_MODEL_SUFFIX):
            return await self._generate_maas(
                tier,
                prompt,
                response_model,
                system_instruction=system_instruction,
                temperature=temperature,
            )
        return await self._generate_gemini(
            tier,
            prompt,
            response_model,
            system_instruction=system_instruction,
            temperature=temperature,
        )

    async def _generate_gemini(
        self,
        tier: ModelConfig,
        prompt: str,
        response_model: type[T],
        *,
        system_instruction: str | None,
        temperature: float | None,
    ) -> ModelResult[T]:
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=response_model,
            thinking_config=types.ThinkingConfig(thinking_level=tier.thinking_level),
            temperature=temperature,
        )

        async def call() -> types.GenerateContentResponse:
            return await self._genai_client.aio.models.generate_content(
                model=tier.model, contents=prompt, config=config
            )

        response = await _call_with_retry(call, policy=self._retry_policy, sleep=self._sleep)

        parsed = response.parsed
        if not isinstance(parsed, response_model):
            raise ModelCallError(
                f"model {tier.model!r} returned no parseable {response_model.__name__}"
            )

        usage_metadata = response.usage_metadata
        usage = TokenUsage(
            prompt_tokens=(usage_metadata.prompt_token_count or 0) if usage_metadata else 0,
            output_tokens=(usage_metadata.candidates_token_count or 0) if usage_metadata else 0,
            thinking_tokens=(usage_metadata.thoughts_token_count or 0) if usage_metadata else 0,
        )
        return ModelResult(output=parsed, usage=usage, model=tier.model)

    async def _generate_maas(
        self,
        tier: ModelConfig,
        prompt: str,
        response_model: type[T],
        *,
        system_instruction: str | None,
        temperature: float | None,
    ) -> ModelResult[T]:
        messages: list[dict[str, str]] = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        schema_hint = (
            "Respond with a single JSON object matching this JSON schema, and "
            f"nothing else: {response_model.model_json_schema()}"
        )
        messages.append({"role": "user", "content": f"{prompt}\n\n{schema_hint}"})

        payload: dict[str, object] = {
            "model": tier.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        if temperature is not None:
            payload["temperature"] = temperature
        headers = {"Authorization": f"Bearer {self._token_provider()}"}

        async def call() -> httpx.Response:
            response = await self._maas_http_client.post(
                "/chat/completions", json=payload, headers=headers
            )
            response.raise_for_status()
            return response

        response = await _call_with_retry(call, policy=self._retry_policy, sleep=self._sleep)

        body = response.json()
        content = body["choices"][0]["message"]["content"]
        output = response_model.model_validate_json(content)

        usage_body = body.get("usage", {})
        usage = TokenUsage(
            prompt_tokens=usage_body.get("prompt_tokens", 0),
            output_tokens=usage_body.get("completion_tokens", 0),
        )
        return ModelResult(output=output, usage=usage, model=tier.model)


async def _real_sleep(seconds: float) -> None:
    """The default `sleep` dependency: a thin wrapper so it can be swapped in tests."""
    import asyncio

    await asyncio.sleep(seconds)
