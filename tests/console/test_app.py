"""Tests for setback.console.app: the resident-facing FastAPI console.

Fully offline against fakes -- no live model calls (0 budget for this work
package), no live Firestore, no live document tracker. `create_app` is the
seam: every test builds its own app with `InMemoryCaseStore`, a recording
fake `QuestionComposer`, a real (but fully in-memory) `UserUploadedDocumentSource`,
and a recording fake job trigger.
"""

from __future__ import annotations

import asyncio
import io
import json

import pytest
from fastapi.testclient import TestClient

from setback.console.app import RealJobTrigger, create_app
from setback.ingest.tracker import ExhibitedDocument, UserUploadedDocumentSource
from setback.state.firestore import InMemoryCaseStore, case_id_for


class _FakeComposer:
    """Deterministic stand-in for `ModelQuestionComposer` -- no model call."""

    def __init__(self) -> None:
        self.instructions: list[str] = []

    async def compose(self, *, instruction: str, context: object = ()) -> str:
        self.instructions.append(instruction)
        return f"COMPOSED: {instruction}"


class _RecordingJobTrigger:
    def __init__(self) -> None:
        self.triggered_case_ids: list[str] = []

    async def trigger(self, case_id: str) -> None:
        self.triggered_case_ids.append(case_id)


@pytest.fixture
def store() -> InMemoryCaseStore:
    return InMemoryCaseStore()


@pytest.fixture
def composer() -> _FakeComposer:
    return _FakeComposer()


@pytest.fixture
def job_trigger() -> _RecordingJobTrigger:
    return _RecordingJobTrigger()


@pytest.fixture
def client(
    store: InMemoryCaseStore, composer: _FakeComposer, job_trigger: _RecordingJobTrigger
) -> TestClient:
    app = create_app(
        store,
        composer=composer,
        document_source=UserUploadedDocumentSource(),
        job_trigger=job_trigger,
        max_upload_bytes=1024,
        sse_idle_timeout_seconds=0.2,
        sse_poll_interval_seconds=0.02,
    )
    return TestClient(app)


def _create_case(
    client: TestClient, *, application_number: str = "PAN-1", session: str = "s1"
) -> str:
    response = client.post(
        "/api/cases", json={"application_number": application_number, "resident_session": session}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["case_id"])


# --- case creation ------------------------------------------------------------


def test_create_case_returns_deterministic_case_id(client: TestClient) -> None:
    case_id = _create_case(client)
    assert case_id == case_id_for("PAN-1", "s1")


def test_create_case_is_idempotent(client: TestClient) -> None:
    first = _create_case(client)
    second = _create_case(client)
    assert first == second


# --- interview ------------------------------------------------------------


def test_get_interview_auto_starts(client: TestClient, composer: _FakeComposer) -> None:
    case_id = _create_case(client)
    response = client.get(f"/api/cases/{case_id}/interview")
    assert response.status_code == 200
    body = response.json()
    assert body["stage"] == "opening"
    assert len(body["turns"]) == 1
    assert composer.instructions  # the opener was actually composed


def test_get_interview_is_idempotent_across_reconnects(
    client: TestClient, composer: _FakeComposer
) -> None:
    case_id = _create_case(client)
    client.get(f"/api/cases/{case_id}/interview")
    calls_after_first = len(composer.instructions)
    response = client.get(f"/api/cases/{case_id}/interview")
    assert len(composer.instructions) == calls_after_first  # no re-compose
    assert len(response.json()["turns"]) == 1


def test_interview_unknown_case_is_404(client: TestClient) -> None:
    response = client.get("/api/cases/does-not-exist/interview")
    assert response.status_code == 404


