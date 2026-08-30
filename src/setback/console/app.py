"""The resident-facing FastAPI console: `setback-console`.

Wiring only in this work package (0 live-model-call budget in its tests) --
`create_app` is a pure factory over injected ports, so every route is fully
testable against `InMemoryCaseStore` and a fake `QuestionComposer` with no
network, no ADC, no Vertex AI, no live Firestore. The module-level `app`
object at the bottom wires the real dependencies for `uvicorn
setback.console.app:app` (the Cloud Run Service entrypoint); constructing
those (a `FirestoreCaseStore`, a `ModelClient`) only builds client objects,
it never makes a network call by itself, so importing this module for
testing is safe -- tests should still prefer `create_app` directly rather
than importing `app`.

Routes
------
``POST /api/cases``                                  create (or resume) a case
``GET  /api/cases/{case_id}/interview``               current transcript, auto-starting
``POST /api/cases/{case_id}/interview``               submit a resident answer
``POST /api/cases/{case_id}/documents``                upload a photo/document (size-capped)
``POST /api/cases/{case_id}/tribunal``                 start the tribunal job
``GET  /api/cases/{case_id}/events``                   SSE stream of case events
``POST /api/cases/{case_id}/grounds/{ground_id}/feedback``   capture refusal pushback
``GET  /``                                             docket board (via `CaseStore.list_cases`)
``GET  /cases/{case_id}``                              the case page
``GET  /static/*``                                     app.js / style.css

The docket board renders `CaseStore.list_cases`, not an in-process
registry -- it survives a console restart/redeploy and reflects every case
across every console instance, not just the one that happened to create it.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import secrets
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol
from urllib.parse import quote as _urlquote
from zoneinfo import ZoneInfo

import segno
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from setback import config
from setback.clerk import DocumentKind, classify_concern, redact_personal_information
from setback.clerk import _classify_document_by_keywords as _classify_document_kind_offline
from setback.console.guards import (
    PRIVILEGED_COOKIE_MAX_AGE_SECONDS,
    PRIVILEGED_COOKIE_NAME,
    PUBLIC_SPEND_CEILING_USD,
    PUBLIC_TURN_COST_ESTIMATE_USD,
    CachedGuardTotalsReader,
    enforce_concurrent_tribunal_cap,
    enforce_daily_spend_budget,
    is_privileged_request,
    is_public_guard_paused,
    per_case_feedback_cap_guard,
    per_case_interview_turn_cap_guard,
    per_case_interview_turn_guard,
    per_case_upload_cap_guard,
    per_client_daily_case_cap_guard,
    per_ip_case_creation_guard,
    privileged_cookie_value,
    public_guard_dependency,
    record_threshold_events_if_crossed,
)
from setback.evidence.dossier import ProvenanceGrade
from setback.evidence.illustration import (
    ILLUSTRATION_COST_NOTE,
    ILLUSTRATION_EXPLAINER,
    ILLUSTRATION_LABEL,
    simulation_clip_for_case,
)
from setback.evidence.overlays import ROLE_CSS_CLASS_SUFFIX, ROLE_LEGEND_TEXT, OverlayRole
from setback.ingest.tracker import (
    DocumentNotFoundError,
    DocumentSource,
    EvidenceUploadStore,
    ExhibitedDocument,
    UserUploadedDocumentSource,
)
from setback.interview.flow import (
    ConcernNormaliser,
    ConcernType,
    InterviewFlow,
    InterviewStage,
    InterviewTurn,
    ModelConcernNormaliser,
    ModelQuestionComposer,
    QuestionComposer,
    RaisedConcern,
    capture_refusal_feedback,
)
from setback.models.client import ModelClient
from setback.state.firestore import (
    CaseEvent,
    CaseNotFoundError,
    CaseRecord,
    CaseStore,
    GroundRecord,
    GroundStatus,
)
from setback.state.guard_store import (
    GuardCounterStore,
    GuardTotals,
    GuardTotalsStore,
    InMemoryGuardCounterStore,
    InMemoryGuardTotalsStore,
)
from setback.state.ledger import Ledger

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_DEFAULT_MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB

# Magic-byte signatures for the only upload kinds the console's own upload
# widget offers (`accept="image/*,application/pdf"`). Keyed by the exact
# content-type this app will trust; the client-supplied `UploadFile.
# content_type` header is never trusted on its own (P0 security fix: a
# same-origin stored-XSS path existed where an attacker-controlled
# Content-Type header, echoed back verbatim by `get_uploaded_document`,
# could get arbitrary bytes served -- and rendered -- as `text/html`).
_UPLOAD_MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
)


def _sniff_upload_content_type(content: bytes) -> str | None:
    """Determine the real content type of an uploaded file from its own
    bytes, never from the client-supplied header. Returns `None` for
    anything that isn't one of the image/PDF kinds this app accepts --
    the caller must reject those, not fall back to trusting the header."""
    for signature, content_type in _UPLOAD_MAGIC_SIGNATURES:
        if content.startswith(signature):
            return content_type
    if content[:12].startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    return None


_CONCERN_CATEGORY: Mapping[ConcernType, str] = {
    ConcernType.HEIGHT_BULK: "epi_dcp_provisions",
    ConcernType.PRIVACY_OVERLOOKING: "environmental_and_social_impacts",
    ConcernType.OVERSHADOWING: "environmental_and_social_impacts",
    ConcernType.TREES_LANDSCAPE: "environmental_and_social_impacts",
    ConcernType.TRAFFIC_PARKING: "environmental_and_social_impacts",
    ConcernType.HERITAGE_CHARACTER: "epi_dcp_provisions",
    ConcernType.VIEW_LOSS: "private_view_loss",
    ConcernType.PROPERTY_VALUE: "property_value",
    ConcernType.NOISE: "environmental_and_social_impacts",
    ConcernType.OTHER: "environmental_and_social_impacts",
}
"""Maps the interview's light keyword-matched `ConcernType` triage onto the
s4.15 category the gate (`gate.relevance`) rules on. This is a deliberate,
demo-scope simplification -- picking the *closest* statutory head (or the
matching non-planning ground) for each concern type, so every confirmed
concern reaches the gate as a real candidate ground rather than being
silently dropped. `HEIGHT_BULK`/`HERITAGE_CHARACTER` map to
`epi_dcp_provisions` since this build's zoning controls (a height limit, a
heritage flag) are LEP/DCP-hooked, citable clauses, not bare impacts;
`VIEW_LOSS` maps to the explicit `private_view_loss` non-planning ground
since a bare view-loss complaint has no control hook by default."""


class JobTrigger(Protocol):
    """Starts the `setback-tribunal` Cloud Run Job for a case."""

    async def trigger(self, case_id: str) -> None: ...


class LoggingJobTrigger:
    """A `JobTrigger` test double: records that a trigger was requested
    in-process without invoking a real Cloud Run Jobs execution.

    Used by tests and by `SETBACK_LOCAL_TRIBUNAL=1` mode's sibling
    `LocalPipelineJobTrigger` below -- `RealJobTrigger` is the production
    default (see `_build_production_app`). The console route that calls a
    `JobTrigger` always records the request as a durable case event
    regardless of which one is wired in, so a request is never silently
    lost even if the trigger itself does nothing.
    """

    def __init__(self) -> None:
        self.triggered_case_ids: list[str] = []

    async def trigger(self, case_id: str) -> None:
        self.triggered_case_ids.append(case_id)


_TRIBUNAL_JOB_NAME = "setback-tribunal"
"""Matches `deploy.sh`'s `TRIBUNAL_JOB` -- the one Cloud Run Job resource
every case's tribunal run executes against, distinguished per run only by
the `CASE_ID` environment override `RealJobTrigger` sets below."""


class RealJobTrigger:
    """The production `JobTrigger`: starts a real `setback-tribunal` Cloud
    Run Job execution via the `google-cloud-run` client, overriding the
    container's `CASE_ID` environment variable per execution so one
    deployed Job resource serves every case.

    `google-cloud-run` was added to `pyproject.toml` at this wave's
    integration checkpoint (WP-B's dependency report). Importing
    `google.cloud.run_v2` is still deferred to the moment a real client is
    actually needed (`_build_client`), never at import time or construction
    time, so this module keeps importing cleanly and this class keeps being
    constructible even in an environment without network access. The
    request itself is built as a plain `dict` (`_build_request`) rather
    than the typed `run_v2.RunJobRequest` for the same reason -- Google's
    generated API clients accept a plain dict matching the request
    message's shape just as validly as the typed message, which is what
    lets tests exercise the real request-building logic against a fake
    client with no real network dependency at all.
    """

    def __init__(
        self,
        *,
        project: str | None = None,
        region: str | None = None,
        job_name: str = _TRIBUNAL_JOB_NAME,
        client: Any | None = None,
    ) -> None:
        self._project = project or config.GCP_PROJECT
        self._region = region or config.REGION
        self._job_name = job_name
        self._client = client

    def _job_path(self) -> str:
        return f"projects/{self._project}/locations/{self._region}/jobs/{self._job_name}"

    def _build_request(self, case_id: str) -> dict[str, Any]:
        return {
            "name": self._job_path(),
            "overrides": {
                "container_overrides": [{"env": [{"name": "CASE_ID", "value": case_id}]}],
            },
        }

    def _build_client(self) -> Any:
        from google.cloud import run_v2

        return run_v2.JobsClient()

    async def trigger(self, case_id: str) -> None:
        client = (
            self._client
            if self._client is not None
            else await asyncio.to_thread(self._build_client)
        )
        await asyncio.to_thread(client.run_job, request=self._build_request(case_id))


class LocalPipelineJobTrigger:
    """A local/dev `JobTrigger` that runs the real tribunal pipeline
    in-process, as a background `asyncio` task, sharing this console
    process's own `store`/`document_source` instances.

    Enabled only when `SETBACK_LOCAL_TRIBUNAL=1` is set (see
    `_build_production_app`) -- the deployed Cloud Run Service never sets
    it, so it never runs the tribunal pipeline inside a customer-facing web
    request; a real `setback-tribunal` Cloud Run Job execution remains the
    production path (`LoggingJobTrigger`, unchanged). This trigger exists
    because a real Cloud Run Job execution runs in a separate container
    with no access to this process's in-memory `UserUploadedDocumentSource`
    (see `job.pipeline`'s module docstring) -- sharing the instance
    directly is exactly how local end-to-end testing sidesteps that gap.

    Fire-and-forget: `trigger` schedules the run and returns immediately
    (matching the production route's 202-Accepted semantics) rather than
    blocking the HTTP request for the whole tribunal run; the SSE stream
    and case page pick up every event as `RealPipelineRunner` persists it.
    """

    def __init__(
        self,
        *,
        store: CaseStore,
        document_source: DocumentSource,
        model_client: ModelClient,
    ) -> None:
        self._store = store
        self._document_source = document_source
        self._model_client = model_client
        self._tasks: list[asyncio.Task[None]] = []

    async def trigger(self, case_id: str) -> None:
        self._tasks.append(asyncio.create_task(self._run(case_id)))

    async def _run(self, case_id: str) -> None:
        from setback.job.main import run_job
        from setback.job.pipeline import RealPipelineRunner

        pipeline = RealPipelineRunner(
            document_source=self._document_source,
            polisher=self._model_client,
            grounding_client=self._model_client,
        )
        await run_job(case_id, store=self._store, pipeline=pipeline)


# --- request/response bodies ---------------------------------------------------


class CreateCaseRequest(BaseModel):
    application_number: str
    resident_session: str


class InterviewAnswerRequest(BaseModel):
    answer: str


class RefusalFeedbackRequest(BaseModel):
    original_explanation: str
    pushback: str


_SUGGESTED_REPLIES: Mapping[InterviewStage, tuple[str, ...]] = {
    # Only stages with a genuinely closed answer set get chips (UI-SPEC.md
    # §2.2) -- every other stage stays input-only (`None`), exactly like
    # today. Chip text reads as a spoken confirmation, not a bare boolean
    # (copy tone guide §4 rule 8).
    InterviewStage.CONFIRMING: ("Yes, that's right", "No, let me fix that"),
    InterviewStage.ASK_MORE: ("Yes, there's something else", "No, that's everything"),
    InterviewStage.REQUESTING_EVIDENCE: ("Skip for now",),
}


def _suggested_replies_for(stage: InterviewStage) -> list[str] | None:
    replies = _SUGGESTED_REPLIES.get(stage)
    return list(replies) if replies is not None else None


def _turn_to_json(turn: InterviewTurn, transcript: Sequence[InterviewTurn]) -> dict[str, Any]:
    return {
        "stage": turn.stage.value,
        "prompt": turn.prompt,
        "turns": [{"stage": t.stage.value, "prompt": t.prompt, "role": t.role} for t in transcript],
        "suggested_replies": _suggested_replies_for(turn.stage),
    }


def _event_to_json(event: CaseEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "payload": dict(event.payload),
        "sequence": event.sequence,
        "recorded_at": event.recorded_at.isoformat(),
    }


async def _persist_system_turn(store: CaseStore, case_id: str, turn: InterviewTurn) -> None:
    digest = hashlib.sha256(f"system:{turn.stage.value}:{turn.prompt}".encode()).hexdigest()[:16]
    await store.append_event(
        case_id,
        f"interview-turn:{digest}",
        "interview_turn",
        payload={"role": "system", "stage": turn.stage.value, "message": turn.prompt},
    )


async def _persist_resident_answer(store: CaseStore, case_id: str, stage: str, answer: str) -> None:
    digest = hashlib.sha256(f"resident:{stage}:{answer}".encode()).hexdigest()[:16]
    await store.append_event(
        case_id,
        f"interview-answer:{digest}",
        "interview_turn",
        payload={"role": "resident", "stage": stage, "message": answer},
    )


def _ground_id_for(case_id: str, concern: RaisedConcern) -> str:
    """A deterministic ground id from the confirmed concern's identifying
    content, so re-processing the same confirmation (a retried request) is
    idempotent rather than proposing a duplicate ground."""
    digest = hashlib.sha256(
        f"{case_id}:{concern.concern_type.value}:{concern.initial_statement}".encode()
    ).hexdigest()[:16]
    return f"ground-{digest}"


async def _propose_ground_for_confirmed_concern(
    store: CaseStore, case_id: str, concern: RaisedConcern
) -> None:
    """Propose a `CandidateGround` the moment a concern is confirmed
    (`InterviewFlow._handle_confirming`'s affirm branch), and tag it with
    the s4.15 category `job.pipeline.RealPipelineRunner` later reads back
    to run the court/gate pipeline for this ground. This is the only place
    the interview's parsed `RaisedConcern` (with its classified
    `ConcernType`) is available -- the tribunal job never re-parses the
    transcript itself, per `job.pipeline`'s module docstring.
    """
    ground_id = _ground_id_for(case_id, concern)
    # P0 fix (wave-4 carry-forward): every ground claim must be built from
    # the clerk's `redacted_text` -- never the resident's raw statement --
    # so a name/phone/email the resident typed never reaches a downstream
    # tribunal prompt or the resident-facing docket board. `redacted_text`
    # falls back to `initial_statement` only in the defensive case it was
    # somehow never populated (it always is, in practice -- see
    # `InterviewFlow._handle_opening`).
    claim = concern.redacted_text or concern.initial_statement
    await store.propose_ground(case_id, ground_id, claim=claim)
    category = _CONCERN_CATEGORY.get(concern.concern_type, "environmental_and_social_impacts")
    await store.append_event(
        case_id,
        f"ground-category:{ground_id}",
        "ground_category_assigned",
        payload={
            "ground_id": ground_id,
            "category": category,
            "concern_type": concern.concern_type.value,
            "evidence_document_ids": list(concern.evidence_document_ids),
        },
    )


_IN_PROGRESS_INTERVIEW_STAGES: Final[frozenset[InterviewStage]] = frozenset(
    {InterviewStage.CLARIFYING, InterviewStage.REQUESTING_EVIDENCE, InterviewStage.CONFIRMING}
)


def _reconstruct_confirmed_concerns(
    grounds: Sequence[GroundRecord], events: Sequence[CaseEvent]
) -> list[RaisedConcern]:
    """Best-effort `RaisedConcern`s for every already-confirmed ground, from
    this case's own `ground_category_assigned` events (the concern's
    original `concern_type`/`evidence_document_ids`) plus the ground's own
    stored `claim` (already redacted -- see `_propose_ground_for_confirmed_
    concern`) as both `initial_statement` and `redacted_text`. Only
    `disputed_confirmations`/the separate raw `clarification` text are not
    recoverable this way; neither is read by anything downstream of a
    *confirmed* concern (`InterviewFlow._handle_ask_more` never revisits an
    already-appended `self.concerns` entry), so this approximation is safe."""
    category_by_ground: dict[str, Mapping[str, Any]] = {
        str(e.payload.get("ground_id", "")): e.payload
        for e in events
        if e.event_type == "ground_category_assigned"
    }
    confirmed: list[RaisedConcern] = []
    for ground in grounds:
        payload = category_by_ground.get(ground.ground_id)
        if payload is not None and payload.get("concern_type"):
            concern_type = ConcernType(str(payload["concern_type"]))
            evidence_ids = tuple(str(d) for d in payload.get("evidence_document_ids", ()))
        else:
            concern_type = classify_concern(ground.claim)
            evidence_ids = ()
        confirmed.append(
            RaisedConcern(
                concern_type=concern_type,
                initial_statement=ground.claim,
                confirmed=True,
                evidence_document_ids=evidence_ids,
                redacted_text=ground.claim,
            )
        )
    return confirmed


def _reconstruct_in_progress_concern(turn_events: Sequence[CaseEvent]) -> RaisedConcern | None:
    """Best-effort `RaisedConcern` for a concern the resident is still
    mid-way through (stage `CLARIFYING`/`REQUESTING_EVIDENCE`/`CONFIRMING`),
    rebuilt from this case's own persisted `interview_turn` events rather
    than from any in-memory state -- so a resumed flow's next `submit()`
    call has a real, usable `_current` instead of crashing on the state
    machine's `assert self._current is not None`.

    `turn_events` must be every `interview_turn` event for this case,
    sorted oldest-first. Approximation, documented rather than hidden: the
    resident's own `disputed_confirmations` history is not reconstructed
    (empty on resume) since nothing reads it except to compose the next
    clarifying question's wording, a cosmetic degrade at worst -- the state
    machine's own transitions are unaffected either way.
    """
    if not turn_events:
        return None
    last_stage_value = turn_events[-1].payload.get("stage")
    try:
        last_stage = InterviewStage(str(last_stage_value))
    except ValueError:
        return None
    if last_stage not in _IN_PROGRESS_INTERVIEW_STAGES:
        return None

    # A confirmed concern's own last turn is a system ASK_MORE prompt --
    # everything after the most recent one belongs to the concern still in
    # progress; everything before it belongs to already-confirmed concerns.
    block_start = 0
    for i, e in enumerate(turn_events):
        if (
            e.payload.get("stage") == InterviewStage.ASK_MORE.value
            and e.payload.get("role") == "system"
        ):
            block_start = i + 1
    block = turn_events[block_start:]

    initial_statement: str | None = None
    clarification_parts: list[str] = []
    for e in block:
        if e.payload.get("role") != "resident":
            continue
        stage = e.payload.get("stage")
        message = str(e.payload.get("message", ""))
        if stage == InterviewStage.OPENING.value and initial_statement is None:
            initial_statement = message
        elif stage in (InterviewStage.CLARIFYING.value, InterviewStage.REQUESTING_EVIDENCE.value):
            clarification_parts.append(message)
    if initial_statement is None:
        return None

    concern_type = classify_concern(initial_statement)
    redacted = redact_personal_information(initial_statement)
    clarification = "\n".join(clarification_parts) if clarification_parts else None
    if clarification:
        redacted = f"{redacted} {redact_personal_information(clarification)}".strip()
    return RaisedConcern(
        concern_type=concern_type,
        initial_statement=initial_statement,
        clarification=clarification,
        redacted_text=redacted,
    )


async def _rehydrate_flow_from_store(
    store: CaseStore,
    case_id: str,
    *,
    composer: QuestionComposer,
    concern_normaliser: ConcernNormaliser | None,
) -> InterviewFlow | None:
    """Rebuild an `InterviewFlow` from a case's own persisted event log
    instead of starting fresh -- the fix for the documented cold-start bug
    (LEO-FEEDBACK-UIUX.md §2): a fresh process (no in-memory `InterviewFlow`
    for this case -- a redeploy, a scale-to-zero cold start, a second
    instance) used to call `.start()` unconditionally, appending a second,
    differently-worded "opening" turn on top of what the case already had
    durably stored, and rendering only that one new turn rather than the
    full transcript. Returns `None` (caller then does the normal fresh-
    start path) exactly when this case has no persisted `interview_turn`
    events yet -- a genuinely new interview must still greet once."""
    events = await store.list_events(case_id)
    turn_events = sorted(
        (e for e in events if e.event_type == "interview_turn"), key=lambda e: e.sequence
    )
    if not turn_events:
        return None
    transcript = [
        InterviewTurn(
            stage=InterviewStage(str(e.payload.get("stage", InterviewStage.OPENING.value))),
            prompt=str(e.payload.get("message", "")),
            role=str(e.payload.get("role", "system")),
        )
        for e in turn_events
    ]
    grounds = await store.list_grounds(case_id)
    return InterviewFlow.resume(
        composer=composer,
        concern_normaliser=concern_normaliser,
        transcript=transcript,
        concerns=_reconstruct_confirmed_concerns(grounds, events),
        current=_reconstruct_in_progress_concern(turn_events),
    )


async def _sse_event_stream(
    store: CaseStore,
    case_id: str,
    *,
    poll_interval: float,
    idle_timeout: float | None,
    after: int = -1,
) -> AsyncIterator[str]:
    """Yield newly appended case events, in sequence order, as SSE `data:`
    lines, polling `store` for new ones.

    With `idle_timeout=None` (production default) this polls forever --
    exactly what keeps a resident's open SSE connection alive for the
    duration of a Cloud Run Service request. Tests pass a small
    `idle_timeout` so the stream terminates deterministically once it has
    caught up and gone quiet for that long, rather than hanging.

    `after`: skip every event at or below this sequence number. A fresh
    page load opens a brand-new SSE connection with an empty `seen` set, so
    without this cursor every event the case already has would be replayed
    and treated as "new" by the client -- which reloads the page on any
    event it doesn't already know how to handle in place, causing an
    infinite reload loop the moment a case has any history at all. The
    case page renders its own last-seen sequence number for the client to
    pass back here (see `render_case_page`/`app.js`).
    """
    seen: set[str] = set()
    idle_elapsed = 0.0
    while True:
        events = await store.list_events(case_id)
        new_events = sorted(
            (e for e in events if e.event_id not in seen and e.sequence > after),
            key=lambda e: e.sequence,
        )
        if new_events:
            idle_elapsed = 0.0
        for event in new_events:
            seen.add(event.event_id)
            yield f"data: {json.dumps(_event_to_json(event))}\n\n"
        if idle_timeout is not None and not new_events:
            idle_elapsed += poll_interval
            if idle_elapsed >= idle_timeout:
                return
        await asyncio.sleep(poll_interval)


def create_app(
    store: CaseStore,
    *,
    composer: QuestionComposer,
    document_source: EvidenceUploadStore | None = None,
    job_trigger: JobTrigger | None = None,
    concern_normaliser: ConcernNormaliser | None = None,
    max_upload_bytes: int = _DEFAULT_MAX_UPLOAD_BYTES,
    sse_poll_interval_seconds: float = 0.5,
    sse_idle_timeout_seconds: float | None = None,
    guard_counter_store: GuardCounterStore | None = None,
    guard_totals_store: GuardTotalsStore | None = None,
    docket_key_provider: Callable[[], str | None] | None = None,
) -> FastAPI:
    """Build the console FastAPI app over injected ports.

    Args:
        store: The case-store port (an `InMemoryCaseStore` in tests, a
            `FirestoreCaseStore` in production).
        composer: Composes interview turn wording (a fake in tests, a
            `ModelQuestionComposer` in production).
        document_source: Where uploaded photos/documents are durably
            written and later read back from -- an `EvidenceUploadStore`
            (`UserUploadedDocumentSource`'s in-memory offline-test double,
            or `evidence.storage.GcsEvidenceStore` in production). Defaults
            to a fresh in-memory `UserUploadedDocumentSource` per app.
        job_trigger: Starts the tribunal job. Defaults to `LoggingJobTrigger`.
        concern_normaliser: Classifies a resident's opening statement into
            structured `NormalisedConcern`s (see `interview.flow.
            ConcernNormaliser`). Defaults to `None`, which leaves
            `InterviewFlow` to fall back to its own offline
            `KeywordConcernNormaliser` -- production wiring
            (`_build_production_app`) passes a real `ModelConcernNormaliser`
            instead (P0 wave-4-carry-forward fix: this was previously never
            wired at all, so every deployed interview silently ran the
            keyword-only fallback regardless of the Gemma clerk's
            availability).
        max_upload_bytes: Hard cap on a single document/photo upload.
        sse_poll_interval_seconds: How often the events stream re-checks
            `store` for new events.
        sse_idle_timeout_seconds: If set, the events stream terminates
            after this many quiet seconds with no new events -- used by
            tests so a stream request completes instead of hanging;
            `None` (the default) polls forever, correct for production.
        guard_counter_store: Durable per-actor daily counters for the
            public-abuse guard's case-creation cap (see
            `console.guards.per_client_daily_case_cap_guard`). Defaults to
            a fresh `InMemoryGuardCounterStore` per app -- production
            wiring (`_build_production_app`) passes a
            `FirestoreGuardCounterStore` instead.
        guard_totals_store: The public-abuse guard's running spend/count
            aggregate (see `console.guards.CachedGuardTotalsReader`).
            Defaults to a fresh `InMemoryGuardTotalsStore` per app --
            production wiring passes a `FirestoreGuardTotalsStore` instead.
        docket_key_provider: Returns the console's current docket key (or
            `None`), read fresh on every call so a key rotation takes
            effect immediately with no restart. Defaults to reading
            `SETBACK_DOCKET_KEY` from the environment -- the exact source
            `_docket_key_accepted` already reads for the docket board's own
            gate, so the privileged-session cookie always agrees with it.
    """
    documents = document_source if document_source is not None else UserUploadedDocumentSource()
    trigger = job_trigger if job_trigger is not None else LoggingJobTrigger()
    guard_counters = (
        guard_counter_store if guard_counter_store is not None else InMemoryGuardCounterStore()
    )
    guard_totals = (
        guard_totals_store if guard_totals_store is not None else InMemoryGuardTotalsStore()
    )
    docket_key_of = (
        docket_key_provider
        if docket_key_provider is not None
        else (lambda: os.environ.get(_DOCKET_KEY_ENV_VAR))
    )

    app = FastAPI(title="Setback")
    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    interview_flows: dict[str, InterviewFlow] = {}
    _case_creation_guard = per_ip_case_creation_guard(docket_key_provider=docket_key_of)
    _interview_turn_guard = per_case_interview_turn_guard()
    _daily_case_cap_guard = per_client_daily_case_cap_guard(
        guard_counters, docket_key_provider=docket_key_of
    )
    _interview_turn_cap_guard = per_case_interview_turn_cap_guard(
        store, docket_key_provider=docket_key_of
    )
    _upload_cap_guard = per_case_upload_cap_guard(store, docket_key_provider=docket_key_of)
    _feedback_cap_guard = per_case_feedback_cap_guard(store, docket_key_provider=docket_key_of)
    _totals_reader = CachedGuardTotalsReader(guard_totals)
    _public_guard = public_guard_dependency(_totals_reader, docket_key_provider=docket_key_of)

    async def _is_paused() -> bool:
        return is_public_guard_paused(await _totals_reader.get_totals())

    async def _book_anonymous_spend(request: Request, amount_usd: float) -> None:
        """Add `amount_usd` to the public aggregate, but only for an
        anonymous request -- a privileged (judge/founder) session's own
        usage never counts against the public ceiling that pauses everyone
        else's access."""
        if is_privileged_request(request, docket_key_provider=docket_key_of):
            return
        await guard_totals.add_spend(amount_usd)
        totals = await guard_totals.get_totals()
        await record_threshold_events_if_crossed(guard_totals, totals)
        _totals_reader.invalidate()

    async def _require_case(case_id: str) -> CaseRecord:
        case = await store.get_case(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail=f"case {case_id!r} not found")
        return case

    @app.post(
        "/api/cases",
        status_code=201,
        dependencies=[
            Depends(_case_creation_guard),
            Depends(_daily_case_cap_guard),
            Depends(_public_guard),
        ],
    )
    async def create_case(body: CreateCaseRequest, request: Request) -> dict[str, Any]:
        case = await store.create_case(
            application_number=body.application_number, resident_session=body.resident_session
        )
        if not is_privileged_request(request, docket_key_provider=docket_key_of):
            await guard_totals.increment_anonymous_cases()
            totals = await guard_totals.get_totals()
            await record_threshold_events_if_crossed(guard_totals, totals)
            _totals_reader.invalidate()
        return {
            "case_id": case.case_id,
            "application_number": case.application_number,
            "resident_session": case.resident_session,
            "created_at": case.created_at.isoformat(),
        }

    @app.get("/api/cases/{case_id}/interview")
    async def get_interview(case_id: str) -> dict[str, Any]:
        await _require_case(case_id)
        flow = interview_flows.get(case_id)
        if flow is None:
            flow = await _rehydrate_flow_from_store(
                store, case_id, composer=composer, concern_normaliser=concern_normaliser
            )
            if flow is None:
                flow = InterviewFlow(composer=composer, concern_normaliser=concern_normaliser)
                turn = await flow.start()
                await _persist_system_turn(store, case_id, turn)
            interview_flows[case_id] = flow
        return _turn_to_json(flow.transcript[-1], flow.transcript)

    @app.post(
        "/api/cases/{case_id}/interview",
        dependencies=[
            Depends(_interview_turn_guard),
            Depends(_interview_turn_cap_guard),
            Depends(_public_guard),
        ],
    )
    async def answer_interview(
        case_id: str, body: InterviewAnswerRequest, request: Request
    ) -> dict[str, Any]:
        await _require_case(case_id)
        flow = interview_flows.get(case_id)
        if flow is None:
            raise HTTPException(
                status_code=404, detail="interview has not been started for this case yet"
            )
        current_stage = flow.stage
        await _persist_resident_answer(store, case_id, current_stage.value, body.answer)
        turn = await flow.submit(body.answer)
        await _persist_system_turn(store, case_id, turn)
        if turn.stage is InterviewStage.ASK_MORE and flow.concerns:
            await _propose_ground_for_confirmed_concern(store, case_id, flow.concerns[-1])
        if not is_privileged_request(request, docket_key_provider=docket_key_of):
            await guard_totals.increment_anonymous_turns()
            await _book_anonymous_spend(request, PUBLIC_TURN_COST_ESTIMATE_USD)
        return _turn_to_json(turn, flow.transcript)

    @app.post(
        "/api/cases/{case_id}/documents",
        dependencies=[Depends(_upload_cap_guard), Depends(_public_guard)],
    )
    async def upload_document(
        case_id: str,
        file: UploadFile = File(...),  # noqa: B008 -- required FastAPI idiom
    ) -> JSONResponse:
        await _require_case(case_id)
        content = await file.read(max_upload_bytes + 1)
        if len(content) > max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"document exceeds the {max_upload_bytes}-byte upload limit",
            )
        # Server-determined from the bytes themselves, never the
        # client-supplied header -- see `_sniff_upload_content_type`.
        sniffed_content_type = _sniff_upload_content_type(content)
        if sniffed_content_type is None:
            raise HTTPException(
                status_code=415,
                detail="unsupported file type -- only photos and PDF documents are accepted",
            )
        document_id = hashlib.sha256(content).hexdigest()[:16]
        await documents.add_evidence_document(
            case_id, document_id, content, content_type=sniffed_content_type
        )
        await store.append_event(
            case_id,
            f"document-uploaded:{document_id}",
            "document_uploaded",
            payload={
                "document_id": document_id,
                "filename": file.filename,
                "content_type": sniffed_content_type,
                "size_bytes": len(content),
            },
        )
        flow = interview_flows.get(case_id)
        if flow is not None:
            turn = await flow.record_evidence_upload(document_id)
            await _persist_system_turn(store, case_id, turn)
        return JSONResponse({"document_id": document_id, "size_bytes": len(content)})

    @app.get("/api/cases/{case_id}/documents/{document_id}")
    async def get_uploaded_document(case_id: str, document_id: str) -> Response:
        """Serve a previously uploaded document/photo's raw bytes back,
        from wherever `documents` (`EvidenceUploadStore`) actually durably
        wrote them -- in-memory in tests, `evidence.storage.GcsEvidenceStore`
        (GCS) in production, so this works identically against the deployed
        app with no separate wiring. The doc-card thumbnail (`_render_
        document_uploaded_item`) points a real `<img>` at this exact URL
        for a photo upload -- previously always a placeholder icon, even
        though the resident's actual photo bytes existed in the store the
        whole time.

        `content_type`/`filename` aren't tracked by `EvidenceUploadStore`
        itself (`download_document` returns bytes only), so they're read
        back from this same case's own `document_uploaded` event -- the
        one place that already recorded them at upload time. A wave-9
        full-resolution overlay document (`job.pipeline._store_full_res_
        overlay`) was never uploaded through that endpoint, so it also
        checks this case's `annotated_overlay` events for a matching
        `full_res_document_id`, using that event's own `mime_type` --
        without this, the browser would receive `application/octet-stream`
        for a real PNG and download it instead of displaying it inline in
        the lightbox."""
        await _require_case(case_id)
        content_type = "application/octet-stream"
        for event in await store.list_events(case_id):
            if (
                event.event_type == "document_uploaded"
                and event.payload.get("document_id") == document_id
            ):
                content_type = str(event.payload.get("content_type") or content_type)
                break
            if (
                event.event_type == "annotated_overlay"
                and event.payload.get("full_res_document_id") == document_id
            ):
                content_type = str(event.payload.get("mime_type") or content_type)
                break
        try:
            content = await documents.download_document(
                ExhibitedDocument(
                    document_id=document_id,
                    title=document_id,
                    source="user-upload",
                    case_id=case_id,
                )
            )
        except DocumentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(
            content=content,
            media_type=content_type,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @app.get("/api/cases/{case_id}/qr.png")
    async def case_qr_code(case_id: str, request: Request) -> Response:
        """A QR code encoding this case's own page URL (LEO-FEEDBACK-
        UIUX.md §1) -- account-free re-access: a resident scans it (e.g.
        from a printed copy or a second device) to get straight back to
        their case, no login and nothing server-side beyond the case's own
        already-unguessable URL."""
        await _require_case(case_id)
        case_url = str(request.url_for("case_page", case_id=case_id))
        return Response(content=_render_qr_png(case_url), media_type="image/png")

    @app.post(
        "/api/cases/{case_id}/tribunal",
        status_code=202,
        dependencies=[Depends(_public_guard)],
    )
    async def start_tribunal(case_id: str) -> dict[str, Any]:
        await _require_case(case_id)
        await enforce_concurrent_tribunal_cap(store)
        await enforce_daily_spend_budget(store)
        # A per-attempt nonce, not just `case_id`, keyed into the event id:
        # `CaseStore.append_event` dedups by exact event id (its
        # idempotency mechanism for retried writes), so a fixed
        # `f"tribunal-requested:{case_id}"` id meant every attempt after
        # the very first silently collapsed into the same Firestore
        # document -- no new event, no sequence advance, no audit trail --
        # while `trigger.trigger` below still fired a real second Cloud Run
        # Job execution regardless. Caught live in smoke loop #2.
        await store.append_event(
            case_id,
            f"tribunal-requested:{case_id}:{secrets.token_hex(4)}",
            "tribunal_requested",
            payload={},
        )
        try:
            await trigger.trigger(case_id)
        except Exception as exc:  # noqa: BLE001 -- must record + report, never crash uncaught
            # A `tribunal_requested` event with no later terminal event
            # counts as "still running" forever against
            # `guards.enforce_concurrent_tribunal_cap` (smoke loop #2 found
            # this live: a `RealJobTrigger` permission error left a case
            # permanently burning one of only 2 concurrent-run slots). Book
            # a `job_failed` terminal event -- the same event type/payload
            # shape `job.main`'s own pipeline-failure handler already uses
            # -- before reporting the error, so the guard sees this run as
            # over rather than still in flight.
            await store.append_event(
                case_id,
                f"job-failed:{case_id}:{type(exc).__name__}:{secrets.token_hex(4)}",
                "job_failed",
                payload={"error": str(exc)},
            )
            raise HTTPException(
                status_code=502,
                detail="could not start the tribunal job; please try again shortly",
            ) from exc
        return {"case_id": case_id, "status": "tribunal_requested"}

    @app.get("/api/cases/{case_id}/events")
    async def stream_events(case_id: str, after: int = -1) -> StreamingResponse:
        await _require_case(case_id)
        return StreamingResponse(
            _sse_event_stream(
                store,
                case_id,
                poll_interval=sse_poll_interval_seconds,
                idle_timeout=sse_idle_timeout_seconds,
                after=after,
            ),
            media_type="text/event-stream",
        )

    async def _latest_submission_payload(case_id: str) -> Mapping[str, Any]:
        events = await store.list_events(case_id)
        submissions = [e for e in events if e.event_type == "submission_composed"]
        if not submissions:
            raise HTTPException(status_code=404, detail="no submission has been composed yet")
        return max(submissions, key=lambda e: e.sequence).payload

    @app.get("/api/cases/{case_id}/submission.md", response_class=PlainTextResponse)
    async def download_submission_markdown(case_id: str) -> str:
        await _require_case(case_id)
        payload = await _latest_submission_payload(case_id)
        return str(payload.get("submission_markdown", ""))

    @app.get("/api/cases/{case_id}/submission.html", response_class=HTMLResponse)
    async def download_submission_html(case_id: str) -> str:
        await _require_case(case_id)
        payload = await _latest_submission_payload(case_id)
        return str(payload.get("submission_html", ""))

    @app.get("/api/cases/{case_id}/refusals.md", response_class=PlainTextResponse)
    async def download_refusals_markdown(case_id: str) -> str:
        await _require_case(case_id)
        payload = await _latest_submission_payload(case_id)
        return str(payload.get("refusals_markdown", ""))

    @app.get("/api/cases/{case_id}/refusals.html", response_class=HTMLResponse)
    async def download_refusals_html(case_id: str) -> str:
        await _require_case(case_id)
        payload = await _latest_submission_payload(case_id)
        return str(payload.get("refusals_html", ""))

    @app.get("/api/cases/{case_id}/transcript.txt", response_class=PlainTextResponse)
    async def download_transcript(case_id: str) -> str:
        """Plain-text export of the full interview transcript (LEO-FEEDBACK-
        UIUX.md §2) with absolute Australia/Sydney timestamps -- a resident
        can keep a copy independent of the app."""
        await _require_case(case_id)
        events = await store.list_events(case_id)
        return _render_transcript_text(events)

    @app.post(
        "/api/cases/{case_id}/grounds/{ground_id}/feedback",
        dependencies=[Depends(_feedback_cap_guard), Depends(_public_guard)],
    )
    async def refusal_feedback(
        case_id: str, ground_id: str, body: RefusalFeedbackRequest, request: Request
    ) -> dict[str, Any]:
        await _require_case(case_id)
        try:
            feedback = await capture_refusal_feedback(
                store=store,
                composer=composer,
                case_id=case_id,
                ground_id=ground_id,
                original_explanation=body.original_explanation,
                pushback=body.pushback,
            )
        except CaseNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        # Security-review finding (2026-08-30): this route makes one real
        # model call per request (`capture_refusal_feedback` ->
        # `composer.compose`) but was never booked against the public
        # aggregate -- see `MAX_REFUSAL_FEEDBACK_PER_CASE`'s docstring.
        # Booked at the same flat estimate as an interview turn: both are
        # one INTERVIEW-tier `composer.compose` call.
        if not is_privileged_request(request, docket_key_provider=docket_key_of):
            await _book_anonymous_spend(request, PUBLIC_TURN_COST_ESTIMATE_USD)
        return {
            "ground_id": feedback.ground_id,
            "re_rendered_explanation": feedback.re_rendered_explanation,
        }

    @app.get("/", response_class=HTMLResponse)
    async def landing_page(theme: str | None = None) -> str:
        """The public, unauthenticated home page (LEO-FEEDBACK-UIUX.md §1):
        no key, no docket content -- a resident starts a new objection here
        with one DA-number input. `key`/other stray query params are simply
        ignored rather than gating anything; this route must never 401 and
        must never be blocked by the public-abuse guard -- it only ever
        renders the honest paused banner when the guard is paused (see
        `_is_paused`), it never refuses the read itself."""
        return render_landing_page(force_theme=theme, paused=await _is_paused())

    @app.get("/docket", response_class=HTMLResponse)
    async def docket_board(
        response: Response, theme: str | None = None, key: str | None = None
    ) -> str:
        if not _docket_key_accepted(key):
            raise HTTPException(
                status_code=401,
                detail=(
                    "This docket board requires a passphrase: GET /docket?key=<SETBACK_DOCKET_KEY>."
                ),
            )
        # A VALID key doubles as a privileged-session grant (DESIGN SPEC
        # point 1, "Layered + key bypass"): `_docket_key_accepted` above
        # already enforced `key == expected_key` via `secrets.compare_digest`
        # whenever a real key is configured, so reaching this line with
        # `expected_key` truthy means `key` is genuine -- safe to mint the
        # cookie from it. No configured key at all (local dev) means there
        # is no real secret to grant a privileged session over.
        expected_key = os.environ.get(_DOCKET_KEY_ENV_VAR)
        if expected_key:
            response.set_cookie(
                key=PRIVILEGED_COOKIE_NAME,
                value=privileged_cookie_value(expected_key),
                max_age=PRIVILEGED_COOKIE_MAX_AGE_SECONDS,
                httponly=True,
                secure=True,
                samesite="lax",
            )
        cases: list[tuple[CaseRecord, tuple[GroundRecord, ...]]] = []
        for case in await store.list_cases():
            if not _is_hygiene_excluded(case):
                cases.append((case, await store.list_grounds(case.case_id)))
        totals = await guard_totals.get_totals()
        return render_docket_board(
            cases, force_theme=theme, guard_totals=totals, ceiling_usd=PUBLIC_SPEND_CEILING_USD
        )

    @app.get("/cases/{case_id}", response_class=HTMLResponse)
    async def case_page(case_id: str, theme: str | None = None) -> str:
        case = await _require_case(case_id)
        grounds = await store.list_grounds(case_id)
        events = await store.list_events(case_id)
        ledger = await store.load_ledger(case_id)
        return render_case_page(
            case, grounds, events, ledger, force_theme=theme, paused=await _is_paused()
        )

    return app


# --- server-rendered HTML -----------------------------------------------------


def _esc(text: object) -> str:
    return html.escape(str(text))


_PAGE_STYLE = """
<link rel="stylesheet" href="/static/style.css">
"""


_DISCLAIMER_FOOTER = """
  <footer class="disclaimer-footer">
    <p>Setback is not a law firm and does not provide legal advice. It helps you prepare a
    submission; council and the Land and Environment Court decide the outcome. Not affiliated
    with, endorsed by, or a service of the NSW Government.</p>
  </footer>
"""
"""Persistent, non-dismissible footer (UI-SPEC.md §2.14/§5) -- present on
every page, not a modal a resident can dismiss once."""


_DOCKET_KEY_ENV_VAR: Final[str] = "SETBACK_DOCKET_KEY"

_UUID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


_JUNK_METADATA_PATTERNS: Final[tuple[str, ...]] = (
    "smoke",
    "test",
    "wiring-proof",
    "rate-limit",
    "qa",
    "deploy",
    "sv-test",
)
"""Case-insensitive substrings that mark a case as test/smoke/deploy-
verification debris even when `_looks_like_a_resident_session` alone would
let it through -- e.g. a scripted/curl-created case given a real
`window.crypto.randomUUID()`-shaped `resident_session` on purpose, or one
whose junk label lives in `application_number` rather than
`resident_session` (a `SMOKE-TEST-PHOTO`/`wave6-wiring-proof`/`rate-limit-
check` application number). Found live: SMOKE.md v4's own docket-hygiene
finding, a case that passed the purely structural UUID check but was
still an obvious smoke artifact by its own label.

`qa`/`deploy`/`sv-test` (wave 9.5) close a second real leak: the wave-9
populate/redeploy-verification passes created live, real-UUID-session
cases with application numbers like `DA2026/DEPLOY-QA` and
`DA2026/SV-TEST` (SMOKE.md v8's Street View verification case) that would
otherwise sit on the public docket board next to genuine resident
objections and the film cases. `sv-test` is listed explicitly even though
the pre-existing `test` pattern already matches it, so the specific,
named regression this wave's brief called out has its own on-the-record
pattern rather than relying solely on a broader substring incidentally
covering it. None of these substrings can appear in a hex-shaped
`resident_session`/`case_id` (no `q` in `0-9a-f`) or in a real DA
number/film-case label (`DA2026/0359`, `DA2026/0412-FILM2`, and the
populate pass's own real-DA case ids all miss every one of them), so this
extension carries no risk of hiding a genuine or film case -- see the
tests pinning FILM/FILM2 and Case A stay visible."""


def _mentions_a_junk_pattern(text: str) -> bool:
    lowered = text.lower()
    return any(pattern in lowered for pattern in _JUNK_METADATA_PATTERNS)


_DEPRECATED_CASE_IDS: Final[frozenset[str]] = frozenset(
    {
        "f3f8c3475e2646537212677fbf7c8075",  # DA2026/0412-FILM
    }
)
"""Specific `case_id`s to hide from the docket-board list regardless of how
legitimate their own `application_number`/`resident_session` look (P0
synthesis wave 12, #4): `f3f8c3475e2646537212677fbf7c8075`
(`DA2026/0412-FILM`) is a superseded predecessor of the canonical film case
(`DA2026/0412-FILM2`, `cc9bfc59084fd7cac527c479f0e71996`) that was still
showing up on the public docket next to it. A denylist keyed on `case_id`
is used instead of extending `_JUNK_METADATA_PATTERNS` with a `film`
substring precisely because that would also catch the canonical
`DA2026/0412-FILM2` case -- `"film2".find("film") != -1` -- which must stay
visible. Hide only, never delete: `/cases/{case_id}` is untouched."""


def _is_hygiene_excluded(case: CaseRecord) -> bool:
    """True when `case` should never appear on the public docket-board
    *list* (its own `/cases/{case_id}` page is unaffected either way --
    see `docket_board`, which never calls this for the case-page route):
    `case.case_id` is in `_DEPRECATED_CASE_IDS`, a `resident_session` that
    isn't UUID-shaped (`_looks_like_a_resident_session`), or a
    `resident_session`/`application_number`/`case_id` that contains one of
    `_JUNK_METADATA_PATTERNS`. The latter two checks are deliberately
    independent (not just "non-UUID OR junk-keyword-in-session") so a case
    with a genuine random UUID session is still caught when the junk label
    was put in `application_number` instead."""
    if case.case_id in _DEPRECATED_CASE_IDS:
        return True
    if not _looks_like_a_resident_session(case.resident_session):
        return True
    return any(
        _mentions_a_junk_pattern(field)
        for field in (case.resident_session, case.application_number, case.case_id)
    )


def _looks_like_a_resident_session(resident_session: str) -> bool:
    """True exactly when `resident_session` is shaped like
    `window.crypto.randomUUID()`'s output -- what every genuine resident
    case is created with (`getResidentSessionId`, `console/static/app.js`).

    The docket board's own hygiene filter (`docket_board`, below) uses this
    as a purely structural rule rather than a hardcoded list of known
    smoke/test/deploy-verification session labels (`SMOKE-RATE-LIMIT-
    TEST-*`, `deploy-wiring-proof`, `deploy-verify-au-001`, ...) to keep in
    sync by hand -- every one of those was created by a manual `POST
    /api/cases` call during testing, never the real browser flow, and so
    is structurally incapable of ever producing a UUID. A judge visiting
    the hosted docket board previously saw this dev/smoke-test debris
    ahead of the one real demo case; an individual case's own page is
    unaffected by this filter (reachable at its unguessable case-id URL
    regardless), only the public *list* is hygiened."""
    return bool(_UUID_PATTERN.match(resident_session))


def _docket_key_accepted(provided_key: str | None) -> bool:
    """The docket **list** route's access gate: `SETBACK_DOCKET_KEY`
    (unset by default, e.g. local dev and every test in this suite that
    doesn't set it) disables the gate entirely, preserving today's
    behaviour. Once configured (production/demo), `GET /` requires a
    matching `?key=`, closing the real PII-exposure gap a judge could
    otherwise stumble into: no login, no per-session boundary, a
    stranger's full objection narrative and uploaded evidence reachable
    from a public board with zero friction. An individual case page's own
    unguessable URL stays reachable either way (`case_page` never calls
    this) -- a judge who has a direct link, or creates their own case
    through the normal flow, is never blocked.

    `secrets.compare_digest` (already imported at module scope) rather
    than `==`, since this is a real -- if low-stakes -- secret comparison.
    """
    expected = os.environ.get(_DOCKET_KEY_ENV_VAR)
    if not expected:
        return True
    if provided_key is None:
        return False
    return secrets.compare_digest(provided_key, expected)


_DOCKET_STATUS_MODIFIER_AND_LABEL: Mapping[str, tuple[str, str]] = {
    "flagged": ("flagged", "Needs your input"),
    "in_review": ("pending", "In review"),
    "ready": ("shipped", "Ready to submit"),
    "just_started": ("pending", "Just started"),
}


def _docket_status_for(grounds: Sequence[GroundRecord]) -> tuple[str, str]:
    """Derive the docket board's overall case status from the
    worst-priority ground status present (UI-SPEC.md §3.1): any `flagged`
    ground needs the resident's attention first; any ground still
    `proposed`/`under_review` means the tribunal hasn't finished; once
    every ground has reached a terminal state (`supported` or `refused`)
    the case is ready to submit; no grounds at all means the interview
    hasn't produced one yet.

    Returns the `(tag_modifier, label)` pair -- `tag_modifier` is one of
    the shared four-token `.tag--*` classes (§2.11), `label` the
    plain-English adjective shown on it (copy tone guide §4 rule 6).
    """
    if not grounds:
        modifier, label = _DOCKET_STATUS_MODIFIER_AND_LABEL["just_started"]
        return modifier, label
    statuses = {g.status for g in grounds}
    if GroundStatus.FLAGGED in statuses:
        return _DOCKET_STATUS_MODIFIER_AND_LABEL["flagged"]
    if GroundStatus.PROPOSED in statuses or GroundStatus.UNDER_REVIEW in statuses:
        return _DOCKET_STATUS_MODIFIER_AND_LABEL["in_review"]
    return _DOCKET_STATUS_MODIFIER_AND_LABEL["ready"]


def _earlier_cases_note(earlier_count: int) -> str:
    if earlier_count <= 0:
        return ""
    noun = "case" if earlier_count == 1 else "cases"
    return f'<span class="docket-card__earlier">+{earlier_count} earlier {noun}</span>'


def _render_docket_card(
    case: CaseRecord, grounds: Sequence[GroundRecord], *, earlier_count: int = 0
) -> str:
    modifier, label = _docket_status_for(grounds)
    return f"""
        <a class="docket-card" href="/cases/{_esc(case.case_id)}" title="{_esc(case.case_id)}">
          <div class="docket-card__main">
            <span class="docket-card__app">{_esc(case.application_number)}</span>
            <span class="docket-card__id">{_esc(case.case_id)}</span>
          </div>
          {_earlier_cases_note(earlier_count)}
          <span class="tag tag--{modifier}">{_esc(label)}</span>
        </a>
        """


_THEME_TOGGLE_BUTTON: Final[str] = (
    '<button type="button" id="theme-toggle" class="button--secondary" '
    'aria-label="Toggle light/dark theme">&#9788;/&#9789;</button>'
)
"""One shared toggle markup (LEO-FEEDBACK-UIUX.md §8), present in every
page's header -- `app.js` persists the viewer's choice to `localStorage`,
overriding system preference, while an explicit `?theme=` query param
(this page's own `force_theme`) still wins for a single filmed load."""


_VALID_FORCE_THEMES: Final[frozenset[str]] = frozenset({"light", "dark"})


def _html_tag(force_theme: str | None) -> str:
    """`<html>`, bare by default so `style.css`'s `prefers-color-scheme`
    contract governs (no hardcoded theme -- see
    `test_docket_board_does_not_hardcode_a_light_theme`), or with an
    explicit `data-theme` when `force_theme` names one of the two themes
    `style.css` actually implements (`?theme=light`/`?theme=dark` on
    either page route) -- a deliberate, opt-in override for filming
    consistency (every existing gallery screenshot is light-mode), never
    a change to any viewer's default. Any other value (unset, unknown)
    degrades to the bare, system-following tag rather than emitting a
    `data-theme` value `style.css` doesn't define."""
    if force_theme in _VALID_FORCE_THEMES:
        return f'<html data-theme="{force_theme}">'
    return "<html>"


def _collapse_to_latest_per_application_number(
    cases: Sequence[tuple[CaseRecord, tuple[GroundRecord, ...]]],
) -> list[tuple[CaseRecord, tuple[GroundRecord, ...], int]]:
    """Collapse the docket board to one row per `application_number` -- the
    most recently created case for that application number wins, annotated
    with how many earlier cases for that same number were folded in (an
    `earlier_count`, shown as "+N earlier cases"). HIDE only: this only
    changes what the docket *list* shows -- every older case's data is
    untouched in `store` and still reachable at its own `/cases/{case_id}`
    URL (see `docket_board`) -- addressing SMOKE.md v4's own "duplicate
    app-number labels" docket-hygiene finding (`PAN-661190` appearing
    twice, `DA2026/0359` three times, all shown as separate "Ready to
    submit" rows).

    Row order otherwise follows `cases`' own order (i.e. `store.list_
    cases()`'s order) by the *kept* case, not by grouping/dict-iteration
    order -- so collapsing duplicates never reshuffles the rest of the
    board.
    """
    by_application: dict[str, list[tuple[CaseRecord, tuple[GroundRecord, ...]]]] = {}
    for case, grounds in cases:
        by_application.setdefault(case.application_number, []).append((case, grounds))

    kept_by_case_id: dict[str, tuple[CaseRecord, tuple[GroundRecord, ...], int]] = {}
    for group in by_application.values():
        latest_case, latest_grounds = max(group, key=lambda item: item[0].created_at)
        kept_by_case_id[latest_case.case_id] = (latest_case, latest_grounds, len(group) - 1)

    return [
        kept_by_case_id[case.case_id] for case, _grounds in cases if case.case_id in kept_by_case_id
    ]


_GUARD_PAUSED_BANNER_COPY: Final[str] = (
    "We've used up the public demo budget for this hackathon build. "
    "Every case that's already open stays open to browse. "
    "Starting anything new is paused for now."
)
"""Copy checked against WRITING-STYLE-GUIDE.md (binding for public-facing
text): plain, honest, contractions kept in, no slop vocabulary, and no
mention of the key/bypass mechanism (DESIGN SPEC point 4)."""


def _guard_paused_banner_html(paused: bool) -> str:
    if not paused:
        return ""
    return f'<div class="guard-paused-banner" role="status">{_esc(_GUARD_PAUSED_BANNER_COPY)}</div>'


def _docket_spend_summary_html(totals: GuardTotals | None, ceiling_usd: float) -> str:
    """Founder/judge-only spend visibility (DESIGN SPEC point 5), rendered
    only on the key-gated docket board -- never on any public page."""
    if totals is None:
        return ""
    pct = min(100.0, (totals.spend_usd / ceiling_usd) * 100) if ceiling_usd > 0 else 0.0
    return (
        '<p class="docket-spend-summary">Public spend: '
        f"${totals.spend_usd:.2f} / ${ceiling_usd:.2f} ({pct:.0f}%) &middot; "
        f"{totals.anonymous_cases} anonymous cases &middot; "
        f"{totals.anonymous_turns} anonymous turns</p>"
    )


def render_docket_board(
    cases: Sequence[tuple[CaseRecord, tuple[GroundRecord, ...]]],
    *,
    force_theme: str | None = None,
    guard_totals: GuardTotals | None = None,
    ceiling_usd: float = PUBLIC_SPEND_CEILING_USD,
) -> str:
    """Render the docket board: every case this console instance has
    created, each as a `.docket-card` (UI-SPEC.md §3.1) carrying a derived
    overall-status tag rather than a bare grounds count -- collapsed to one
    row per `application_number` (`_collapse_to_latest_per_application_
    number`) so duplicate variants of the same real DA number don't each
    get their own row.

    `guard_totals`/`ceiling_usd`: the public-abuse guard's current spend %,
    surfaced here only (founder/judge-visible, key-gated) -- see
    `_docket_spend_summary_html`."""
    collapsed = _collapse_to_latest_per_application_number(cases)
    rows = "".join(
        _render_docket_card(case, grounds, earlier_count=earlier_count)
        for case, grounds, earlier_count in collapsed
    )
    if not rows:
        rows = '<p class="empty">No cases yet -- create one to get started.</p>'
    spend_summary = _docket_spend_summary_html(guard_totals, ceiling_usd)
    return f"""
<!doctype html>
{_html_tag(force_theme)}
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Setback -- Docket Board</title>
  {_PAGE_STYLE}
</head>
<body>
  <header class="topbar">
    <h1><a href="/">Setback</a></h1>
    <p class="tagline">A Collaborative Partner for planning objections</p>
    <div class="topbar__actions">{_THEME_TOGGLE_BUTTON}</div>
  </header>
  <main class="container">
    <h2>Docket board</h2>
    {spend_summary}
    <div class="docket-list">
      {rows}
    </div>
  </main>
{_DISCLAIMER_FOOTER}
  <script src="/static/app.js"></script>
</body>
</html>
"""


def _render_qr_png(data: str) -> bytes:
    """A PNG QR code encoding `data`, via the pure-Python `segno` library
    (no native deps, no network) -- LEO-FEEDBACK-UIUX.md §1's "server-
    generated PNG is fine" suggestion."""
    import io

    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="png", scale=6, border=2)
    return buf.getvalue()


