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
import secrets
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from setback import config
from setback.console.guards import (
    enforce_concurrent_tribunal_cap,
    enforce_daily_spend_budget,
    per_case_interview_turn_guard,
    per_ip_case_creation_guard,
)
from setback.ingest.tracker import DocumentSource, EvidenceUploadStore, UserUploadedDocumentSource
from setback.interview.flow import (
    ConcernType,
    InterviewFlow,
    InterviewStage,
    InterviewTurn,
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
)

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_DEFAULT_MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB

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


def _turn_to_json(turn: InterviewTurn, transcript: Sequence[InterviewTurn]) -> dict[str, Any]:
    return {
        "stage": turn.stage.value,
        "prompt": turn.prompt,
        "turns": [{"stage": t.stage.value, "prompt": t.prompt} for t in transcript],
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
    claim = concern.initial_statement
    if concern.clarification:
        claim = f"{claim} {concern.clarification}"
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
    max_upload_bytes: int = _DEFAULT_MAX_UPLOAD_BYTES,
    sse_poll_interval_seconds: float = 0.5,
    sse_idle_timeout_seconds: float | None = None,
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
        max_upload_bytes: Hard cap on a single document/photo upload.
        sse_poll_interval_seconds: How often the events stream re-checks
            `store` for new events.
        sse_idle_timeout_seconds: If set, the events stream terminates
            after this many quiet seconds with no new events -- used by
            tests so a stream request completes instead of hanging;
            `None` (the default) polls forever, correct for production.
    """
    documents = document_source if document_source is not None else UserUploadedDocumentSource()
    trigger = job_trigger if job_trigger is not None else LoggingJobTrigger()

    app = FastAPI(title="Setback")
    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    interview_flows: dict[str, InterviewFlow] = {}
    _case_creation_guard = per_ip_case_creation_guard()
    _interview_turn_guard = per_case_interview_turn_guard()

    async def _require_case(case_id: str) -> CaseRecord:
        case = await store.get_case(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail=f"case {case_id!r} not found")
        return case

    @app.post("/api/cases", status_code=201, dependencies=[Depends(_case_creation_guard)])
    async def create_case(body: CreateCaseRequest) -> dict[str, Any]:
        case = await store.create_case(
            application_number=body.application_number, resident_session=body.resident_session
        )
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
            flow = InterviewFlow(composer=composer)
            interview_flows[case_id] = flow
            turn = await flow.start()
            await _persist_system_turn(store, case_id, turn)
        return _turn_to_json(flow.transcript[-1], flow.transcript)

    @app.post("/api/cases/{case_id}/interview", dependencies=[Depends(_interview_turn_guard)])
    async def answer_interview(case_id: str, body: InterviewAnswerRequest) -> dict[str, Any]:
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
        return _turn_to_json(turn, flow.transcript)

    @app.post("/api/cases/{case_id}/documents")
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
        document_id = hashlib.sha256(content).hexdigest()[:16]
        await documents.add_evidence_document(
            case_id, document_id, content, content_type=file.content_type
        )
        await store.append_event(
            case_id,
            f"document-uploaded:{document_id}",
            "document_uploaded",
            payload={
                "document_id": document_id,
                "filename": file.filename,
                "content_type": file.content_type,
                "size_bytes": len(content),
            },
        )
        flow = interview_flows.get(case_id)
        if flow is not None:
            turn = await flow.record_evidence_upload(document_id)
            await _persist_system_turn(store, case_id, turn)
        return JSONResponse({"document_id": document_id, "size_bytes": len(content)})

    @app.post("/api/cases/{case_id}/tribunal", status_code=202)
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

    @app.post("/api/cases/{case_id}/grounds/{ground_id}/feedback")
    async def refusal_feedback(
        case_id: str, ground_id: str, body: RefusalFeedbackRequest
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
        return {
            "ground_id": feedback.ground_id,
            "re_rendered_explanation": feedback.re_rendered_explanation,
        }

    @app.get("/", response_class=HTMLResponse)
    async def docket_board() -> str:
        cases: list[tuple[CaseRecord, tuple[GroundRecord, ...]]] = []
        for case in await store.list_cases():
            cases.append((case, await store.list_grounds(case.case_id)))
        return render_docket_board(cases)

    @app.get("/cases/{case_id}", response_class=HTMLResponse)
    async def case_page(case_id: str) -> str:
        case = await _require_case(case_id)
        grounds = await store.list_grounds(case_id)
        events = await store.list_events(case_id)
        return render_case_page(case, grounds, events)

    return app


# --- server-rendered HTML -----------------------------------------------------


def _esc(text: object) -> str:
    return html.escape(str(text))


_PAGE_STYLE = """
<link rel="stylesheet" href="/static/style.css">
"""


def render_docket_board(cases: Sequence[tuple[CaseRecord, tuple[GroundRecord, ...]]]) -> str:
    """Render the docket board: every case this console instance has
    created, with a live-updating grounds count."""
    rows = "".join(
        f"""
        <a class="docket-row" href="/cases/{_esc(case.case_id)}">
          <div class="docket-row__main">
            <span class="docket-row__app">{_esc(case.application_number)}</span>
            <span class="docket-row__id">{_esc(case.case_id)}</span>
          </div>
          <span class="badge">{len(grounds)} ground(s)</span>
        </a>
        """
        for case, grounds in cases
    )
    if not rows:
        rows = '<p class="empty">No cases yet -- create one to get started.</p>'
    return f"""
<!doctype html>
<html data-theme="light">
<head>
  <meta charset="utf-8">
  <title>Setback -- Docket Board</title>
  {_PAGE_STYLE}
</head>
<body>
  <header class="topbar">
    <h1>Setback</h1>
    <p class="tagline">A Collaborative Partner for planning objections</p>
  </header>
  <main class="container">
    <h2>Docket board</h2>
    <div class="docket-list">
      {rows}
    </div>
  </main>
  <script src="/static/app.js"></script>
</body>
</html>
"""


_EVENT_SECTION_TITLES: Mapping[str, str] = {
    "interview_turn": "Interview transcript",
    "document_uploaded": "Evidence",
    "review_verdict": "Reviewer opinions",
    "adjudication_decision": "Adjudication",
    "gate_decision": "Gate decisions",
    "annotated_overlay": "Annotated evidence overlay",
    "resident_refusal_feedback": "Resident feedback on refusals",
    "submission_composed": "Submission documents",
    "tribunal_requested": "Tribunal",
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


def _render_gate_decision_item(event: CaseEvent) -> str:
    payload = event.payload
    return (
        f'<li class="gate-decision gate-decision--{_esc(payload.get("status", ""))}">'
        f"<strong>{_esc(payload.get('status', ''))}</strong> "
        f"({_esc(payload.get('statutory_basis', ''))})<br>"
        f"{_esc(payload.get('explanation', ''))}</li>"
    )


def _render_annotated_overlay_item(event: CaseEvent) -> str:
    payload = event.payload
    mime_type = _esc(payload.get("mime_type", "image/png"))
    image_base64 = _esc(payload.get("image_base64", ""))
    return (
        '<li class="annotated-overlay">'
        f'<img src="data:{mime_type};base64,{image_base64}" alt="Annotated evidence overlay">'
        "</li>"
    )


def _render_submission_composed_item(case_id: str, event: CaseEvent) -> str:
    submission_html = str(event.payload.get("submission_html", ""))
    refusals_html = str(event.payload.get("refusals_html", ""))
    base = f"/api/cases/{_esc(case_id)}"
    return f"""<li class="submission-package">
      <div class="document-preview">{submission_html}</div>
      <p class="document-downloads">
        <a href="{base}/submission.md" download>Download submission (.md)</a>
        &middot;
        <a href="{base}/submission.html" download>Download submission (.html)</a>
      </p>
      <div class="document-preview">{refusals_html}</div>
      <p class="document-downloads">
        <a href="{base}/refusals.md" download>Download refusals explainer (.md)</a>
        &middot;
        <a href="{base}/refusals.html" download>Download refusals explainer (.html)</a>
      </p>
    </li>"""


_EVENT_ITEM_RENDERERS: Mapping[str, Callable[[str, CaseEvent], str]] = {
    "review_verdict": lambda _case_id, e: _render_review_verdict_item(e),
    "gate_decision": lambda _case_id, e: _render_gate_decision_item(e),
    "annotated_overlay": lambda _case_id, e: _render_annotated_overlay_item(e),
    "submission_composed": _render_submission_composed_item,
}


def _render_events_section(
    case_id: str, event_type: str, title: str, events: Sequence[CaseEvent]
) -> str:
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


def _render_grounds_section(grounds: Sequence[GroundRecord]) -> str:
    if not grounds:
        return (
            '<section class="card"><h3>Grounds</h3>'
            '<p class="empty">No grounds proposed yet.</p></section>'
        )
    rows = "".join(
        f"""<li class="ground ground--{_esc(g.status.value)}">
              <span class="ground__claim">{_esc(g.claim)}</span>
              <span class="badge">{_esc(g.status.value)}</span>
            </li>"""
        for g in grounds
    )
    return f'<section class="card"><h3>Grounds</h3><ul class="ground-list">{rows}</ul></section>'


def render_case_page(
    case: CaseRecord, grounds: Sequence[GroundRecord], events: Sequence[CaseEvent]
) -> str:
    """Render the case page: interview transcript, evidence, reviewer
    opinions, adjudication, gate decisions with refusal explanations, and
    output documents -- each section reads directly from the case's event
    log, so it renders correctly (as "nothing yet") for every stage the
    tribunal pipeline hasn't reached, and fills in automatically once a
    future wave starts emitting that event type."""
    by_type: dict[str, list[CaseEvent]] = {}
    for event in events:
        by_type.setdefault(event.event_type, []).append(event)

    sections = "".join(
        _render_events_section(case.case_id, event_type, title, by_type.get(event_type, ()))
        for event_type, title in _EVENT_SECTION_TITLES.items()
    )
    grounds_section = _render_grounds_section(grounds)
    last_sequence = max((e.sequence for e in events), default=-1)

    return f"""
<!doctype html>
<html data-theme="light">
<head>
  <meta charset="utf-8">
  <title>Setback -- {_esc(case.application_number)}</title>
  {_PAGE_STYLE}
</head>
<body data-case-id="{_esc(case.case_id)}" data-last-sequence="{last_sequence}">
  <header class="topbar">
    <h1>Setback</h1>
    <p class="tagline">Case {_esc(case.application_number)} &middot; {_esc(case.case_id)}</p>
  </header>
  <main class="container case-page">
    <section class="card chat-card">
      <h3>Collaborative Partner</h3>
      <div id="interview-transcript" class="chat-transcript"></div>
      <form id="interview-form" class="chat-form">
        <input id="interview-input" type="text" placeholder="Type your answer..."
               autocomplete="off">
        <button type="submit">Send</button>
      </form>
      <form id="upload-form" class="upload-form">
        <input id="upload-input" type="file" accept="image/*,application/pdf">
        <button type="submit">Upload photo/document</button>
      </form>
      <button id="start-tribunal" type="button">Start tribunal</button>
    </section>
    {grounds_section}
    {sections}
  </main>
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
    )


app = _build_production_app()