def test_full_interview_answer_sequence_reaches_done(client: TestClient) -> None:
    case_id = _create_case(client)
    client.get(f"/api/cases/{case_id}/interview")

    r = client.post(
        f"/api/cases/{case_id}/interview",
        json={"answer": "The new second storey will overshadow my entire garden."},
    )
    assert r.json()["stage"] == "clarifying"

    r = client.post(
        f"/api/cases/{case_id}/interview", json={"answer": "It loses sun 11am-3pm in winter."}
    )
    assert r.json()["stage"] == "requesting_evidence"

    r = client.post(f"/api/cases/{case_id}/interview", json={"answer": "No photos, sorry."})
    assert r.json()["stage"] == "confirming"

    r = client.post(f"/api/cases/{case_id}/interview", json={"answer": "Yes, correct."})
    assert r.json()["stage"] == "ask_more"

    r = client.post(f"/api/cases/{case_id}/interview", json={"answer": "No, that's all."})
    assert r.json()["stage"] == "done"


def test_interview_answer_before_start_is_404(client: TestClient) -> None:
    case_id = _create_case(client)
    response = client.post(f"/api/cases/{case_id}/interview", json={"answer": "hello"})
    assert response.status_code == 404


def test_confirmed_concern_proposes_a_ground_with_its_category(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    """The moment a concern is confirmed (stage advances to `ask_more`),
    the console must propose a ground and tag it with the s4.15 category
    the tribunal job (`job.pipeline`) later reads back to run the court/gate
    pipeline -- this is the only place the interview's parsed concern is
    available to record it."""
    case_id = _create_case(client)
    client.get(f"/api/cases/{case_id}/interview")
    client.post(
        f"/api/cases/{case_id}/interview",
        json={"answer": "The new second storey will overshadow my entire garden."},
    )
    client.post(f"/api/cases/{case_id}/interview", json={"answer": "It loses sun in winter."})
    client.post(f"/api/cases/{case_id}/interview", json={"answer": "No photos, sorry."})
    response = client.post(f"/api/cases/{case_id}/interview", json={"answer": "Yes, correct."})
    assert response.json()["stage"] == "ask_more"

    grounds = asyncio.run(store.list_grounds(case_id))
    assert len(grounds) == 1
    assert "overshadow" in grounds[0].claim.lower()

    events = asyncio.run(store.list_events(case_id))
    category_events = [e for e in events if e.event_type == "ground_category_assigned"]
    assert len(category_events) == 1
    assert category_events[0].payload["category"] == "environmental_and_social_impacts"
    assert category_events[0].payload["ground_id"] == grounds[0].ground_id


def test_confirming_a_second_concern_proposes_a_second_ground(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    case_id = _create_case(client)
    client.get(f"/api/cases/{case_id}/interview")
    client.post(f"/api/cases/{case_id}/interview", json={"answer": "It will devalue my property."})
    client.post(f"/api/cases/{case_id}/interview", json={"answer": "Comparable sales say so."})
    client.post(f"/api/cases/{case_id}/interview", json={"answer": "No photos, sorry."})
    client.post(f"/api/cases/{case_id}/interview", json={"answer": "Yes, correct."})
    client.post(f"/api/cases/{case_id}/interview", json={"answer": "Yes, one more thing."})
    client.post(
        f"/api/cases/{case_id}/interview",
        json={"answer": "It will also overshadow my garden."},
    )
    client.post(f"/api/cases/{case_id}/interview", json={"answer": "Loses sun in winter."})
    client.post(f"/api/cases/{case_id}/interview", json={"answer": "No photos, sorry."})
    client.post(f"/api/cases/{case_id}/interview", json={"answer": "Yes, correct."})

    grounds = asyncio.run(store.list_grounds(case_id))
    assert len(grounds) == 2
    events = asyncio.run(store.list_events(case_id))
    categories = {
        e.payload["category"] for e in events if e.event_type == "ground_category_assigned"
    }
    assert categories == {"property_value", "environmental_and_social_impacts"}


# --- document upload --------------------------------------------------------


def test_upload_document_records_event_and_advances_interview(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    case_id = _create_case(client)
    client.get(f"/api/cases/{case_id}/interview")
    client.post(f"/api/cases/{case_id}/interview", json={"answer": "It'll overshadow my garden."})
    client.post(f"/api/cases/{case_id}/interview", json={"answer": "Loses sun in winter."})

    response = client.post(
        f"/api/cases/{case_id}/documents",
        files={"file": ("garden.jpg", io.BytesIO(b"fake-photo-bytes"), "image/jpeg")},
    )
    assert response.status_code == 200, response.text
    document_id = response.json()["document_id"]
    assert document_id

    interview_state = client.get(f"/api/cases/{case_id}/interview").json()
    assert interview_state["stage"] == "confirming"

    events = list(store._cases[case_id].events.values())  # noqa: SLF001 -- white-box event assertion
    event_types = [e.event_type for e in events]
    assert "document_uploaded" in event_types


def test_upload_document_over_size_cap_is_413(client: TestClient) -> None:
    case_id = _create_case(client)
    response = client.post(
        f"/api/cases/{case_id}/documents",
        files={"file": ("big.jpg", io.BytesIO(b"x" * 2000), "image/jpeg")},
    )
    assert response.status_code == 413


def test_upload_document_unknown_case_is_404(client: TestClient) -> None:
    response = client.post(
        "/api/cases/does-not-exist/documents",
        files={"file": ("a.jpg", io.BytesIO(b"abc"), "image/jpeg")},
    )
    assert response.status_code == 404


class _RecordingEvidenceStore:
    """A fake `EvidenceUploadStore` recording exactly what it is called
    with, to prove the upload route writes through the port keyed by
    `case_id` (not `application_number`) with the upload's content type."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes, str | None]] = []

    async def add_evidence_document(
        self, case_id: str, document_id: str, content: bytes, *, content_type: str | None = None
    ) -> None:
        self.calls.append((case_id, document_id, content, content_type))

    async def list_documents(self, da_number: str) -> list[ExhibitedDocument]:
        return []

    async def download_document(self, document: ExhibitedDocument) -> bytes:
        raise AssertionError("not exercised by this test")


def test_upload_document_writes_through_the_evidence_store_keyed_by_case_id(
    store: InMemoryCaseStore, composer: _FakeComposer
) -> None:
    evidence_store = _RecordingEvidenceStore()
    app = create_app(store, composer=composer, document_source=evidence_store)
    client = TestClient(app)
    case_id = _create_case(client, application_number="PAN-9", session="s9")

    response = client.post(
        f"/api/cases/{case_id}/documents",
        files={"file": ("garden.jpg", io.BytesIO(b"photo bytes"), "image/jpeg")},
    )
    assert response.status_code == 200, response.text
    document_id = response.json()["document_id"]

    assert evidence_store.calls == [(case_id, document_id, b"photo bytes", "image/jpeg")]


# --- tribunal trigger ---------------------------------------------------------


def test_trigger_tribunal_records_event_and_calls_job_trigger(
    client: TestClient, job_trigger: _RecordingJobTrigger
) -> None:
    case_id = _create_case(client)
    response = client.post(f"/api/cases/{case_id}/tribunal")
    assert response.status_code == 202, response.text
    assert job_trigger.triggered_case_ids == [case_id]


def test_trigger_tribunal_records_job_failed_when_the_trigger_itself_raises(
    store: InMemoryCaseStore, composer: _FakeComposer
) -> None:
    """A `JobTrigger.trigger` failure (e.g. the deployed console's real
    `RealJobTrigger` hitting a `PermissionDenied` from Cloud Run, caught
    live in smoke loop #2) must not silently leave the case stuck
    "running" forever against `enforce_concurrent_tribunal_cap` -- that
    permanently burns one of only `DEFAULT_MAX_CONCURRENT_TRIBUNALS` (2)
    slots for a run that never actually started. The route must record a
    terminal `job_failed` event and surface a clean error status, not an
    unhandled 500."""

    class _RaisingJobTrigger:
        async def trigger(self, case_id: str) -> None:
            raise RuntimeError("simulated: PermissionDenied from Cloud Run")

    app = create_app(
        store,
        composer=composer,
        document_source=UserUploadedDocumentSource(),
        job_trigger=_RaisingJobTrigger(),
    )
    client = TestClient(app, raise_server_exceptions=False)
    case_id = _create_case(client)

    response = client.post(f"/api/cases/{case_id}/tribunal")
    assert response.status_code == 502, response.text

    events = asyncio.run(store.list_events(case_id))
    event_types = [e.event_type for e in events]
    assert "tribunal_requested" in event_types
    assert "job_failed" in event_types

    # The stuck-forever regression this guards against: with a terminal
    # event now recorded, this case must no longer count as "running" --
    # a fresh tribunal request for a *different* case must not be refused
    # by the concurrency cap because of this one's leftover state.
    from setback.console.guards import count_running_tribunals

    assert asyncio.run(count_running_tribunals(store)) == 0


def test_a_second_tribunal_start_on_the_same_case_records_its_own_event(
    client: TestClient, job_trigger: _RecordingJobTrigger, store: InMemoryCaseStore
) -> None:
    """`tribunal_requested`'s event id must be unique per *attempt*, not
    just per case -- a fixed `f"tribunal-requested:{case_id}"` id caused
    `CaseStore.append_event`'s idempotency-by-id dedup to silently swallow
    every request after the first (the event, and its sequence number,
    just never advanced), which is exactly what smoke loop #2 found live:
    a second, later `POST /tribunal` on an already-completed case still
    triggered a *real* second Cloud Run Job execution (`trigger.trigger`
    is unconditional) while leaving no trace of that second attempt in the
    case's own audit log at all."""
    case_id = _create_case(client)

    first = client.post(f"/api/cases/{case_id}/tribunal")
    assert first.status_code == 202, first.text
    second = client.post(f"/api/cases/{case_id}/tribunal")
    assert second.status_code == 202, second.text

    assert job_trigger.triggered_case_ids == [case_id, case_id]
    events = asyncio.run(store.list_events(case_id))
    tribunal_requested_events = [e for e in events if e.event_type == "tribunal_requested"]
    assert len(tribunal_requested_events) == 2
    assert tribunal_requested_events[0].sequence != tribunal_requested_events[1].sequence


def test_trigger_tribunal_unknown_case_is_404(client: TestClient) -> None:
    response = client.post("/api/cases/does-not-exist/tribunal")
    assert response.status_code == 404


def test_start_tribunal_refused_once_the_concurrency_cap_is_reached(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    """`console.guards.enforce_concurrent_tribunal_cap` (default cap: 2) is
    wired ahead of `JobTrigger.trigger` -- two cases with a running (no
    terminal event yet) tribunal must block a third case's request."""
    running_case_ids = [
        _create_case(client, application_number=f"PAN-{i}", session=f"s{i}") for i in range(2)
    ]
    for running_case_id in running_case_ids:
        response = client.post(f"/api/cases/{running_case_id}/tribunal")
        assert response.status_code == 202, response.text

    third_case_id = _create_case(client, application_number="PAN-third", session="s-third")
    response = client.post(f"/api/cases/{third_case_id}/tribunal")

    assert response.status_code == 429, response.text


# --- abuse guards (rate limiting) --------------------------------------------


def test_case_creation_is_rate_limited_per_ip(client: TestClient) -> None:
    """`console.guards.per_ip_case_creation_guard` (default: 5/hour) is
    wired on `POST /api/cases` -- the 6th request from the same client must
    be refused."""
    for i in range(5):
        response = client.post(
            "/api/cases", json={"application_number": f"PAN-limit-{i}", "resident_session": "s"}
        )
        assert response.status_code == 201, response.text

    response = client.post(
        "/api/cases", json={"application_number": "PAN-limit-6", "resident_session": "s"}
    )
    assert response.status_code == 429
    assert "Retry-After" in response.headers


# --- SSE event stream ----------------------------------------------------------


def test_events_stream_returns_seeded_events(client: TestClient) -> None:
    case_id = _create_case(client)
    client.post(f"/api/cases/{case_id}/tribunal")

    with client.stream("GET", f"/api/cases/{case_id}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = [line for line in response.iter_lines() if line.startswith("data:")]

    assert lines  # the short idle_timeout fixture config means the stream terminates
    payload = json.loads(lines[0].removeprefix("data:").strip())
    assert "event_type" in payload


def test_events_stream_unknown_case_is_404(client: TestClient) -> None:
    response = client.get("/api/cases/does-not-exist/events")
    assert response.status_code == 404


def test_events_stream_after_param_skips_already_rendered_events(client: TestClient) -> None:
    """A case page reload opens a brand-new SSE connection with no memory of
    what it already showed -- without an `after` cursor, the server would
    replay every historical event and the client would treat each one as
    "new", reloading the page again, forever. `after` must let a fresh
    connection skip everything at or below the sequence the page already
    rendered."""
    case_id = _create_case(client)
    client.post(f"/api/cases/{case_id}/tribunal")

    with client.stream("GET", f"/api/cases/{case_id}/events") as response:
        lines = [line for line in response.iter_lines() if line.startswith("data:")]
    last_sequence = max(
        json.loads(line.removeprefix("data:").strip())["sequence"] for line in lines
    )

    with client.stream("GET", f"/api/cases/{case_id}/events?after={last_sequence}") as response:
        assert response.status_code == 200
        replayed = [line for line in response.iter_lines() if line.startswith("data:")]
    assert replayed == []


# --- refusal feedback ----------------------------------------------------------


def test_refusal_feedback_persists_and_returns_re_rendered_explanation(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    case_id = _create_case(client)
    response = client.post(
        f"/api/cases/{case_id}/grounds/ground-1/feedback",
        json={
            "original_explanation": "Property value is not a s4.15(1) matter.",
            "pushback": "But it's worth so much less now!",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["re_rendered_explanation"].startswith("COMPOSED:")

    events = list(store._cases[case_id].events.values())  # noqa: SLF001
    assert any(e.event_type == "resident_refusal_feedback" for e in events)


def test_refusal_feedback_unknown_case_is_404(client: TestClient) -> None:
    response = client.post(
        "/api/cases/does-not-exist/grounds/g1/feedback",
        json={"original_explanation": "x", "pushback": "y"},
    )
    assert response.status_code == 404


# --- HTML pages ----------------------------------------------------------------


def test_docket_board_lists_created_cases(client: TestClient) -> None:
    case_id = _create_case(client, application_number="PAN-1", session="s1")
    _create_case(client, application_number="PAN-2", session="s2")

    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert case_id in response.text
    assert "PAN-2" in response.text


def test_docket_board_loads_the_client_script_so_the_create_case_form_renders(
    client: TestClient,
) -> None:
    """`app.js` builds the docket board's only case-creation UI at runtime
    (`initCreateCaseForm`, per its own module docstring -- the
    server-rendered board has no case-creation markup of its own). Without
    a `<script src="/static/app.js">` tag on this page, a resident has no
    way to start a case through the UI at all -- caught live against the
    deployed console (smoke loop #2)."""
    response = client.get("/")
    assert response.status_code == 200
    assert '<script src="/static/app.js"></script>' in response.text


def test_docket_board_survives_a_fresh_app_instance_over_the_same_store(
    store: InMemoryCaseStore, composer: _FakeComposer
) -> None:
    """The board renders `CaseStore.list_cases`, not an in-process registry
    -- a brand-new app built over the same store (modelling a console
    restart/redeploy, or a second replica) must still show every case."""
    first_app = create_app(store, composer=composer, document_source=UserUploadedDocumentSource())
    _create_case(TestClient(first_app), application_number="PAN-1", session="s1")

    second_app = create_app(store, composer=composer, document_source=UserUploadedDocumentSource())
    response = TestClient(second_app).get("/")

    assert response.status_code == 200
    assert "PAN-1" in response.text


def test_case_page_renders_known_sections(client: TestClient) -> None:
    case_id = _create_case(client)
    response = client.get(f"/cases/{case_id}")
    assert response.status_code == 200
    body_lower = response.text.lower()
    for section in ("interview", "evidence", "reviewer", "adjudication", "gate", "submission"):
        assert section in body_lower


def test_case_page_unknown_case_is_404(client: TestClient) -> None:
    response = client.get("/cases/does-not-exist")
    assert response.status_code == 404


def test_annotated_overlay_event_renders_as_an_image(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    case_id = _create_case(client)
    asyncio.run(
        store.append_event(
            case_id,
            "annotated-overlay:doc-1",
            "annotated_overlay",
            payload={"document_id": "doc-1", "mime_type": "image/png", "image_base64": "QUJD"},
        )
    )
    response = client.get(f"/cases/{case_id}")
    assert response.status_code == 200
    assert '<img src="data:image/png;base64,QUJD"' in response.text


def test_submission_composed_event_renders_download_links(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    case_id = _create_case(client)
    asyncio.run(
        store.append_event(
            case_id,
            "submission-composed:x",
            "submission_composed",
            payload={
                "submission_markdown": "# Objection\n\nGround text.",
                "submission_html": "<article><h1>Objection</h1></article>",
                "refusals_markdown": "# Refusals\n\nExplanation.",
                "refusals_html": "<article><h1>Refusals</h1></article>",
            },
        )
    )
    response = client.get(f"/cases/{case_id}")
    assert response.status_code == 200
    assert f"/api/cases/{case_id}/submission.md" in response.text
    assert f"/api/cases/{case_id}/submission.html" in response.text
    assert f"/api/cases/{case_id}/refusals.md" in response.text
    assert "<h1>Objection</h1>" in response.text

    md_response = client.get(f"/api/cases/{case_id}/submission.md")
    assert md_response.status_code == 200
    assert md_response.text == "# Objection\n\nGround text."

    html_response = client.get(f"/api/cases/{case_id}/submission.html")
    assert "<h1>Objection</h1>" in html_response.text


def test_download_submission_before_composed_is_404(client: TestClient) -> None:
    case_id = _create_case(client)
    response = client.get(f"/api/cases/{case_id}/submission.md")
    assert response.status_code == 404


# --- static assets ---------------------------------------------------------


def test_static_assets_are_served(client: TestClient) -> None:
    js = client.get("/static/app.js")
    css = client.get("/static/style.css")
    assert js.status_code == 200
    assert css.status_code == 200


# --- RealJobTrigger ----------------------------------------------------------


class _FakeRunJobsClient:
    """Fake `google.cloud.run_v2.JobsClient`: `RealJobTrigger` never
    actually imports the real `google-cloud-run` package when a client is
    injected -- these tests exercise the real request-building logic
    fully offline, with zero dependency on that (not yet installed)
    package."""

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def run_job(self, request: dict[str, object]) -> None:
        self.requests.append(request)


def test_real_job_trigger_builds_the_job_path_from_project_and_region() -> None:
    trigger = RealJobTrigger(project="vexcourt-agent", region="australia-southeast1")
    assert trigger._job_path() == (  # noqa: SLF001 -- white-box assertion
        "projects/vexcourt-agent/locations/australia-southeast1/jobs/setback-tribunal"
    )


async def test_real_job_trigger_calls_run_job_with_a_case_id_override() -> None:
    client = _FakeRunJobsClient()
    trigger = RealJobTrigger(project="vexcourt-agent", region="australia-southeast1", client=client)

    await trigger.trigger("case-123")

    assert len(client.requests) == 1
    request = client.requests[0]
    assert request["name"] == (
        "projects/vexcourt-agent/locations/australia-southeast1/jobs/setback-tribunal"
    )
    overrides = request["overrides"]
    assert isinstance(overrides, dict)
    container_overrides = overrides["container_overrides"]
    assert container_overrides == [{"env": [{"name": "CASE_ID", "value": "case-123"}]}]


async def test_real_job_trigger_defaults_project_and_region_from_config() -> None:
    from setback import config

    client = _FakeRunJobsClient()
    trigger = RealJobTrigger(client=client)

    await trigger.trigger("case-1")

    assert client.requests[0]["name"] == (
        f"projects/{config.GCP_PROJECT}/locations/{config.REGION}/jobs/setback-tribunal"
    )