def render_landing_page(*, force_theme: str | None = None, paused: bool = False) -> str:
    """The public, Google/Claude-style home page (LEO-FEEDBACK-UIUX.md §1):
    product name, caption, ONE highlighted DA-number input that starts a new
    objection, and the persistent disclaimer footer -- no docket content, no
    key gate. `app.js`'s `initLandingPage()` (client-side) submits the form
    via `POST /api/cases`, then redirects to the new case page, and renders
    a "your previous cases" list read from this browser's own localStorage
    (nothing server-side -- no cases are listed here by the server).

    `paused`: the public-abuse guard's current state (DESIGN SPEC point 4)
    -- renders a calm, honest banner when `True`; this route itself never
    401s or refuses the read either way (see `console/app.py`'s
    `landing_page`)."""
    banner = _guard_paused_banner_html(paused)
    return f"""
<!doctype html>
{_html_tag(force_theme)}
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Setback</title>
  {_PAGE_STYLE}
</head>
<body class="landing">
  <div class="landing__theme-toggle">{_THEME_TOGGLE_BUTTON}</div>
  <main class="landing__main">
    <h1 class="landing__title">Setback</h1>
    <p class="landing__tagline">A Collaborative Partner for planning objections</p>
    {banner}
    <form id="start-case-form" class="landing__form">
      <input id="application-number-input" name="application_number" type="text"
             placeholder="DA number, e.g. DA2026/0359" autocomplete="off" autofocus
             aria-label="Development application number">
      <button type="submit">Start my objection</button>
    </form>
    <p id="start-case-error" class="landing__error" role="alert" hidden></p>
    <section id="previous-cases" class="previous-cases" hidden>
      <h2>Your previous cases</h2>
      <ul id="previous-cases-list" class="previous-cases__list"></ul>
    </section>
  </main>
{_DISCLAIMER_FOOTER}
  <script src="/static/app.js"></script>
</body>
</html>
"""


