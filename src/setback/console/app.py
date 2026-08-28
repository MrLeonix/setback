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
``GET  /``                                             docket board (known cases)
``GET  /cases/{case_id}``                              the case page
``GET  /static/*``                                     app.js / style.css

Known MVP limitation: `CaseStore` (wave 2's port) has no "list all cases"
method, so the docket board can only show cases this console *process*
instance has created (an in-memory registry) -- acceptable for a
single-Cloud-Run-Service, single-demo-case hackathon build; a future wave
adding a `list_cases` method to the port would let the board survive a
restart.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from setback.ingest.tracker import UserUploadedDocumentSource
from setback.interview.flow import (
    InterviewFlow,
    InterviewTurn,
    ModelQuestionComposer,
    QuestionComposer,
    capture_refusal_feedback,
)
from setback.state.firestore import (
    CaseEvent,
    CaseNotFoundError,
    CaseRecord,
    CaseStore,
    GroundRecord,
)

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_DEFAULT_MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB


class JobTrigger(Protocol):
    """Starts the `setback-tribunal` Cloud Run Job for a case."""

    async def trigger(self, case_id: str) -> None: ...


class LoggingJobTrigger:
    """The default `JobTrigger`: records that a trigger was requested
    in-process without invoking a real Cloud Run Jobs execution.

    No `google-cloud-run` client is a declared dependency of this package
    yet -- wiring a real execution trigger is future deploy-stage work
    (STATUS.md already tracks `make deploy` as a stub). The console route
    that calls this always records the request as a durable case event
    regardless of what `JobTrigger` is wired in, so a request is never
    silently lost even before a real trigger exists.
    """

    def __init__(self) -> None:
        self.triggered_case_ids: list[str] = []

    async def trigger(self, case_id: str) -> None:
        self.triggered_case_ids.append(case_id)


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


async def _sse_event_stream(
    store: CaseStore,
    case_id: str,
    *,
    poll_interval: float,
    idle_timeout: float | None,
) -> AsyncIterator[str]:
    """Yield newly appended case events, in sequence order, as SSE `data:`
    lines, polling `store` for new ones.

    With `idle_timeout=None` (production default) this polls forever --
    exactly what keeps a resident's open SSE connection alive for the
    duration of a Cloud Run Service request. Tests pass a small
    `idle_timeout` so the stream terminates deterministically once it has
    caught up and gone quiet for that long, rather than hanging.
    """
    seen: set[str] = set()
    idle_elapsed = 0.0
    while True:
        events = await store.list_events(case_id)
        new_events = sorted((e for e in events if e.event_id not in seen), key=lambda e: e.sequence)
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
    document_source: UserUploadedDocumentSource | None = None,
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
        document_source: Where uploaded photos/documents are kept. Defaults
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
    known_case_ids: list[str] = []

    async def _require_case(case_id: str) -> CaseRecord:
        case = await store.get_case(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail=f"case {case_id!r} not found")
        return case

    @app.post("/api/cases", status_code=201)
    async def create_case(body: CreateCaseRequest) -> dict[str, Any]:
        case = await store.create_case(
            application_number=body.application_number, resident_session=body.resident_session
        )
        if case.case_id not in known_case_ids:
            known_case_ids.append(case.case_id)
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

    @app.post("/api/cases/{case_id}/interview")
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
        return _turn_to_json(turn, flow.transcript)

    @app.post("/api/cases/{case_id}/documents")
    async def upload_document(
        case_id: str,
        file: UploadFile = File(...),  # noqa: B008 -- required FastAPI idiom
    ) -> JSONResponse:
        case = await _require_case(case_id)
        content = await file.read(max_upload_bytes + 1)
        if len(content) > max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"document exceeds the {max_upload_bytes}-byte upload limit",
            )
        document_id = hashlib.sha256(content).hexdigest()[:16]
        documents.add_document(case.application_number, document_id, content)
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
        await store.append_event(
            case_id, f"tribunal-requested:{case_id}", "tribunal_requested", payload={}
        )
        await trigger.trigger(case_id)
        return {"case_id": case_id, "status": "tribunal_requested"}

    @app.get("/api/cases/{case_id}/events")
    async def stream_events(case_id: str) -> StreamingResponse:
        await _require_case(case_id)
        return StreamingResponse(
            _sse_event_stream(
                store,
                case_id,
                poll_interval=sse_poll_interval_seconds,
                idle_timeout=sse_idle_timeout_seconds,
            ),
            media_type="text/event-stream",
        )

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
        for case_id in known_case_ids:
            case = await store.get_case(case_id)
            if case is not None:
                cases.append((case, await store.list_grounds(case_id)))
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
</body>
</html>
"""


_EVENT_SECTION_TITLES: Mapping[str, str] = {
    "interview_turn": "Interview transcript",
    "document_uploaded": "Evidence",
    "review_verdict": "Reviewer opinions",
    "adjudication_decision": "Adjudication",
    "gate_decision": "Gate decisions",
    "resident_refusal_feedback": "Resident feedback on refusals",
    "submission_composed": "Submission documents",
    "tribunal_requested": "Tribunal",
}


def _render_events_section(title: str, events: Sequence[CaseEvent]) -> str:
    if not events:
        return (
            f'<section class="card"><h3>{_esc(title)}</h3>'
            '<p class="empty">Nothing yet.</p></section>'
        )
    items = "".join(
        f'<li><span class="event-seq">#{e.sequence}</span> {_esc(json.dumps(dict(e.payload)))}</li>'
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
        _render_events_section(title, by_type.get(event_type, ()))
        for event_type, title in _EVENT_SECTION_TITLES.items()
    )
    grounds_section = _render_grounds_section(grounds)

    return f"""
<!doctype html>
<html data-theme="light">
<head>
  <meta charset="utf-8">
  <title>Setback -- {_esc(case.application_number)}</title>
  {_PAGE_STYLE}
</head>
<body data-case-id="{_esc(case.case_id)}">
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

    Constructing `FirestoreCaseStore()`/`ModelClient()` builds client
    objects only -- neither makes a network call in its constructor -- so
    this runs safely at import time (needed for `uvicorn
    setback.console.app:app`) without ever touching the network during
    test collection.
    """
    from setback.models.client import ModelClient
    from setback.state.firestore import FirestoreCaseStore

    return create_app(FirestoreCaseStore(), composer=ModelQuestionComposer(ModelClient()))


app = _build_production_app()
