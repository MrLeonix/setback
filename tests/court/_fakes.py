"""Shared offline test fixtures for the court package.

`FakeLlm` is a `google.adk.models.BaseLlm` double: it never opens a network
connection and never touches Vertex. Handing it to `google.adk.agents.Agent`
in place of a model-name string is how every test in this package proves the
real ADK graph-construction/parsing mechanics (fan-out, join, tally routing,
the adjudicator's structured output) without a live model call, matching the
proven pattern from `spike-adkCourt.md` (offline-validated separately in this
build's own scratchpad script before being ported here).
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.genai import types
from pydantic import ConfigDict, PrivateAttr


class FakeLlm(BaseLlm):
    """Replays a queue of canned JSON response bodies, one per call.

    The last queued text repeats for any call beyond the queue's length,
    so a test that doesn't care how many times a node runs doesn't need to
    guess an exact count.

    By default no `usage_metadata` is attached to the yielded `LlmResponse`
    (`None`, exactly like an ADK response that never surfaced usage) --
    this is what lets `tests/court/test_graph.py` prove the ledger-truth
    estimation fallback deterministically offline. Pass `usages` (parallel
    to `bodies`, same repeat-last-entry semantics) to instead simulate a
    real usage-reporting call.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    _texts: list[str] = PrivateAttr(default_factory=list)
    _usages: list[types.GenerateContentResponseUsageMetadata | None] = PrivateAttr(
        default_factory=list
    )
    _calls: int = PrivateAttr(default=0)

    def __init__(
        self,
        *,
        model: str,
        bodies: list[dict[str, Any]],
        usages: list[types.GenerateContentResponseUsageMetadata | None] | None = None,
    ) -> None:
        super().__init__(model=model)
        self._texts = [json.dumps(body) for body in bodies]
        self._usages = usages or []
        self._calls = 0

    @property
    def call_count(self) -> int:
        """How many times `generate_content_async` has been invoked."""
        return self._calls

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        index = min(self._calls, len(self._texts) - 1) if self._texts else -1
        usage_index = min(self._calls, len(self._usages) - 1) if self._usages else -1
        self._calls += 1
        text = self._texts[index] if index >= 0 else "{}"
        usage = self._usages[usage_index] if usage_index >= 0 else None
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            usage_metadata=usage,
        )


def review_body(
    *,
    ground_id: str,
    stance: str = "support",
    confidence: float = 0.9,
    cited_anchor_ids: list[str] | None = None,
    rationale: str = "rationale",
) -> dict[str, Any]:
    """A canned `ReviewOutput`-shaped JSON body for a `FakeLlm` reviewer."""
    return {
        "ground_id": ground_id,
        "stance": stance,
        "confidence": confidence,
        "cited_anchor_ids": cited_anchor_ids or [],
        "rationale": rationale,
    }


def adjudicator_body(
    *,
    ground_id: str,
    stance: str = "support",
    confidence: float = 0.9,
    cited_anchor_ids: list[str] | None = None,
    rationale: str = "adjudicated rationale",
) -> dict[str, Any]:
    """A canned `AdjudicatorOutput`-shaped JSON body for a `FakeLlm` adjudicator."""
    return {
        "ground_id": ground_id,
        "stance": stance,
        "confidence": confidence,
        "cited_anchor_ids": cited_anchor_ids or [],
        "rationale": rationale,
    }