_EVENT_SECTION_TITLES: Mapping[str, str] = {
    # `interview_turn` (LEO-FEEDBACK-UIUX.md §2), `review_verdict` and
    # `gate_decision`/`adjudication_decision` (§3) are deliberately absent
    # here: the interview transcript lives ONLY in the chat pane now (the
    # standalone "Interview transcript" section was redundant with it), and
    # a ground's reviewer opinions + gate decision render INSIDE that
    # ground's own accordion (`_render_ground_card`) instead of three
    # separate, unlinked flat lists -- see `render_case_page`.
    "document_uploaded": "Evidence",
    "annotated_overlay": "Annotated evidence overlay",
    "resident_refusal_feedback": "Resident feedback on refusals",
    "submission_composed": "Submission documents",
    # `tribunal_requested`/`ingest_resolved`/`tribunal_rerun_ignored` are
    # deliberately absent here (round-2 UI feedback, item 4): the standalone
    # "Tribunal" tab/section they used to render in no longer exists at
    # all -- the start timestamp moved to the case header
    # (`_format_started_at`/`_render_case_meta_line`), and the other two
    # moved into the Grounds tab's small "Notes" card
    # (`_render_case_notes_section`), called explicitly from
    # `render_case_page` rather than through this generic dispatch.
}


def _render_review_verdict_item(event: CaseEvent) -> str:
    payload = event.payload
    if payload.get("voided"):
        return (
            f'<li class="review-verdict review-verdict--voided">'
            f"<strong>{_esc(payload.get('reviewer', 'reviewer'))}</strong> "
            "(opinion voided -- cited an anchor outside the case dossier)</li>"
        )
    return (
        f'<li class="review-verdict review-verdict--{_esc(payload.get("stance", ""))}">'
        f"<strong>{_esc(payload.get('reviewer', 'reviewer'))}</strong> "
        f"&mdash; {_esc(payload.get('stance', ''))} "
        f"(confidence {_esc(payload.get('confidence', ''))})"
        f"<br><em>{_esc(payload.get('rationale', ''))}</em></li>"
    )


def _refusal_reassurance(total_grounds: int) -> str:
    """ "Your other N grounds are unaffected." -- copy tone guide §4 rule 5:
    a stressed resident's first fear on seeing "Refused" is "did I lose
    everything," answered in the same sentence."""
    other_count = max(total_grounds - 1, 0)
    if other_count <= 0:
        return ""
    noun = "ground" if other_count == 1 else "grounds"
    verb = "is" if other_count == 1 else "are"
    return f" Your other {other_count} {noun} {verb} unaffected."


def _render_gate_detail(
    ground: GroundRecord, gate_decision: Mapping[str, Any], total_grounds: int
) -> str:
    """The gate's ruling on `ground`, rendered INSIDE that ground's own
    accordion (LEO-FEEDBACK-UIUX.md §3) rather than a separate, unlinked
    "Gate decisions" list -- and, for a refusal, naming the ground in the
    heading itself ("We didn't include: <claim>") instead of a generic
    "We didn't include this ground" a resident would have to cross-reference
    against a claim shown somewhere else on the page.

    Framed as rigor rather than apology (copy tone guide §4 rules 4-5):
    `role="region"` (informational, non-interrupting), warm brown
    (`--status-refused`) -- never `--error`/`role="alert"`, reserved for
    true system failures (founder requirement #4)."""
    status = str(gate_decision.get("status", ""))
    explanation = str(gate_decision.get("explanation") or "")
    if status.startswith("refused"):
        return (
            '<div class="refusal-card" role="region" aria-label="A ground that was not included">'
            '<span class="refusal-card__icon" aria-hidden="true">&#9432;</span>'
            "<div>"
            f'<p class="refusal-card__heading">We didn&rsquo;t include: {_esc(ground.claim)}</p>'
            f'<p class="refusal-card__reason">'
            f"{_esc(explanation)}{_refusal_reassurance(total_grounds)}</p>"
            "</div></div>"
        )
    basis = str(gate_decision.get("statutory_basis") or "")
    parts: list[str] = []
    if basis:
        parts.append(
            '<p class="ground-card__basis">Statutory basis: '
            f'<span class="citation-chip citation-chip--clause">{_esc(basis)}</span></p>'
        )
    if explanation:
        parts.append(f'<p class="ground-card__explanation">{_esc(explanation)}</p>')
    return "".join(parts)


def _render_doc_viewer_legend() -> str:
    """The one `.doc-viewer__legend` markup, shared verbatim (down to the
    exact CSS classes and copy) with `console/static/app.js`'s
    `handleAnnotatedOverlay`, which builds this same chrome client-side for
    a live SSE `annotated_overlay` event. Both source their swatch order,
    CSS class suffix, and label text from `evidence.overlays` (`OverlayRole`
    / `ROLE_CSS_CLASS_SUFFIX` / `ROLE_LEGEND_TEXT`) -- the single place the
    overlay's own colour semantics are defined -- so the two can never drift
    apart the way they did before this fix (a server-rendered/reloaded case
    page previously showed the coloured-box image with **no legend at
    all**, since only the live-SSE JS path ever built one; colour-
    discipline rule 4 requires a legend any time an overlay colour is on
    screen)."""
    items = "".join(
        f'<span class="legend-item"><i class="legend-swatch '
        f'legend-swatch--{ROLE_CSS_CLASS_SUFFIX[role]}"></i>{_esc(ROLE_LEGEND_TEXT[role])}</span>'
        for role in OverlayRole
    )
    return f'<div class="doc-viewer__legend">{items}</div>'


def _render_annotated_overlay_item(case_id: str, event: CaseEvent) -> str:
    """Wave 9 (LEO-FEEDBACK-UIUX.md §5): when `job.pipeline` recorded a
    `full_res_document_id` (the pre-shrink image, durably stored via
    `EvidenceUploadStore` -- see that module's `_store_full_res_overlay`),
    the `<img>` carries a `data-full-res-src` pointing at this case's own
    `GET /api/cases/{case_id}/documents/{document_id}` route, which
    `app.js`'s lightbox (`wireOverlayLightbox`) opens instead of
    re-displaying the shrunk, embedded copy bigger. Absent for an overlay
    event recorded before this fix landed (an append-only log is never
    rewritten) -- degrades to the shrunk copy exactly as before."""
    payload = event.payload
    mime_type = _esc(payload.get("mime_type", "image/png"))
    image_base64 = _esc(payload.get("image_base64", ""))
    document_id = payload.get("document_id")
    doc_id_attr = f' data-doc-id="{_esc(document_id)}"' if document_id else ""
    full_res_document_id = payload.get("full_res_document_id")
    full_res_attr = (
        f' data-full-res-src="/api/cases/{_esc(case_id)}/documents/{_esc(full_res_document_id)}"'
        if full_res_document_id
        else ""
    )
    return (
        '<li class="annotated-overlay"><div class="doc-viewer">'
        '<div class="doc-viewer__stage">'
        f'<img src="data:{mime_type};base64,{image_base64}" '
        f'alt="Annotated evidence overlay"{doc_id_attr}{full_res_attr}>'
        "</div>"
        f"{_render_doc_viewer_legend()}"
        "</div></li>"
    )


_MAX_MAILTO_BODY_CHARS: Final[int] = 1800
"""A conservative cap keeping the composed `mailto:` URL well under the
~2000-character limit some mail clients/browsers silently truncate at --
long enough for most objection letters in full, with an honest truncation
note (never a silently cut-off letter) when one runs longer."""


def _mailto_href(*, subject: str, body: str) -> str:
    """A `mailto:` link with a prefilled subject/body (LEO-FEEDBACK-
    UIUX.md §6) -- no email is ever sent server-side; this only opens the
    resident's own mail client with the text already filled in."""
    truncated = len(body) > _MAX_MAILTO_BODY_CHARS
    text = body[:_MAX_MAILTO_BODY_CHARS]
    if truncated:
        text += "\n\n[...continues -- use “Copy text” for the full letter]"
    return f"mailto:?subject={_urlquote(subject)}&body={_urlquote(text)}"


def _render_document_actions(
    *, label: str, markdown_text: str, html_download_href: str, textarea_id: str
) -> str:
    """The shared actions row for a composed document (LEO-FEEDBACK-
    UIUX.md §6): **Copy text** (plain text to clipboard, via `app.js`
    reading the paired hidden `<textarea>`) and **Email this** (`mailto:`,
    prefilled) as the two primary actions; the HTML download stays as a
    secondary link. The Markdown download is deliberately not linked here
    at all -- residents don't know what a `.md` file is -- though the
    `.md` API route itself is untouched for anyone who wants it directly."""
    mailto = _mailto_href(subject=f"My {label.lower()}", body=markdown_text)
    return f"""
      <textarea class="visually-hidden" id="{_esc(textarea_id)}"
                aria-hidden="true" tabindex="-1" readonly>{_esc(markdown_text)}</textarea>
      <p class="document-actions">
        <button type="button" class="button--secondary copy-text-button"
                data-copy-source="{_esc(textarea_id)}">Copy text</button>
        <a class="button--secondary" href="{_esc(mailto)}">Email this</a>
        <a class="document-downloads__html" href="{_esc(html_download_href)}">Download HTML</a>
      </p>
    """


def _render_submission_composed_item(case_id: str, event: CaseEvent) -> str:
    submission_html = str(event.payload.get("submission_html", ""))
    submission_markdown = str(event.payload.get("submission_markdown", ""))
    refusals_html = str(event.payload.get("refusals_html", ""))
    refusals_markdown = str(event.payload.get("refusals_markdown", ""))
    base = f"/api/cases/{_esc(case_id)}"
    submission_actions = _render_document_actions(
        label="objection submission",
        markdown_text=submission_markdown,
        html_download_href=f"{base}/submission.html",
        textarea_id=f"submission-text-{event.sequence}",
    )
    refusals_actions = _render_document_actions(
        label="refusals explainer",
        markdown_text=refusals_markdown,
        html_download_href=f"{base}/refusals.html",
        textarea_id=f"refusals-text-{event.sequence}",
    )
    return f"""<li class="submission-package">
      <div class="document-preview">{submission_html}</div>
      {submission_actions}
      <div class="document-preview">{refusals_html}</div>
      {refusals_actions}
    </li>"""


_DOCUMENT_KIND_LABELS: Mapping[DocumentKind, str] = {
    DocumentKind.ELEVATIONS: "Elevations",
    DocumentKind.SITE_PLAN: "Site plan",
    DocumentKind.SEE: "Statement of Environmental Effects",
    DocumentKind.SHADOW_DIAGRAM: "Shadow diagram",
    DocumentKind.SURVEY: "Survey",
    DocumentKind.BASIX: "BASIX certificate",
    DocumentKind.WASTE: "Waste management plan",
    DocumentKind.OTHER: "Document",
}
"""`DocumentKind` -> plain-English label (UI-SPEC.md §2.4) -- the raw enum
value is never shown to a resident."""


def _humanize_filename(filename: str) -> str:
    """`"north-elevation.pdf"` -> `"North elevation"` -- a plain-English
    doc-card title, not the raw filename with its extension and dashes."""
    stem = filename.rsplit(".", 1)[0] if filename else ""
    words = [w for w in re.split(r"[-_\s]+", stem) if w]
    if not words:
        return "Document"
    first, *rest = words
    return " ".join([first.capitalize(), *(w.lower() for w in rest)])


_SYDNEY_TZ: Final = ZoneInfo("Australia/Sydney")
"""Every timestamp shown to a resident renders in this timezone, converted
from whatever tz-aware `datetime` was actually stored (UTC in production,
`state.firestore._utcnow`) -- LEO-FEEDBACK-UIUX.md §7: timestamps
previously rendered a bare `dt.hour`/`dt.minute` as if the stored UTC value
were already local Sydney time, silently wrong by up to 11 hours."""


def _to_sydney(dt: datetime) -> datetime:
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return aware.astimezone(_SYDNEY_TZ)


def _format_clock_time(dt: datetime) -> str:
    """`"2:14pm"` in Australia/Sydney time -- not an ISO timestamp (copy
    tone guide §4 rule 2), and not the stored UTC clock time read verbatim
    (the bug this fixes)."""
    sydney = _to_sydney(dt)
    hour = sydney.hour % 12 or 12
    period = "am" if sydney.hour < 12 else "pm"
    return f"{hour}:{sydney.minute:02d}{period}"


def _transcript_line(event: CaseEvent) -> str:
    role = str(event.payload.get("role", "system"))
    speaker = "You" if role == "resident" else "Setback"
    message = str(event.payload.get("message", ""))
    return f"{speaker}  {_format_sydney_export_timestamp(event.recorded_at)}  {message}"


def _render_transcript_text(events: Sequence[CaseEvent]) -> str:
    """Plain-text lines like `"Setback  2026-08-29 19:42 AEST  <message>"`
    (LEO-FEEDBACK-UIUX.md §2's exact format) for `GET /transcript.txt`."""
    turn_events = sorted(
        (e for e in events if e.event_type == "interview_turn"), key=lambda e: e.sequence
    )
    if not turn_events:
        return ""
    return "\n".join(_transcript_line(e) for e in turn_events) + "\n"


def _format_sydney_export_timestamp(dt: datetime) -> str:
    """`"2026-08-29 19:42 AEST"` -- the literal export-transcript line
    format (LEO-FEEDBACK-UIUX.md §2), distinct from `_format_sydney_
    timestamp`'s human "29 Aug 2026, 7:42pm" prose form used elsewhere on
    the page."""
    sydney = _to_sydney(dt)
    return f"{sydney.strftime('%Y-%m-%d %H:%M')} {sydney.tzname()}"


def _format_sydney_timestamp(dt: datetime) -> str:
    """An absolute Australia/Sydney date+time, e.g. `"29 Aug 2026, 7:42pm
    AEST"` -- LEO-FEEDBACK-UIUX.md §7's "with the date" requirement, used
    wherever a tribunal/event timestamp needs to be unambiguous rather than
    just a same-day clock time (`_format_clock_time`)."""
    sydney = _to_sydney(dt)
    return f"{sydney.day} {sydney.strftime('%b %Y')}, {_format_clock_time(dt)} {sydney.tzname()}"


def _format_started_at(dt: datetime) -> str:
    """`"29/08/2026 06:35 AM"` -- the exact `DD/MM/YYYY HH:MM AM/PM` format
    (round-2 UI feedback, item 4) for showing when the tribunal was started
    directly in the case header, in Australia/Sydney time. This replaces
    the standalone "Tribunal" tab/section, whose only irreplaceable
    resident-facing content was this timestamp (see `_render_case_meta_
    line`/`_render_case_notes_section` for where its other two contents --
    the run-cost figure and the live-ingest line -- moved to instead)."""
    sydney = _to_sydney(dt)
    hour = sydney.hour % 12 or 12
    period = "AM" if sydney.hour < 12 else "PM"
    return (
        f"{sydney.day:02d}/{sydney.month:02d}/{sydney.year} {hour:02d}:{sydney.minute:02d} {period}"
    )


def _is_photo_upload(filename: str, content_type: object) -> bool:
    """Mirrors `job.pipeline._UploadedDocument.is_pdf`'s exact rule (a
    non-PDF upload is treated as a photo) so the doc-card's provenance
    badge, shown at upload time, agrees with the grade the tribunal
    pipeline will actually assign this same document later."""
    content_type_str = str(content_type or "").lower()
    is_pdf = "pdf" in content_type_str or filename.lower().endswith(".pdf")
    return not is_pdf


_STREET_VIEW_FILENAME_PREFIX: Final[str] = "Street View fallback ("


def _street_view_attribution(filename: str) -> str:
    """Recovers the visible attribution text Google's Street View terms
    require (`evidence.imagery`'s module docstring) from the fallback
    document's own filename -- `job.pipeline._street_view_fallback_document`
    bakes it in as `f"Street View fallback ({fallback.attribution})"` since
    that's the only field this event carries it in. `_render_document_
    uploaded_item` overrides the doc-card's *title* to the friendlier
    "Street View (archival)", which would otherwise silently drop this
    attribution from the visible page entirely. Falls back to the raw
    filename for an older persisted event that predates this exact shape
    (an append-only log is never rewritten) -- degraded but never blank."""
    if filename.startswith(_STREET_VIEW_FILENAME_PREFIX) and filename.endswith(")"):
        return filename[len(_STREET_VIEW_FILENAME_PREFIX) : -1]
    return filename


def _render_document_uploaded_item(case_id: str, event: CaseEvent) -> str:
    """A `.doc-card` (UI-SPEC.md §2.4/§3.3). A photo upload gets a real
    `<img>` thumbnail -- the bytes already exist in whichever
    `EvidenceUploadStore` this app was built with (in-memory in tests, GCS
    in production), served back via this case's own
    `GET /api/cases/{case_id}/documents/{document_id}` route, so this needs
    no new storage or thumbnail-generation pipeline. A PDF upload keeps the
    placeholder-icon variant (no PDF-preview pipeline exists, and none was
    asked for). The `DocumentKind` is classified via the clerk's own
    deterministic, no-model-call fallback (`_classify_document_by_keywords`)
    over the filename alone -- the same fallback the clerk itself degrades
    to on a Gemma outage, so this never makes a live call and never
    blocks."""
    payload = event.payload
    filename = str(payload.get("filename") or "document")
    content_type = payload.get("content_type")
    document_id = payload.get("document_id")
    kind = _classify_document_kind_offline(filename, "")
    kind_label = _DOCUMENT_KIND_LABELS.get(kind, "Document")
    # LEO-FEEDBACK-UIUX.md §4: the Street View grade-B fallback
    # (`job.pipeline._record_street_view_fallback_event`) is the one
    # `document_uploaded` producer that isn't a resident's own upload --
    # it carries its own `provenance_grade`/attribution rather than a
    # human-typed filename, so it gets its own title/badge instead of
    # being mislabelled "Your photo".
    is_street_view = payload.get("provenance_grade") == ProvenanceGrade.STREET_VIEW_SOLAR_FALLBACK
    title = "Street View (archival)" if is_street_view else _humanize_filename(filename)
    uploaded_at = _format_clock_time(event.recorded_at)
    is_photo = _is_photo_upload(filename, content_type)
    grade_badge = ""
    attribution_text = ""
    if is_street_view:
        grade_badge = (
            f'<span class="tag tag--grade-b" title="Provenance grade B -- {_esc(filename)}">'
            "Archival Street View</span>"
        )
        # Founder bug report (P1) fix, part 2: the attribution Google's
        # Street View terms require must be genuinely visible on the page,
        # not just present as a hover-only tooltip's substring -- see
        # `_street_view_attribution`'s docstring.
        attribution_text = _street_view_attribution(filename)
    elif is_photo:
        grade_badge = (
            '<span class="tag tag--grade-a" title="Provenance grade A -- your own photo">'
            "Your photo</span>"
        )
    doc_url = f"/api/cases/{_esc(case_id)}/documents/{_esc(document_id)}" if document_id else None
    if is_photo and doc_url:
        thumb = f'<img class="doc-card__thumb" src="{doc_url}" alt="{_esc(title)}">'
    else:
        thumb = '<div class="doc-card__thumb doc-card__thumb--placeholder"></div>'
    attribution_html = (
        f'<p class="doc-card__attribution">{_esc(attribution_text)}</p>' if attribution_text else ""
    )
    card_body = (
        f"{thumb}"
        '<div class="doc-card__body">'
        f'<p class="doc-card__title">{_esc(title)}</p>'
        f'<p class="doc-card__meta">{_esc(kind_label)} &middot; uploaded {_esc(uploaded_at)}</p>'
        f"{attribution_html}"
        "</div>"
        f"{grade_badge}"
    )
    # LEO-FEEDBACK-UIUX.md §4 (original): a doc-card must open the full
    # image/PDF, never sit inert.
    #
    # Founder bug report (P1) fix, part 1: an *image* evidence card
    # (a resident's own photo, or this Street View fallback) now opens in
    # the same in-page lightbox the annotated overlay uses
    # (`app.js`'s `wireDocCardLightbox`/`openLightbox`) instead of a new
    # tab -- carried entirely as data attributes, since a lightbox has
    # nothing server-rendered to hook into ahead of time. A PDF doc-card
    # is untouched: a lightbox `<img>` cannot render a PDF, so it keeps
    # the plain new-tab link.
    if is_photo and doc_url is not None:
        caption_attr = (
            f' data-lightbox-caption="{_esc(attribution_text)}"' if attribution_text else ""
        )
        card = (
            '<div class="doc-card doc-card--clickable doc-card--lightbox" '
            'role="button" tabindex="0" '
            f'data-lightbox-src="{doc_url}" data-lightbox-alt="{_esc(title)}"'
            f"{caption_attr}>{card_body}</div>"
        )
    elif doc_url is not None:
        card = (
            f'<a class="doc-card doc-card--clickable" href="{doc_url}" '
            f'target="_blank" rel="noopener">{card_body}</a>'
        )
    else:
        card = f'<div class="doc-card">{card_body}</div>'
    return f"<li>{card}</li>"


def _render_ingest_resolved_item(event: CaseEvent) -> str:
    """`job.pipeline.RealPipelineRunner.run`'s `ingest_resolved` event
    (wave 9's un-frozen ingest) -- plain-English handoff for whoever owns
    `console/app.py`, per the fixer's cross-lane note: this must never fall
    through to the raw-JSON branch, and a resident whose typed DA number
    could not be resolved live deserves to know their submission is using
    the demo case's letterhead instead, not silently guess why."""
    payload = event.payload
    application_number = str(payload.get("application_number", ""))
    if payload.get("used_demo_fixture"):
        council_number = str(payload.get("council_application_number", ""))
        return (
            '<li class="case-note">Could not fetch '
            f"{_esc(application_number)} live; showing the demo case "
            f"({_esc(council_number)}) instead.</li>"
        )
    return f'<li class="case-note">Fetched live council data for {_esc(application_number)}.</li>'


def _render_tribunal_rerun_ignored_item(event: CaseEvent) -> str:
    """`job.pipeline.RealPipelineRunner.run`'s idempotency guard (SMOKE.md's
    "Fix 4"): a judge pressing "Start tribunal" a second time against an
    already-decided case makes no changes -- told here in plain English
    rather than dropped or raw-JSON-dumped."""
    return (
        '<li class="case-note">This case&rsquo;s tribunal has already '
        "run &mdash; nothing further happened.</li>"
    )


_CASE_NOTES_EVENT_TYPES: Final[tuple[str, ...]] = ("ingest_resolved", "tribunal_rerun_ignored")
"""Every event type the small "Notes" card (`_render_case_notes_section`)
draws from -- what remains of the round-2-removed standalone "Tribunal"
tab/section once its start-timestamp moved to the case header
(`_format_started_at`/`_render_case_meta_line`) and `tribunal_requested`
itself needed no other rendering (its only resident-facing content *was*
that timestamp). `ground_rerun_skipped` is deliberately absent (an
internal resume-safety signal with no resident-facing value, per the
fixer's own cross-lane note)."""


def _render_case_notes_section(events: Sequence[CaseEvent]) -> str:
    """A small "Notes" card living inside the Grounds tab (round-2 UI
    feedback, item 4): the live-ingest-source line and the tribunal-rerun-
    ignored notice that used to live in the now-removed standalone
    "Tribunal" tab/section. Demo-valuable info, never dropped -- only
    relocated, per the item's own instruction. Renders nothing (not even
    an empty-state card) when this case has neither event yet, since an
    empty "Notes" card ahead of the grounds list would be visual noise for
    the overwhelmingly common case (a fresh interview, no tribunal run
    yet)."""
    renderers: Mapping[str, Callable[[CaseEvent], str]] = {
        "ingest_resolved": _render_ingest_resolved_item,
        "tribunal_rerun_ignored": _render_tribunal_rerun_ignored_item,
    }
    relevant = sorted(
        (e for e in events if e.event_type in _CASE_NOTES_EVENT_TYPES), key=lambda e: e.sequence
    )
    if not relevant:
        return ""
    items = "".join(renderers[e.event_type](e) for e in relevant)
    return f'<section class="card case-notes"><h3>Notes</h3><ul class="event-list">{items}</ul></section>'  # noqa: E501


_ADJUDICATION_STANCE_LABELS: Mapping[str, str] = {
    "support": "supports this ground",
    "reject": "does not support this ground",
}
"""`ReviewStance` value -> plain-English verdict phrase, mirroring
`_DOCUMENT_KIND_LABELS`' rule that a raw enum value is never shown."""


def _render_adjudication_decision_item(event: CaseEvent) -> str:
    """The adjudicator's final call on a contested ground -- reached the
    raw-JSON fallback branch before this fix, the same live bug class as
    `tribunal_requested` above."""
    payload = event.payload
    stance = str(payload.get("stance", ""))
    stance_label = _ADJUDICATION_STANCE_LABELS.get(stance, stance)
    confidence = payload.get("confidence")
    confidence_pct = f"{float(confidence) * 100:.0f}%" if confidence is not None else "unknown"
    rationale = str(payload.get("rationale", ""))
    return (
        '<li class="adjudication-decision">'
        f"<strong>Adjudicator</strong> {_esc(stance_label)} "
        f"(confidence {_esc(confidence_pct)})"
        f"<br><em>{_esc(rationale)}</em></li>"
    )


def _render_resident_refusal_feedback_item(event: CaseEvent) -> str:
    """The resident's recorded pushback on a refusal, and Setback's
    acknowledging restatement (`interview.flow.capture_refusal_feedback`)
    -- reached the raw-JSON fallback branch before this fix, the same live
    bug class as `tribunal_requested`/`adjudication_decision` above."""
    payload = event.payload
    pushback = str(payload.get("pushback", ""))
    re_rendered = str(payload.get("re_rendered_explanation", ""))
    return (
        '<li class="refusal-feedback">'
        f'<p class="refusal-feedback__pushback">&ldquo;{_esc(pushback)}&rdquo;</p>'
        f'<p class="refusal-feedback__response">{_esc(re_rendered)}</p>'
        "</li>"
    )


_EVENT_ITEM_RENDERERS: Mapping[str, Callable[[str, CaseEvent], str]] = {
    "annotated_overlay": _render_annotated_overlay_item,
    "submission_composed": _render_submission_composed_item,
    "document_uploaded": _render_document_uploaded_item,
    "resident_refusal_feedback": lambda _case_id, e: _render_resident_refusal_feedback_item(e),
}
"""Event types rendered via the `(case_id, event) -> html` shape, one flat
list section apiece. `interview_turn`, `review_verdict`, `adjudication_
decision`, and `gate_decision` are deliberately absent -- the interview
transcript lives only in the chat pane now, and the other three render
INSIDE each ground's own accordion (`_render_ground_card`, called from
`_render_grounds_section`) rather than as separate, unlinked flat lists --
see `render_case_page`."""


def _render_events_section(
    case_id: str, event_type: str, title: str, events: Sequence[CaseEvent]
) -> str:
    # No section-level `id` here (round-2 UI feedback, item 1): each of
    # these sections now renders as the sole content of its own tabpanel
    # (`_render_section_panel`), which already carries the addressable
    # `id="panel-<tab>"` -- a second, redundant anchor id on the section
    # itself served no purpose once the sticky anchor-link nav it supported
    # was replaced by real tabs.
    if not events:
        return (
            f'<section class="card"><h3>{_esc(title)}</h3>'
            '<p class="empty">Nothing yet.</p></section>'
        )
    renderer = _EVENT_ITEM_RENDERERS.get(event_type)
    if renderer is not None:
        items = "".join(renderer(case_id, e) for e in events)
    else:
        items = "".join(
            f'<li><span class="event-seq">#{e.sequence}</span> '
            f"{_esc(json.dumps(dict(e.payload)))}</li>"
            for e in events
        )
    return (
        f'<section class="card"><h3>{_esc(title)}</h3><ul class="event-list">{items}</ul></section>'
    )


_GROUND_STATUS_MODIFIER_AND_LABEL: Mapping[GroundStatus, tuple[str, str]] = {
    # All 5 `GroundStatus` values covered (UI-SPEC.md §2.6/§3.6) -- the
    # pre-wave-5 code only styled 3 of them, leaving `proposed`/`under_
    # review` unstyled. Both map to the neutral `pending` token; the other
    # three map 1:1 onto their own status.
    GroundStatus.PROPOSED: ("pending", "Pending"),
    GroundStatus.UNDER_REVIEW: ("pending", "Pending"),
    GroundStatus.SUPPORTED: ("shipped", "Shipped"),
    GroundStatus.REFUSED: ("refused", "Refused"),
    GroundStatus.FLAGGED: ("flagged", "Flagged"),
}


def _render_ground_card(
    ground: GroundRecord,
    gate_decision: Mapping[str, Any] | None,
    review_verdicts: Sequence[CaseEvent],
    adjudication: CaseEvent | None,
    total_grounds: int,
) -> str:
    """One ground as an accordion (LEO-FEEDBACK-UIUX.md §3): a clamped
    one-liner of the resident's own words + a status pill, always visible;
    the statutory basis/explanation, the reviewers' opinions, and (for a
    refusal) the gate's ground-naming refusal card all live inside the
    `<details>` body, collapsed by default. Native `<details>`/`<summary>`
    -- no JS needed for the expand/collapse itself, and it's keyboard- and
    screen-reader-accessible for free."""
    modifier, label = _GROUND_STATUS_MODIFIER_AND_LABEL[ground.status]
    detail_html = ""
    if gate_decision is not None:
        detail_html += _render_gate_detail(ground, gate_decision, total_grounds)
    opinions_html = "".join(_render_review_verdict_item(e) for e in review_verdicts)
    if adjudication is not None:
        opinions_html += _render_adjudication_decision_item(adjudication)
    if opinions_html:
        detail_html += (
            '<div class="ground-card__opinions"><h5>What the reviewers said</h5>'
            f'<ul class="event-list">{opinions_html}</ul></div>'
        )
    if not detail_html:
        detail_html = '<p class="empty">Still under review.</p>'
    return (
        f'<li class="ground-card ground-card--{modifier}">'
        '<details class="ground-card__accordion">'
        '<summary class="ground-card__summary">'
        f'<span class="ground-card__claim">{_esc(ground.claim)}</span>'
        f'<span class="tag tag--{modifier}">{_esc(label)}</span>'
        "</summary>"
        f'<div class="ground-card__body">{detail_html}</div>'
        "</details></li>"
    )


def _render_grounds_section(
    grounds: Sequence[GroundRecord],
    gate_decisions_by_ground: Mapping[str, Mapping[str, Any]],
    review_verdicts_by_ground: Mapping[str, Sequence[CaseEvent]],
    adjudication_by_ground: Mapping[str, CaseEvent],
) -> str:
    if not grounds:
        return (
            '<section class="card" id="grounds"><h3>Grounds</h3>'
            '<p class="empty">No grounds proposed yet.</p></section>'
        )
    total_grounds = len(grounds)
    items = "".join(
        _render_ground_card(
            g,
            gate_decisions_by_ground.get(g.ground_id),
            review_verdicts_by_ground.get(g.ground_id, ()),
            adjudication_by_ground.get(g.ground_id),
            total_grounds,
        )
        for g in grounds
    )
    return (
        '<section class="card" id="grounds"><h3>Grounds</h3>'
        f'<ul class="ground-list">{items}</ul></section>'
    )


def _render_check_answers_section(
    grounds: Sequence[GroundRecord], events: Sequence[CaseEvent]
) -> str:
    """The GOV.UK-pattern check-your-answers recap (UI-SPEC.md §3.8), shown
    once the interview reaches `InterviewStage.DONE`. Read-only: the
    interview state machine cannot reopen an arbitrary past stage this
    wave, so per the spec's own graceful-degradation rule this ships a
    single "Change" link that reopens the full transcript rather than a
    fake per-row edit that would not actually work."""
    interview_done = any(
        e.event_type == "interview_turn" and e.payload.get("stage") == InterviewStage.DONE.value
        for e in events
    )
    if not interview_done or not grounds:
        return ""
    document_count = sum(1 for e in events if e.event_type == "document_uploaded")
    rows = "".join(
        f'<div class="summary-list__row"><dt>Ground {i}</dt><dd>{_esc(g.claim)}</dd></div>'
        for i, g in enumerate(grounds, start=1)
    )
    rows += (
        '<div class="summary-list__row"><dt>Evidence</dt>'
        f"<dd>{document_count} document(s) uploaded</dd></div>"
    )
    return f"""
    <section class="card check-answers">
      <h3>Check your answers before we check them against the Act</h3>
      <dl class="summary-list">{rows}</dl>
      <a class="summary-list__change" href="#interview-transcript">Change something</a>
    </section>
    """


def _tribunal_button_state(events: Sequence[CaseEvent]) -> tuple[bool, str]:
    """`(disabled, label)` for the "Start tribunal" button (LEO-FEEDBACK-
    UIUX.md §7): un-crashable-by-construction on the UI side -- disabled
    with an honest label once a submission has already been composed
    (re-running against an already-adjudicated case is the known job-side
    crash, SMOKE.md's "Fix 4 -- not fixed", out of this lane's files) or
    while a run is genuinely in flight; enabled again after a failed
    attempt so a resident isn't locked out by a transient error."""
    if any(e.event_type == "submission_composed" for e in events):
        return True, "Tribunal complete"
    start_sequence = max(
        (e.sequence for e in events if e.event_type == "tribunal_requested"), default=None
    )
    if start_sequence is None:
        return False, "Start tribunal"
    terminal_sequence = max(
        (e.sequence for e in events if e.event_type in ("submission_composed", "job_failed")),
        default=None,
    )
    if terminal_sequence is None or terminal_sequence < start_sequence:
        return True, "Tribunal running…"
    return False, "Start tribunal"


_SECTION_TABS: Final[tuple[tuple[str, str], ...]] = (
    ("grounds", "Grounds"),
    ("evidence", "Evidence"),
    ("overlay", "Overlay"),
    ("documents", "Documents"),
)
"""The case page's right-pane tab set (round-2 UI feedback, item 1). The
former sticky anchor-link nav (LEO-FEEDBACK-UIUX.md §9) rendered every
section at once and merely scrolled to one on click -- not a real tab
component, per the founder's own correction ("it's not a ref link for the
page block, it's an interactive component that renders the associated
content when it's selected"). Every panel is still server-rendered in
full (progressive enhancement, and so a reader with JS disabled can at
least read every section by disabling `[hidden]` in devtools); `app.js`
toggles which single one is visible via the `hidden` attribute and
`aria-selected`, driven by real WAI-ARIA tablist keyboard semantics
(arrow keys/Home/End). Default active tab is Grounds (index 0) -- the
founder's own instruction. "Tribunal" was removed as its own tab (item
4): see `_format_started_at`/`_render_case_meta_line` and
`_render_case_notes_section` for where its three pieces of content moved
instead."""


def _render_section_tabs() -> str:
    buttons = "".join(
        f'<button type="button" role="tab" id="tab-{tab_id}" aria-controls="panel-{tab_id}" '
        f'aria-selected="{"true" if i == 0 else "false"}" tabindex="{0 if i == 0 else -1}" '
        f'class="tab">{_esc(label)}</button>'
        for i, (tab_id, label) in enumerate(_SECTION_TABS)
    )
    return f'<div class="section-tabs" role="tablist" aria-label="Case sections">{buttons}</div>'


def _render_section_panel(tab_id: str, tab_index: int, content: str) -> str:
    hidden_attr = "" if tab_index == 0 else " hidden"
    return (
        f'<div role="tabpanel" id="panel-{tab_id}" aria-labelledby="tab-{tab_id}" '
        f'tabindex="0"{hidden_attr}>{content}</div>'
    )


def _render_case_meta_line(events: Sequence[CaseEvent], ledger: Ledger | None) -> str:
    """The case header's small meta line (round-2 UI feedback, item 4): the
    tribunal-start timestamp (in the exact `DD/MM/YYYY HH:MM AM/PM` format
    requested, Australia/Sydney) and, once non-zero, the run-cost figure --
    both demoted from the now-removed standalone "Tribunal" tab/section
    rather than dropped. Renders nothing before a tribunal has ever been
    requested (there is nothing yet to report)."""
    start_event = max(
        (e for e in events if e.event_type == "tribunal_requested"),
        key=lambda e: e.sequence,
        default=None,
    )
    if start_event is None:
        return ""
    parts = [f"Tribunal started {_esc(_format_started_at(start_event.recorded_at))}"]
    run_cost_usd = ledger.total_cost_usd if ledger is not None else 0.0
    if run_cost_usd > 0:
        cost_text = f"${run_cost_usd:.2f}" if run_cost_usd >= 0.01 else f"${run_cost_usd:.4f}"
        parts.append(f"Run cost: {_esc(cost_text)}")
    return f'<p class="case-meta">{" &middot; ".join(parts)}</p>'


def _has_overshadowing_ground(events: Sequence[CaseEvent]) -> bool:
    """Whether this case has raised an overshadowing concern, per its own
    `ground_category_assigned` event(s) (`_propose_ground_for_confirmed_
    concern`'s `concern_type` payload field) -- the one gate the
    overshadowing-simulation card (`_render_simulation_clip_card`) shares
    with `simulation_clip_for_case`'s demo-case-id check, mirroring the
    Street View grade-B fallback's own always-gated-on-real-content
    conditional-rendering pattern."""
    return any(
        e.event_type == "ground_category_assigned"
        and e.payload.get("concern_type") == "overshadowing"
        for e in events
    )


def _render_simulation_clip_card(clip: Any) -> str:
    """The overshadowing-simulation `<video>` card (RECOMMENDATION.md's
    minimal integration plan, item 2): a hazard-styled card, visually
    distinct from the neutral-grey provenance badges used for real
    evidence documents, carrying the mandatory non-dismissible label and a
    one-line explainer directly under the clip -- it can never be mistaken
    for the real Street View photo or the real annotated plan elevation
    next to it. `clip` is a `setback.evidence.illustration.SimulationClip`
    (typed loosely here only to avoid a hard import-order dependency in
    this module's own type-checking pass)."""
    return (
        '<section class="card simulation-card">'
        '<span class="tag tag--simulation">Simulation</span>'
        f'<video controls preload="none" src="{_esc(clip.static_path)}" '
        f'aria-label="{_esc(clip.caption)}">'
        "Your browser does not support embedded video."
        "</video>"
        f'<p class="simulation-card__label">{_esc(ILLUSTRATION_LABEL)}</p>'
        f'<p class="simulation-card__explainer">{_esc(ILLUSTRATION_EXPLAINER)}</p>'
        f'<p class="simulation-card__cost">{_esc(ILLUSTRATION_COST_NOTE)}</p>'
        "</section>"
    )


def render_case_page(
    case: CaseRecord,
    grounds: Sequence[GroundRecord],
    events: Sequence[CaseEvent],
    ledger: Ledger | None = None,
    *,
    force_theme: str | None = None,
    paused: bool = False,
) -> str:
    """Render the case page: interview transcript, evidence, reviewer
    opinions, adjudication, gate decisions with refusal explanations, and
    output documents -- each section reads directly from the case's event
    log, so it renders correctly (as "nothing yet") for every stage the
    tribunal pipeline hasn't reached, and fills in automatically once a
    future wave starts emitting that event type.

    `ledger`: the case's token-spend ledger (`CaseStore.load_ledger`), if
    any run has booked cost against it yet. Cost-visibility carry-forward
    (wave-4 -> wave-5): there was previously no UI/API surface for this at
    all. This package exposes the total as a `data-run-cost-usd` attribute
    on `<body>` -- the tribunal-timeline "This run: $0.02" chip itself is
    package C's (`app.js`) to render, reading this attribute.

    `force_theme`: see `_html_tag` -- an opt-in `?theme=light`/`dark`
    override for filming consistency, never a default.

    `paused`: the public-abuse guard's current state (DESIGN SPEC point 4)
    -- this page (and every read) stays fully reachable either way; only
    the calm banner changes.
    """
    by_type: dict[str, list[CaseEvent]] = {}
    for event in events:
        by_type.setdefault(event.event_type, []).append(event)

    gate_decisions_by_ground: dict[str, Mapping[str, Any]] = {
        str(e.payload.get("ground_id", "")): e.payload for e in by_type.get("gate_decision", ())
    }
    review_verdicts_by_ground: dict[str, list[CaseEvent]] = {}
    for e in by_type.get("review_verdict", ()):
        review_verdicts_by_ground.setdefault(str(e.payload.get("ground_id", "")), []).append(e)
    adjudication_by_ground: dict[str, CaseEvent] = {
        str(e.payload.get("ground_id", "")): e for e in by_type.get("adjudication_decision", ())
    }

    grounds_section = _render_grounds_section(
        grounds, gate_decisions_by_ground, review_verdicts_by_ground, adjudication_by_ground
    )
    check_answers_section = _render_check_answers_section(grounds, events)
    case_notes_section = _render_case_notes_section(events)
    resident_feedback_section = _render_events_section(
        case.case_id,
        "resident_refusal_feedback",
        _EVENT_SECTION_TITLES["resident_refusal_feedback"],
        by_type.get("resident_refusal_feedback", ()),
    )
    evidence_section = _render_events_section(
        case.case_id,
        "document_uploaded",
        _EVENT_SECTION_TITLES["document_uploaded"],
        by_type.get("document_uploaded", ()),
    )
    simulation_clip = simulation_clip_for_case(
        case.case_id, has_overshadowing_ground=_has_overshadowing_ground(events)
    )
    if simulation_clip is not None:
        evidence_section += _render_simulation_clip_card(simulation_clip)
    overlay_section = _render_events_section(
        case.case_id,
        "annotated_overlay",
        _EVENT_SECTION_TITLES["annotated_overlay"],
        by_type.get("annotated_overlay", ()),
    )
    documents_section = _render_events_section(
        case.case_id,
        "submission_composed",
        _EVENT_SECTION_TITLES["submission_composed"],
        by_type.get("submission_composed", ()),
    )
    # Tab order/ids/labels come from `_SECTION_TABS` -- the single source
    # of truth both `_render_section_tabs` (the tablist buttons) and this
    # panel assembly read from, so a tab button and its panel can never
    # drift out of sync (round-2 UI feedback, item 1).
    panel_content: Mapping[str, str] = {
        "grounds": check_answers_section
        + case_notes_section
        + grounds_section
        + resident_feedback_section,
        "evidence": evidence_section,
        "overlay": overlay_section,
        "documents": documents_section,
    }
    panels = "".join(
        _render_section_panel(tab_id, i, panel_content[tab_id])
        for i, (tab_id, _label) in enumerate(_SECTION_TABS)
    )
    last_sequence = max((e.sequence for e in events), default=-1)
    run_cost_usd = ledger.total_cost_usd if ledger is not None else 0.0
    tribunal_disabled, tribunal_label = _tribunal_button_state(events)
    tribunal_disabled_attr = " disabled" if tribunal_disabled else ""
    case_meta_line = _render_case_meta_line(events, ledger)

    return f"""
<!doctype html>
{_html_tag(force_theme)}
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Setback -- {_esc(case.application_number)}</title>
  {_PAGE_STYLE}
</head>
<body data-case-id="{_esc(case.case_id)}" data-last-sequence="{last_sequence}"
      data-run-cost-usd="{run_cost_usd:.6f}">
  <header class="topbar">
    <h1><a href="/">Setback</a></h1>
    <p class="tagline">Case {_esc(case.application_number)} &middot; {_esc(case.case_id)}</p>
    {case_meta_line}
    <div class="case-actions">
      <button type="button" id="copy-link-button" class="button--secondary"
              data-case-path="/cases/{_esc(case.case_id)}">Copy link</button>
      <img class="case-actions__qr" src="/api/cases/{_esc(case.case_id)}/qr.png"
           alt="QR code linking back to this case" width="72" height="72" loading="lazy">
      {_THEME_TOGGLE_BUTTON}
    </div>
  </header>
  {_guard_paused_banner_html(paused)}
  <main class="case-layout">
    <aside class="case-layout__chat">
      <section class="card chat-card">
        <h3>Collaborative Partner</h3>
        <div id="interview-transcript" class="chat-transcript" aria-live="polite"></div>
        <div id="typing-indicator" class="typing-indicator" hidden aria-hidden="true">
          <span></span><span></span><span></span>
        </div>
        <form id="interview-form" class="chat-form">
          <label for="interview-input" class="visually-hidden">Your answer</label>
          <input id="interview-input" type="text" placeholder="Type your answer..."
                 autocomplete="off">
          <button type="submit" class="chat-form__send">Send</button>
          <input id="upload-input" type="file" accept="image/*,application/pdf"
                 class="visually-hidden" tabindex="-1" aria-hidden="true">
          <button type="button" id="upload-trigger" class="button--secondary chat-form__upload"
                  aria-label="Upload a photo or document">
            <span aria-hidden="true">&#128206;</span>
            <span class="chat-form__upload-label">Upload</span>
          </button>
        </form>
        <p id="upload-status-chip" class="upload-chip" hidden aria-live="polite"></p>
        <button id="start-tribunal" type="button"
                data-idle-label="Start tribunal"{tribunal_disabled_attr}>
          {_esc(tribunal_label)}
        </button>
        <a class="chat-card__export" href="/api/cases/{_esc(case.case_id)}/transcript.txt"
           download>Export transcript</a>
      </section>
    </aside>
    <div class="case-layout__sections">
      {_render_section_tabs()}
      <div class="section-tabpanels">
        {panels}
      </div>
    </div>
  </main>
{_DISCLAIMER_FOOTER}
  <script src="/static/app.js"></script>
</body>
</html>
"""


# --- production wiring --------------------------------------------------------


def _build_production_app() -> FastAPI:
    """Construct the production app with real GCP-backed dependencies.

    Constructing `FirestoreCaseStore()`/`ModelClient()`/`GcsEvidenceStore()`
    builds client objects only -- none makes a network call in its
    constructor -- so this runs safely at import time (needed for `uvicorn
    setback.console.app:app`) without ever touching the network during
    test collection.

    `SETBACK_LOCAL_TRIBUNAL=1` swaps in `LocalPipelineJobTrigger` (see its
    docstring) for local/dev testing -- unset (the default, and always
    unset on the deployed Cloud Run Service), `start_tribunal` triggers a
    real `setback-tribunal` Cloud Run Job execution via `RealJobTrigger`.
    Uploads always go through `GcsEvidenceStore` in production regardless
    of which trigger is active, so a real job execution (a separate
    container) can see them either way.
    """
    from setback.evidence.storage import GcsEvidenceStore
    from setback.state.firestore import FirestoreCaseStore
    from setback.state.guard_store import FirestoreGuardCounterStore, FirestoreGuardTotalsStore

    store = FirestoreCaseStore()
    document_source = GcsEvidenceStore()
    model_client = ModelClient()

    job_trigger: JobTrigger = RealJobTrigger()
    if os.environ.get("SETBACK_LOCAL_TRIBUNAL") == "1":
        job_trigger = LocalPipelineJobTrigger(
            store=store, document_source=document_source, model_client=model_client
        )

    return create_app(
        store,
        composer=ModelQuestionComposer(model_client),
        document_source=document_source,
        job_trigger=job_trigger,
        concern_normaliser=ModelConcernNormaliser(model_client),
        # Security-review finding (2026-08-30): these two were declared on
        # `create_app`'s signature (and this very function's docstring
        # already claimed they were wired here) but never actually passed
        # -- meaning the deployed console would have silently fallen back
        # to a fresh in-memory counter/aggregate *per Cloud Run instance*,
        # each reset to zero on every restart/scale event, for both the
        # per-client daily cap and (far more seriously) the global public-
        # spend ceiling the founder is relying on as the one hard blocker
        # on real spend. Firestore-backed, durable, and shared by every
        # instance is the entire point of `state.guard_store`'s Firestore
        # adapters existing at all.
        guard_counter_store=FirestoreGuardCounterStore(),
        guard_totals_store=FirestoreGuardTotalsStore(),
    )


app = _build_production_app()
