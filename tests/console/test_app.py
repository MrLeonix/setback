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
import os
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from setback.console.app import (
    RealJobTrigger,
    _is_hygiene_excluded,
    create_app,
    render_landing_page,
)
from setback.ingest.tracker import ExhibitedDocument, UserUploadedDocumentSource
from setback.interview.flow import NormalisedConcern
from setback.state.firestore import CaseRecord, GroundStatus, InMemoryCaseStore, case_id_for

# Real JPEG magic bytes (SOI + APP0 marker) prefixed onto otherwise-fake
# photo content -- since the upload route now sniffs actual file bytes
# rather than trusting the client-supplied Content-Type header (P0
# security fix), fixture "photos" must carry a real signature to be
# accepted as `image/jpeg`.
_FAKE_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"fake-photo-bytes"


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


_REAL_SESSION = "11111111-1111-4111-8111-111111111111"
"""A stand-in for `app.js`'s `window.crypto.randomUUID()` -- every genuine
resident session is shaped like this (see `getResidentSessionId` in
`console/static/app.js`), which is exactly what `console/app.py`'s docket-
board hygiene filter (`_looks_like_a_resident_session`) uses to tell a real
case apart from a manually-created smoke/test/deploy-verification one
(`"s1"`, `"SMOKE-RATE-LIMIT-TEST-1"`, `"deploy-wiring-proof"`, ...), none of
which are ever produced by the real create-case flow. Tests that exercise
the docket board's *listing* behaviour use this constant so they are not
accidentally testing the hygiene filter too; tests that don't care about
docket-board visibility keep using short human-readable session labels."""


def _create_case(
    client: TestClient, *, application_number: str = "PAN-1", session: str = _REAL_SESSION
) -> str:
    response = client.post(
        "/api/cases", json={"application_number": application_number, "resident_session": session}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["case_id"])


# --- case creation ------------------------------------------------------------


def test_create_case_returns_deterministic_case_id(client: TestClient) -> None:
    case_id = _create_case(client)
    assert case_id == case_id_for("PAN-1", _REAL_SESSION)


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


# --- get_interview persisted-transcript resume (LEO-FEEDBACK-UIUX.md §2) ---
# A cold Cloud Run instance has no in-memory `InterviewFlow` for an
# already-started case -- `get_interview` used to call `flow.start()`
# unconditionally in that situation, appending a second, differently-worded
# "opening" turn on top of what was already durably persisted. These tests
# model that exact scenario: a *second*, fresh `create_app` over the *same*
# store (mirroring `test_docket_board_survives_a_fresh_app_instance_over_
# the_same_store`), simulating the instance loss directly rather than
# reaching into the first app's private `interview_flows` dict.


def test_get_interview_after_a_fresh_instance_does_not_re_greet(
    store: InMemoryCaseStore, composer: _FakeComposer, job_trigger: _RecordingJobTrigger
) -> None:
    first_app = create_app(
        store,
        composer=composer,
        document_source=UserUploadedDocumentSource(),
        job_trigger=job_trigger,
    )
    first_client = TestClient(first_app)
    case_id = _create_case(first_client)
    first_client.get(f"/api/cases/{case_id}/interview")
    first_client.post(
        f"/api/cases/{case_id}/interview", json={"answer": "It overshadows my garden."}
    )
    persisted_turn_count = len(
        [e for e in asyncio.run(store.list_events(case_id)) if e.event_type == "interview_turn"]
    )

    second_app = create_app(
        store,
        composer=composer,
        document_source=UserUploadedDocumentSource(),
        job_trigger=job_trigger,
    )
    second_client = TestClient(second_app)
    response = second_client.get(f"/api/cases/{case_id}/interview")
    assert response.status_code == 200
    body = response.json()

    # No duplicate opening turn was appended -- the persisted event count
    # (system + resident turns) is unchanged, and the reconstructed
    # transcript matches it exactly.
    turn_events_after = [
        e for e in asyncio.run(store.list_events(case_id)) if e.event_type == "interview_turn"
    ]
    assert len(turn_events_after) == persisted_turn_count
    assert len(body["turns"]) == persisted_turn_count
    assert body["stage"] == "clarifying"
    # The full persisted transcript renders, not just the latest turn.
    assert any("overshadows my garden" in t["prompt"] for t in body["turns"])


def test_get_interview_after_a_fresh_instance_preserves_each_turns_role(
    store: InMemoryCaseStore, composer: _FakeComposer, job_trigger: _RecordingJobTrigger
) -> None:
    """P0 regression (wave-12 synthesis #2): a replayed resident turn must
    still render as the resident's own bubble after a cold start, not get
    relabelled as Setback's. `_turn_to_json` must carry a `role` per turn
    reflecting what was actually persisted (`_persist_system_turn`/
    `_persist_resident_answer`), surviving the same rehydration path as
    `test_get_interview_after_a_fresh_instance_does_not_re_greet`."""
    first_app = create_app(
        store,
        composer=composer,
        document_source=UserUploadedDocumentSource(),
        job_trigger=job_trigger,
    )
    first_client = TestClient(first_app)
    case_id = _create_case(first_client)
    first_client.get(f"/api/cases/{case_id}/interview")
    first_client.post(
        f"/api/cases/{case_id}/interview", json={"answer": "It overshadows my garden."}
    )

    second_app = create_app(
        store,
        composer=composer,
        document_source=UserUploadedDocumentSource(),
        job_trigger=job_trigger,
    )
    second_client = TestClient(second_app)
    response = second_client.get(f"/api/cases/{case_id}/interview")
    body = response.json()

    resident_turns = [t for t in body["turns"] if t["prompt"] == "It overshadows my garden."]
    assert resident_turns, body["turns"]
    assert resident_turns[0]["role"] == "resident"
    # And the greeting that preceded it is still labelled the other way.
    system_turns = [t for t in body["turns"] if t["role"] == "system"]
    assert system_turns


def test_get_interview_after_a_fresh_instance_can_still_advance(
    store: InMemoryCaseStore, composer: _FakeComposer, job_trigger: _RecordingJobTrigger
) -> None:
    """Proves the rehydrated flow isn't just render-only -- a resident can
    keep answering after a cold start mid-concern, with the state machine
    landing on the correct next stage exactly as an uninterrupted session
    would."""
    first_app = create_app(
        store,
        composer=composer,
        document_source=UserUploadedDocumentSource(),
        job_trigger=job_trigger,
    )
    first_client = TestClient(first_app)
    case_id = _create_case(first_client)
    first_client.get(f"/api/cases/{case_id}/interview")
    first_client.post(
        f"/api/cases/{case_id}/interview", json={"answer": "It overshadows my garden."}
    )

    second_app = create_app(
        store,
        composer=composer,
        document_source=UserUploadedDocumentSource(),
        job_trigger=job_trigger,
    )
    second_client = TestClient(second_app)
    second_client.get(f"/api/cases/{case_id}/interview")
    response = second_client.post(
        f"/api/cases/{case_id}/interview", json={"answer": "Loses sun in winter afternoons."}
    )
    assert response.status_code == 200
    assert response.json()["stage"] == "requesting_evidence"


def test_get_interview_still_greets_fresh_when_no_transcript_exists_yet(
    store: InMemoryCaseStore, composer: _FakeComposer
) -> None:
    """Baseline preserved: a genuinely brand-new case still greets exactly
    once, whether or not this is the first app instance to see it."""
    app_a = create_app(store, composer=composer, document_source=UserUploadedDocumentSource())
    case_id = _create_case(TestClient(app_a))

    app_b = create_app(store, composer=composer, document_source=UserUploadedDocumentSource())
    response = TestClient(app_b).get(f"/api/cases/{case_id}/interview")
    body = response.json()
    assert body["stage"] == "opening"
    assert len(body["turns"]) == 1


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
        files={"file": ("garden.jpg", io.BytesIO(_FAKE_JPEG_BYTES), "image/jpeg")},
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


def test_upload_document_rejects_content_with_no_recognized_image_or_pdf_signature(
    client: TestClient,
) -> None:
    """P0 security fix: the upload route must reject a file whose bytes
    don't match any accepted image/PDF signature, regardless of what
    Content-Type the client claims -- this is what closes the stored-XSS
    path (an attacker upload declaring `text/html`/`image/jpeg` while
    the bytes are actually an HTML/script payload)."""
    case_id = _create_case(client)
    response = client.post(
        f"/api/cases/{case_id}/documents",
        files={
            "file": (
                "evil.jpg",
                io.BytesIO(b"<script>alert(document.referrer)</script>"),
                "image/jpeg",
            )
        },
    )
    assert response.status_code == 415, response.text


def test_upload_document_ignores_a_spoofed_content_type_header(client: TestClient) -> None:
    """P0 security fix: the stored/served content type comes from sniffing
    the file's own magic bytes, never from the client-supplied header --
    a resident's browser sends a real header, but nothing stops an
    attacker's client from lying. Real PNG bytes declared as `text/html`
    must still be stored (and later served) as `image/png`, not the
    attacker-chosen type."""
    case_id = _create_case(client)
    real_png_bytes = b"\x89PNG\r\n\x1a\n" + b"not-really-a-full-png-but-has-the-signature"
    upload = client.post(
        f"/api/cases/{case_id}/documents",
        files={"file": ("evil.png", io.BytesIO(real_png_bytes), "text/html")},
    )
    assert upload.status_code == 200, upload.text
    document_id = upload.json()["document_id"]

    response = client.get(f"/api/cases/{case_id}/documents/{document_id}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_get_uploaded_document_sets_nosniff_header(client: TestClient) -> None:
    """Belt-and-braces alongside server-side content-type sniffing: even
    if a stored `content_type` were ever wrong, the browser must not be
    allowed to MIME-sniff its way into rendering the response as HTML."""
    case_id = _create_case(client)
    upload = client.post(
        f"/api/cases/{case_id}/documents",
        files={"file": ("garden.jpg", io.BytesIO(_FAKE_JPEG_BYTES), "image/jpeg")},
    )
    document_id = upload.json()["document_id"]

    response = client.get(f"/api/cases/{case_id}/documents/{document_id}")
    assert response.headers["x-content-type-options"] == "nosniff"


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
        files={"file": ("garden.jpg", io.BytesIO(_FAKE_JPEG_BYTES), "image/jpeg")},
    )
    assert response.status_code == 200, response.text
    document_id = response.json()["document_id"]

    assert evidence_store.calls == [(case_id, document_id, _FAKE_JPEG_BYTES, "image/jpeg")]


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


_REAL_SESSION_2 = "22222222-2222-4222-8222-222222222222"


# --- landing page (LEO-FEEDBACK-UIUX.md §1): PUBLIC, no key, docket moved to /docket ---


def test_root_is_public_and_never_401s_even_with_a_docket_key_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETBACK_DOCKET_KEY", "let-me-in")
    response = client.get("/")
    assert response.status_code == 200


def test_theme_toggle_present_on_landing_docket_and_case_pages(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LEO-FEEDBACK-UIUX.md §8: the same toggle markup everywhere, so
    `app.js`'s one `#theme-toggle` handler wires up on every page."""
    monkeypatch.delenv("SETBACK_DOCKET_KEY", raising=False)
    case_id = _create_case(client)
    for path in ("/", "/docket", f"/cases/{case_id}"):
        response = client.get(path)
        assert 'id="theme-toggle"' in response.text, path


def test_root_renders_the_minimal_landing_page(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "Setback" in body
    assert "A Collaborative Partner for planning objections" in body
    assert "<form" in body
    assert 'name="application_number"' in body
    assert 'class="disclaimer-footer"' in body


def test_root_carries_no_docket_content(client: TestClient) -> None:
    """The landing page must never leak another resident's case data --
    it is public, unauthenticated, and has no docket list on it at all."""
    _create_case(client, application_number="PAN-SHOULD-NOT-APPEAR")
    response = client.get("/")
    assert "PAN-SHOULD-NOT-APPEAR" not in response.text
    assert "docket-list" not in response.text


def test_root_ignores_theme_and_key_query_params_gracefully(client: TestClient) -> None:
    # The landing page never gates on `?key=`; an unrecognised `?theme=`
    # degrades exactly like every other page.
    response = client.get("/?key=anything&theme=dark")
    assert response.status_code == 200


def test_docket_board_moved_to_slash_docket_still_gated_the_same_way(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETBACK_DOCKET_KEY", "let-me-in")
    assert client.get("/docket").status_code == 401
    assert client.get("/docket?key=wrong").status_code == 401
    assert client.get("/docket?key=let-me-in").status_code == 200


def test_docket_board_lists_created_cases(client: TestClient) -> None:
    case_id = _create_case(client, application_number="PAN-1", session=_REAL_SESSION)
    _create_case(client, application_number="PAN-2", session=_REAL_SESSION_2)

    response = client.get("/docket")
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
    response = client.get("/docket")
    assert response.status_code == 200
    assert '<script src="/static/app.js"></script>' in response.text


_VIEWPORT_META = '<meta name="viewport" content="width=device-width, initial-scale=1">'


def test_docket_board_has_a_viewport_meta_tag(client: TestClient) -> None:
    """Round-2 UI feedback, item 2: without this tag, a mobile browser lays
    the page out at a virtual ~980px width and scales it down -- text
    reads tiny, requires pinch-zoom, and every `max-width` media query in
    style.css never actually triggers on a real phone. Found live during
    this round's own browser QA pass (`window.innerWidth` reported 980 at
    an emulated 390px viewport before this fix): this is the root cause
    behind "layout on mobile is terrible," not merely a missing nicety."""
    response = client.get("/docket")
    assert _VIEWPORT_META in response.text


def test_landing_page_has_a_viewport_meta_tag() -> None:
    assert _VIEWPORT_META in render_landing_page()


def test_case_page_has_a_viewport_meta_tag(client: TestClient) -> None:
    case_id = _create_case(client)
    response = client.get(f"/cases/{case_id}")
    assert _VIEWPORT_META in response.text


def test_docket_board_does_not_hardcode_a_light_theme(client: TestClient) -> None:
    """`style.css` implements the artifact-style theme contract: an unset
    `data-theme` on `<html>` follows the system's `prefers-color-scheme`
    (`:root:not([data-theme="light"])` for the dark override), and an
    explicit `data-theme="dark"`/`"light"` overrides it either way -- but
    this app has no theme toggle (confirmed: `app.js` has zero `theme`/
    `dark` references), so hardcoding `data-theme="light"` on every page
    permanently defeats system dark mode for every viewer. Confirmed live
    on the deployed console with `prefers-color-scheme: dark` emulated:
    the page stayed light (`background-color: rgb(247, 245, 242)`)."""
    response = client.get("/docket")
    assert 'data-theme="light"' not in response.text


def test_docket_board_honours_theme_light_query_param(client: TestClient) -> None:
    """`?theme=light` is an opt-in, filming-only override (never the
    default -- see the test above) so a recording made on a machine whose
    OS theme happens to be dark still matches every light-mode gallery
    screenshot already captured. Stamps `data-theme="light"` on `<html>`
    exactly as an explicit user toggle would, per `style.css`'s own
    `:root[data-theme="light"]` contract -- no new CSS needed."""
    response = client.get("/docket?theme=light")
    assert response.status_code == 200
    assert '<html data-theme="light">' in response.text


def test_docket_board_ignores_an_unrecognised_theme_value(client: TestClient) -> None:
    """Only the exact, documented value forces anything -- garbage input
    degrades to the same system-default behaviour as no param at all,
    never a crash or an unrecognised `data-theme` value reaching the DOM."""
    response = client.get("/docket?theme=nonsense")
    assert response.status_code == 200
    assert "data-theme" not in response.text


# --- docket hygiene: excluding smoke/test/deploy-verification cases --------


@pytest.mark.parametrize(
    "junk_session",
    [
        "SMOKE-RATE-LIMIT-TEST-1",
        "deploy-wiring-proof",
        "deploy-verify-au-001",
        "smoke-session-final-run",
        "rate-limit-burst",
        "s1",
    ],
)
def test_docket_board_excludes_cases_created_with_a_non_uuid_resident_session(
    client: TestClient, junk_session: str
) -> None:
    """Every genuine resident case is created through the browser flow,
    whose `resident_session` is always `window.crypto.randomUUID()`
    (`getResidentSessionId`, `console/static/app.js`) -- never a short,
    human-typed label. Every one of these labels is a real example from
    this project's own smoke-testing/deploy-verification history
    (STATUS.md, SMOKE.md) that a judge visiting the hosted docket board
    would otherwise see ahead of the one real demo case. The rule is
    purely structural (is `resident_session` UUID-shaped?), so it needs no
    hardcoded blocklist and catches any future test label the same way."""
    case_id = _create_case(client, application_number="PAN-JUNK", session=junk_session)

    response = client.get("/docket")
    assert response.status_code == 200
    assert "PAN-JUNK" not in response.text

    # The exclusion is docket-*list* hygiene only -- a direct link to the
    # case (its own unguessable case-id URL) must still work, exactly as
    # it would for a real resident's case, per this fix's own brief.
    case_response = client.get(f"/cases/{case_id}")
    assert case_response.status_code == 200
    assert "PAN-JUNK" in case_response.text


def test_docket_board_includes_a_case_created_with_a_real_uuid_session(
    client: TestClient,
) -> None:
    """The filter must not be so broad it hides real residents -- a normal
    `_create_case` (UUID session, the default) still appears."""
    _create_case(client, application_number="PAN-REAL")
    response = client.get("/docket")
    assert "PAN-REAL" in response.text


@pytest.mark.parametrize(
    "junk_application_number",
    [
        "SMOKE-TEST-PHOTO",
        "smoke-test-photo",  # lowercase: the match is case-insensitive
        "wave6-wiring-proof",
        "rate-limit-check",
        "Test Run 3",
    ],
)
def test_docket_board_excludes_case_with_a_junk_application_number_despite_a_real_uuid_session(
    client: TestClient, junk_application_number: str
) -> None:
    """`_looks_like_a_resident_session`'s structural check alone lets a
    scripted/curl-created case through the moment it happens to be given a
    real UUID `resident_session` -- exactly what let a `SMOKE-TEST-PHOTO`
    case slip onto the live docket board despite the wave-6 filter (SMOKE.md
    v4's docket-hygiene finding). This case's `application_number` itself
    reads as test/smoke/deploy-verification debris (case-insensitive
    contains: smoke, test, wiring-proof, rate-limit), so the docket-list
    hygiene filter must exclude it on content, not just session-id shape."""
    case_id = _create_case(
        client, application_number=junk_application_number, session=_REAL_SESSION
    )

    response = client.get("/docket")
    assert response.status_code == 200
    assert junk_application_number not in response.text

    # Hide only -- never delete: the case's own page is still reachable.
    case_response = client.get(f"/cases/{case_id}")
    assert case_response.status_code == 200
    assert junk_application_number in case_response.text


def test_docket_board_does_not_exclude_a_genuine_application_number(
    client: TestClient,
) -> None:
    """The content check must not be so eager it flags a real DA number --
    none of the junk keywords appear in an ordinary application number."""
    _create_case(client, application_number="DA2026/0359", session=_REAL_SESSION)
    response = client.get("/docket")
    assert "DA2026/0359" in response.text


# --- docket hygiene: wave 9 QA/deploy-verification leaks (wave 9.5) --------


@pytest.mark.parametrize(
    "junk_application_number",
    [
        "DA2026/DEPLOY-QA",
        "da2026/deploy-qa",  # lowercase: the match is case-insensitive
        "DA2026/SV-TEST",
        "DA2026/QA-CHECK",
        "deploy-verify-au-002",
    ],
)
def test_docket_board_excludes_wave9_qa_and_deploy_verification_cases(
    client: TestClient, junk_application_number: str
) -> None:
    """Wave-9 populate/redeploy-verification passes created real (real
    UUID `resident_session`) cases with application numbers like
    `DA2026/DEPLOY-QA` and `DA2026/SV-TEST` (SMOKE.md v8) that leaked onto
    the public docket board next to genuine resident objections -- neither
    `smoke`/`test`/`wiring-proof`/`rate-limit` alone caught `qa` or
    `deploy` on their own (only `SV-TEST` happened to also contain
    `test`)."""
    case_id = _create_case(
        client, application_number=junk_application_number, session=_REAL_SESSION
    )

    response = client.get("/docket")
    assert response.status_code == 200
    assert junk_application_number not in response.text

    # Hide only -- never delete: the case's own page is still reachable.
    case_response = client.get(f"/cases/{case_id}")
    assert case_response.status_code == 200
    assert junk_application_number in case_response.text


@pytest.mark.parametrize(
    "presentable_application_number",
    [
        "DA2026/0412-FILM2",
        "DA2026/0359",
    ],
)
def test_docket_board_keeps_film_cases_visible_after_the_qa_deploy_extension(
    client: TestClient, presentable_application_number: str
) -> None:
    """The extended exclusion list must not catch the flagship film cases
    (`DA2026/0412-FILM2`, and the frozen demo case's own `DA2026/0359`,
    the same number Case A's real-DA narrative resolves to) -- none of
    `qa`/`deploy`/`sv-test` appear in either."""
    _create_case(client, application_number=presentable_application_number, session=_REAL_SESSION)
    response = client.get("/docket")
    assert presentable_application_number in response.text


def test_docket_board_keeps_case_a_visible_after_the_qa_deploy_extension(
    client: TestClient,
) -> None:
    """Case A (`9f9a6a087f851db107be765391ba48ad`, the populate pass's real,
    live-fetched-DA film beat) must stay visible -- its case id is a plain
    hex hash, which cannot contain `q` (not a hex digit) and so can never
    match `qa`, and neither its `resident_session` nor its
    `application_number` reference this fix's new patterns."""
    case_id = _create_case(client, application_number="PAN-661190", session=_REAL_SESSION)
    response = client.get("/docket")
    assert response.status_code == 200
    assert case_id in response.text


def test_hygiene_excluded_hides_the_specific_deprecated_film_predecessor_case() -> None:
    """P0 regression (wave-12 synthesis #4): `DA2026/0412-FILM`
    (`f3f8c3475e2646537212677fbf7c8075`) is a deprecated predecessor of the
    canonical film case (`DA2026/0412-FILM2`) that was still showing up on
    the public docket board next to it. It's excluded by its specific
    `case_id` (`_DEPRECATED_CASE_IDS`), not by extending the junk-pattern
    heuristic with a `film` substring -- which would also catch the
    canonical FILM2 case, still required to stay visible. A real UUID
    `resident_session` and an otherwise unremarkable `application_number`
    prove the case-id denylist, specifically, is what excludes it."""
    deprecated = CaseRecord(
        case_id="f3f8c3475e2646537212677fbf7c8075",
        application_number="DA2026/0412-FILM",
        resident_session=_REAL_SESSION,
        created_at=datetime.now(UTC),
    )
    assert _is_hygiene_excluded(deprecated)

    canonical_film2 = CaseRecord(
        case_id="cc9bfc59084fd7cac527c479f0e71996",
        application_number="DA2026/0412-FILM2",
        resident_session=_REAL_SESSION,
        created_at=datetime.now(UTC),
    )
    assert not _is_hygiene_excluded(canonical_film2)


# --- docket hygiene: collapsing duplicate application numbers --------------


def test_docket_board_collapses_duplicate_application_numbers_to_the_latest_case(
    client: TestClient,
) -> None:
    """Two real, UUID-sessioned cases for the same `application_number`
    (e.g. two separate live smoke/demo runs against the same real DA
    number, `PAN-661190`/`DA2026/0359` in SMOKE.md's own duplicate-labels
    finding) collapse to a single docket-board row -- the most recently
    created one -- rather than showing every variant as its own "Ready to
    submit" row."""
    older_case_id = _create_case(client, application_number="PAN-661190", session=_REAL_SESSION)
    newer_case_id = _create_case(client, application_number="PAN-661190", session=_REAL_SESSION_2)

    response = client.get("/docket")
    assert response.status_code == 200
    assert newer_case_id in response.text
    assert older_case_id not in response.text

    # Hide only -- never delete: the older case's own page still works.
    older_case_response = client.get(f"/cases/{older_case_id}")
    assert older_case_response.status_code == 200


def test_docket_board_shows_an_earlier_cases_note_for_a_collapsed_application_number(
    client: TestClient,
) -> None:
    _create_case(client, application_number="PAN-661190", session=_REAL_SESSION)
    _create_case(client, application_number="PAN-661190", session=_REAL_SESSION_2)

    response = client.get("/docket")
    assert "+1 earlier case" in response.text


def test_docket_board_pluralizes_the_earlier_cases_note(client: TestClient) -> None:
    third_session = "33333333-3333-4333-8333-333333333333"
    _create_case(client, application_number="PAN-661190", session=_REAL_SESSION)
    _create_case(client, application_number="PAN-661190", session=_REAL_SESSION_2)
    _create_case(client, application_number="PAN-661190", session=third_session)

    response = client.get("/docket")
    assert "+2 earlier cases" in response.text


def test_docket_board_shows_no_earlier_cases_note_for_a_single_case(
    client: TestClient,
) -> None:
    _create_case(client, application_number="PAN-SOLO", session=_REAL_SESSION)
    response = client.get("/docket")
    assert "earlier case" not in response.text


# --- docket access gate (SETBACK_DOCKET_KEY) --------------------------------


def test_docket_board_has_no_gate_when_setback_docket_key_is_unset(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No passphrase is configured (the default, e.g. local dev) -> the
    docket list works exactly as it always has, with no `?key=` needed --
    every test above already relies on this. Explicit here as its own
    documented case rather than only incidentally covered elsewhere."""
    monkeypatch.delenv("SETBACK_DOCKET_KEY", raising=False)
    response = client.get("/docket")
    assert response.status_code == 200


def test_docket_board_requires_the_matching_key_once_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once `SETBACK_DOCKET_KEY` is set, the docket *list* route (the one
    that exposes every resident's case at a glance) needs a matching
    `?key=` -- this is the real PII-exposure gap flagged live: no login,
    no per-session boundary, a stranger's full case reachable from a
    public board. An individual case page's own unguessable URL is
    deliberately left ungated (see the next test) -- a judge who has a
    direct link, or creates their own case, is never blocked."""
    monkeypatch.setenv("SETBACK_DOCKET_KEY", "let-me-in")

    no_key = client.get("/docket")
    assert no_key.status_code == 401

    wrong_key = client.get("/docket?key=nope")
    assert wrong_key.status_code == 401

    right_key = client.get("/docket?key=let-me-in")
    assert right_key.status_code == 200


def test_case_page_stays_reachable_without_the_docket_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_id = _create_case(client)
    monkeypatch.setenv("SETBACK_DOCKET_KEY", "let-me-in")

    response = client.get(f"/cases/{case_id}")
    assert response.status_code == 200


def test_docket_board_survives_a_fresh_app_instance_over_the_same_store(
    store: InMemoryCaseStore, composer: _FakeComposer
) -> None:
    """The board renders `CaseStore.list_cases`, not an in-process registry
    -- a brand-new app built over the same store (modelling a console
    restart/redeploy, or a second replica) must still show every case."""
    first_app = create_app(store, composer=composer, document_source=UserUploadedDocumentSource())
    _create_case(TestClient(first_app), application_number="PAN-1", session=_REAL_SESSION)

    second_app = create_app(store, composer=composer, document_source=UserUploadedDocumentSource())
    response = TestClient(second_app).get("/docket")

    assert response.status_code == 200
    assert "PAN-1" in response.text


def test_case_page_renders_known_sections(client: TestClient) -> None:
    """Post wave-9 merge (LEO-FEEDBACK-UIUX.md §3/§9): the interview lives
    only in the chat pane, and reviewer opinions + gate decisions live
    inside each ground's own accordion rather than as separate flat
    sections -- so this asserts the surviving top-level sections plus the
    chat pane and grounds container, not the now-removed standalone
    "Reviewer opinions"/"Adjudication"/"Gate decisions"/"Interview
    transcript" section titles."""
    case_id = _create_case(client)
    response = client.get(f"/cases/{case_id}")
    assert response.status_code == 200
    body_lower = response.text.lower()
    assert 'id="interview-transcript"' in body_lower  # the chat pane
    for section in ("grounds", "evidence", "submission"):
        assert section in body_lower
    assert "<h3>interview transcript</h3>" not in body_lower
    assert "<h3>reviewer opinions</h3>" not in body_lower
    assert "<h3>adjudication</h3>" not in body_lower
    assert "<h3>gate decisions</h3>" not in body_lower


def test_case_page_does_not_hardcode_a_light_theme(client: TestClient) -> None:
    """Same live-confirmed dark-mode bug as `test_docket_board_does_not_
    hardcode_a_light_theme` above, on the case page's own template."""
    case_id = _create_case(client)
    response = client.get(f"/cases/{case_id}")
    assert 'data-theme="light"' not in response.text


def test_case_page_honours_theme_light_query_param(client: TestClient) -> None:
    """Same filming-consistency override as `test_docket_board_honours_
    theme_light_query_param`, on the case page -- this is the page every
    gallery/demo screenshot is actually captured from."""
    case_id = _create_case(client)
    response = client.get(f"/cases/{case_id}?theme=light")
    assert response.status_code == 200
    assert '<html data-theme="light">' in response.text


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


def test_annotated_overlay_event_links_to_the_full_resolution_document_when_present(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    """Wave-9 click-to-open fix (LEO-FEEDBACK-UIUX.md §5): when
    `job.pipeline` recorded a `full_res_document_id`, the rendered `<img>`
    must carry a `data-full-res-src` pointing `app.js`'s lightbox at this
    case's own document route rather than the shrunk, embedded copy."""
    case_id = _create_case(client)
    asyncio.run(
        store.append_event(
            case_id,
            "annotated-overlay:doc-1",
            "annotated_overlay",
            payload={
                "document_id": "doc-1",
                "mime_type": "image/png",
                "image_base64": "QUJD",
                "full_res_document_id": "overlay-doc-1-p1",
            },
        )
    )
    response = client.get(f"/cases/{case_id}")
    assert response.status_code == 200
    assert f'data-full-res-src="/api/cases/{case_id}/documents/overlay-doc-1-p1"' in response.text


def test_annotated_overlay_event_without_full_res_document_id_has_no_stray_attribute(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    """An overlay event recorded before the wave-9 fix landed (an
    append-only log, never rewritten) has no `full_res_document_id` --
    the `<img>` must degrade gracefully with no `data-full-res-src`
    attribute at all, not a broken link to a document that never
    existed."""
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
    assert "data-full-res-src" not in response.text


def test_full_resolution_overlay_document_is_served_with_the_overlays_own_mime_type() -> None:
    """The full-res overlay document was never uploaded through the
    resident upload endpoint, so `GET .../documents/{document_id}` has no
    matching `document_uploaded` event to read a content type from -- it
    must fall back to the `annotated_overlay` event's own `mime_type`
    field instead of the generic `application/octet-stream` default,
    which would make a browser download the image instead of displaying
    it inline in the lightbox."""
    fixed_store = InMemoryCaseStore()
    fixed_document_source = UserUploadedDocumentSource()
    app = create_app(fixed_store, composer=_FakeComposer(), document_source=fixed_document_source)
    fixed_client = TestClient(app)
    case_id = _create_case(fixed_client)
    full_res_bytes = b"\x89PNG\r\n\x1a\nfake-full-res-bytes"
    asyncio.run(
        fixed_document_source.add_evidence_document(case_id, "overlay-doc-1-p1", full_res_bytes)
    )
    asyncio.run(
        fixed_store.append_event(
            case_id,
            "annotated-overlay:doc-1",
            "annotated_overlay",
            payload={
                "document_id": "doc-1",
                "mime_type": "image/png",
                "image_base64": "QUJD",
                "full_res_document_id": "overlay-doc-1-p1",
            },
        )
    )
    response = fixed_client.get(f"/api/cases/{case_id}/documents/overlay-doc-1-p1")
    assert response.status_code == 200
    assert response.content == full_res_bytes
    assert response.headers["content-type"] == "image/png"


def test_annotated_overlay_event_renders_with_its_own_legend(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    """Regression for the live-reported bug: a normal (server-rendered,
    non-SSE) case-page load previously showed the coloured-box overlay
    image completely bare -- no `.doc-viewer` wrapper, no legend anywhere
    -- because `_render_annotated_overlay_item` only ever emitted a lone
    `<img>`, while `console/static/app.js`'s `handleAnnotatedOverlay`
    built a full legend, but only in response to a *live* SSE event.
    Reloading the page (a documented, common occurrence -- see SMOKE.md's
    multi-instance/post-submission-reload notes) silently lost the legend
    entirely, violating the product's own "any overlay colour on screen
    must have its legend" rule. This asserts the server-rendered path now
    carries the full four-role legend every time, matching
    `evidence.overlays.OverlayRole`/`ROLE_LEGEND_TEXT` exactly, and that
    every legend swatch has a CSS class distinct from the others."""
    from setback.evidence.overlays import ROLE_CSS_CLASS_SUFFIX, ROLE_LEGEND_TEXT, OverlayRole

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
    body = response.text

    assert "doc-viewer__legend" in body
    assert body.count("doc-viewer__legend") == 1, "exactly one legend, not a duplicate"
    for role in OverlayRole:
        suffix = ROLE_CSS_CLASS_SUFFIX[role]
        assert f"legend-swatch--{suffix}" in body
        assert ROLE_LEGEND_TEXT[role] in body


def test_submission_composed_event_renders_actions_and_html_download(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    """LEO-FEEDBACK-UIUX.md §6: the primary actions are Copy text + Email
    this; the HTML download is a secondary link; the Markdown download is
    removed from the UI entirely (though the `.md` API route itself still
    works for anyone who fetches it directly -- see the test below)."""
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
    body = response.text
    assert f"/api/cases/{case_id}/submission.html" in body
    assert f"/api/cases/{case_id}/refusals.html" in body
    assert f"/api/cases/{case_id}/submission.md" not in body
    assert f"/api/cases/{case_id}/refusals.md" not in body
    assert body.count("Copy text") == 2
    assert body.count(">Email this<") == 2
    assert "mailto:?subject=" in body
    assert "Ground text." in body  # the copy-text textarea carries the raw markdown
    assert "<h1>Objection</h1>" in body

    html_response = client.get(f"/api/cases/{case_id}/submission.html")
    assert "<h1>Objection</h1>" in html_response.text


def test_submission_markdown_api_route_still_works_even_though_unlinked_from_the_ui(
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
    md_response = client.get(f"/api/cases/{case_id}/submission.md")
    assert md_response.status_code == 200
    assert md_response.text == "# Objection\n\nGround text."


def test_download_submission_before_composed_is_404(client: TestClient) -> None:
    case_id = _create_case(client)
    response = client.get(f"/api/cases/{case_id}/submission.md")
    assert response.status_code == 404


# --- export transcript (LEO-FEEDBACK-UIUX.md §2) ----------------------------


def test_export_transcript_renders_sydney_timestamped_plain_text_lines() -> None:
    from datetime import UTC, datetime

    fixed_recorded_at = datetime(2026, 8, 29, 9, 42, tzinfo=UTC)  # 19:42 AEST
    fixed_store = InMemoryCaseStore(clock=lambda: fixed_recorded_at)
    app = create_app(
        fixed_store, composer=_FakeComposer(), document_source=UserUploadedDocumentSource()
    )
    fixed_client = TestClient(app)
    case_id = _create_case(fixed_client)
    asyncio.run(
        fixed_store.append_event(
            case_id,
            "interview-turn:1",
            "interview_turn",
            payload={"role": "system", "stage": "opening", "message": "What worries you?"},
        )
    )
    asyncio.run(
        fixed_store.append_event(
            case_id,
            "interview-turn:2",
            "interview_turn",
            payload={"role": "resident", "stage": "opening", "message": "The overshadowing."},
        )
    )
    response = fixed_client.get(f"/api/cases/{case_id}/transcript.txt")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    lines = response.text.strip("\n").splitlines()
    assert lines[0] == "Setback  2026-08-29 19:42 AEST  What worries you?"
    assert lines[1] == "You  2026-08-29 19:42 AEST  The overshadowing."


def test_export_transcript_unknown_case_is_404(client: TestClient) -> None:
    response = client.get("/api/cases/does-not-exist/transcript.txt")
    assert response.status_code == 404


def test_export_transcript_link_present_on_case_page(client: TestClient) -> None:
    case_id = _create_case(client)
    response = client.get(f"/cases/{case_id}")
    assert f"/api/cases/{case_id}/transcript.txt" in response.text
    assert "Export transcript" in response.text


# --- Start tribunal: un-crashable, honest label (LEO-FEEDBACK-UIUX.md §7) ---


def _start_tribunal_button_html(page_html: str) -> str:
    return page_html.split('id="start-tribunal"')[1].split("</button>")[0]


def test_start_tribunal_button_enabled_when_not_started(client: TestClient) -> None:
    case_id = _create_case(client)
    response = client.get(f"/cases/{case_id}")
    button = _start_tribunal_button_html(response.text)
    assert "disabled" not in button
    assert "Start tribunal" in button


def test_start_tribunal_button_disabled_while_running(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    case_id = _create_case(client)
    asyncio.run(
        store.append_event(case_id, "tribunal-requested:x", "tribunal_requested", payload={})
    )
    response = client.get(f"/cases/{case_id}")
    button = _start_tribunal_button_html(response.text)
    assert "disabled" in button
    assert "Tribunal running" in button


def test_start_tribunal_button_disabled_once_submission_composed(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    """The un-crashable case, matching the known job-side idempotency gap
    (SMOKE.md's "Fix 4 -- not fixed"): re-running the tribunal on an
    already-adjudicated case crashes the job. Disabling this button once a
    submission exists closes the UI half of that fix."""
    case_id = _create_case(client)
    asyncio.run(
        store.append_event(case_id, "submission-composed:x", "submission_composed", payload={})
    )
    response = client.get(f"/cases/{case_id}")
    button = _start_tribunal_button_html(response.text)
    assert "disabled" in button
    assert "Tribunal complete" in button


def test_start_tribunal_button_enabled_again_after_a_failed_attempt(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    case_id = _create_case(client)
    asyncio.run(
        store.append_event(case_id, "tribunal-requested:x", "tribunal_requested", payload={})
    )
    asyncio.run(store.append_event(case_id, "job-failed:x", "job_failed", payload={"error": "x"}))
    response = client.get(f"/cases/{case_id}")
    button = _start_tribunal_button_html(response.text)
    assert "disabled" not in button
    assert "Start tribunal" in button


# --- static assets ---------------------------------------------------------


def test_static_assets_are_served(client: TestClient) -> None:
    js = client.get("/static/app.js")
    css = client.get("/static/style.css")
    assert js.status_code == 200
    assert css.status_code == 200


def _css_rule(css_text: str, selector: str, start: int = 0) -> str:
    """The `{...}` declaration block for `selector`'s first occurrence at
    or after `start` -- every rule these tests inspect is flat (no
    nesting), so the first `}` after the opening brace always closes it."""
    open_brace = css_text.index("{", css_text.index(selector, start))
    close_brace = css_text.index("}", open_brace)
    return css_text[open_brace + 1 : close_brace]


def test_chat_pane_has_a_real_viewport_bound_height(client: TestClient) -> None:
    """Wave-12 founder feedback: "the chat keeps expanding vertically as I
    send new messages -- it should have a fixed auto height with the
    screen". `max-height` alone (wave-11) never gave this sticky pane a
    *definite* height, so the percentage/flex sizing below it resolved to
    nothing and an ever-growing transcript just kept pushing the whole
    page taller instead of scrolling internally. A real `height` (not
    just `max-height`), pinned to `dvh` so real (not just large) viewport
    chrome is accounted for, is what fixes that."""
    css = client.get("/static/style.css").text
    rule = _css_rule(css, ".case-layout__chat {")
    assert "display: flex" in rule
    assert "height: calc(100dvh" in rule
    assert "max-height: calc(100dvh" in rule
    # Not a hardcoded space token: the pane sits below the header in
    # normal flow (not at the viewport top) until scrolled far enough for
    # `position: sticky` to engage, so its height must subtract the
    # header's *real* rendered height (app.js-measured, see below) rather
    # than a fixed guess -- a fixed guess left the pane taller than the
    # space actually available and clipped its own input row below the
    # fold on first load (caught live, wave-12 browser QA).
    assert "var(--header-height)" in rule


def test_header_height_custom_property_has_a_root_fallback(client: TestClient) -> None:
    """`--header-height` is set live by app.js, but the CSS still needs a
    sane fallback for the instant before that script runs."""
    css = client.get("/static/style.css").text
    root_rule = _css_rule(css, ":root {")
    assert "--header-height:" in root_rule


def test_app_js_measures_the_real_header_height_for_the_chat_pane(client: TestClient) -> None:
    """The `--header-height` CSS custom property `.case-layout__chat`
    relies on (test above) has to come from somewhere real: this pins
    that app.js actually measures `.topbar` and republishes it, on load
    and on resize (the case-meta line/QR code/wrapping all change the
    header's height at different viewport widths)."""
    js = client.get("/static/app.js").text
    assert ".topbar" in js
    assert "--header-height" in js
    assert 'addEventListener("resize"' in js


def test_chat_card_fills_the_pane_height_for_its_transcript_to_flex_against(
    client: TestClient,
) -> None:
    css = client.get("/static/style.css").text
    rule = _css_rule(css, ".case-layout__chat .chat-card {")
    assert "height: 100%" in rule
    assert "min-height: 0" in rule


def test_chat_transcript_min_height_is_zero_so_it_can_actually_shrink_to_scroll(
    client: TestClient,
) -> None:
    """A flex item's default `min-height: auto` resolves to its content's
    intrinsic height, which silently defeats `overflow-y: auto` -- the
    transcript would just keep growing to fit every message rather than
    scrolling inside the fixed-height pane above it. `min-height: 0`
    overrides that default and is what actually makes the transcript (not
    the page) the thing that scrolls."""
    css = client.get("/static/style.css").text
    rule = _css_rule(css, ".case-layout__chat .chat-transcript {")
    assert "flex: 1" in rule
    assert "min-height: 0" in rule


def test_mobile_chat_pane_resets_the_desktop_fixed_height(client: TestClient) -> None:
    """Below the 860px breakpoint the layout stacks to a single column and
    the chat pane is no longer sticky -- the desktop `height`/
    `max-height` pairing above must reset to `auto`/`none` there, or a
    short mobile viewport would clip the (now static-positioned) card
    instead of letting it size to its own content, per wave-11's existing
    single-column behaviour."""
    css = client.get("/static/style.css").text
    media_start = css.index("@media (max-width: 860px)")
    rule = _css_rule(css, ".case-layout__chat {", start=media_start)
    assert "position: static" in rule
    assert "height: auto" in rule
    assert "max-height: none" in rule


def test_mobile_chat_transcript_stays_bounded_and_scrollable(client: TestClient) -> None:
    """Wave-11 already bounded the mobile transcript to `50vh` with
    `overflow-y: auto` inherited from the base `.chat-transcript` rule --
    this wave adds a `dvh` fallback (same reasoning as the desktop pane)
    and this test pins that it keeps behaving the same way (internal
    scroll, pinned input) rather than regressing alongside the desktop
    fix above."""
    css = client.get("/static/style.css").text
    first = css.index(".case-layout__chat .chat-transcript {")
    rule = _css_rule(css, ".case-layout__chat .chat-transcript {", start=first + 1)
    assert "50dvh" in rule


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


# --- wave 5: human-rendered document_uploaded / interview_turn (UI-SPEC.md §3.3/§3.4) ---


def test_document_uploaded_renders_a_doc_card_with_no_raw_json(client: TestClient) -> None:
    case_id = _create_case(client)
    client.post(
        f"/api/cases/{case_id}/documents",
        files={"file": ("north-elevation.pdf", io.BytesIO(b"%PDF-fake"), "application/pdf")},
    )
    response = client.get(f"/cases/{case_id}")
    assert response.status_code == 200
    # No raw event payload keys leaking through as literal JSON text.
    assert '"document_id"' not in response.text
    assert '"content_type"' not in response.text
    assert '"size_bytes"' not in response.text
    assert 'class="doc-card doc-card--clickable"' in response.text
    assert "North elevation" in response.text
    # A plain-English DocumentKind label, never the raw enum value.
    assert "Elevations" in response.text


def test_document_uploaded_pdf_gets_no_provenance_badge(client: TestClient) -> None:
    case_id = _create_case(client)
    client.post(
        f"/api/cases/{case_id}/documents",
        files={"file": ("site-plan.pdf", io.BytesIO(b"%PDF-fake"), "application/pdf")},
    )
    response = client.get(f"/cases/{case_id}")
    assert "tag--grade-a" not in response.text


def test_document_uploaded_photo_gets_your_photo_provenance_tag(client: TestClient) -> None:
    case_id = _create_case(client)
    client.post(
        f"/api/cases/{case_id}/documents",
        files={"file": ("garden.jpg", io.BytesIO(_FAKE_JPEG_BYTES), "image/jpeg")},
    )
    response = client.get(f"/cases/{case_id}")
    assert 'class="tag tag--grade-a"' in response.text
    assert "Your photo" in response.text


def test_street_view_fallback_document_renders_in_the_evidence_section(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    """LEO-FEEDBACK-UIUX.md §4 / wave-9 populate pass "Blocker 2": a
    `job.pipeline`-recorded Street View fallback (a `document_uploaded`
    event carrying `provenance_grade: "B"`, never a resident's own upload)
    must render as a real, attributed doc-card in the Evidence section --
    found live to be silently absent (the fetch succeeded but nothing ever
    told the console it existed). Regression-tests the renderer contract
    directly, since reproducing the live pipeline's own network calls is
    out of this module's scope."""
    case_id = _create_case(client)
    asyncio.run(
        store.append_event(
            case_id,
            "document-uploaded:street-view-fallback",
            "document_uploaded",
            payload={
                "document_id": "street-view-fallback",
                "filename": "Street View fallback ((c) Google Street View, 2024-06)",
                "content_type": "image/jpeg",
                "size_bytes": 4096,
                "provenance_grade": "B",
            },
        )
    )
    response = client.get(f"/cases/{case_id}")
    assert response.status_code == 200
    assert 'class="tag tag--grade-b"' in response.text
    assert "Archival Street View" in response.text
    expected_src = f"/api/cases/{case_id}/documents/street-view-fallback"
    assert f'<img class="doc-card__thumb" src="{expected_src}"' in response.text
    # Never mislabelled as the resident's own photo.
    assert "Your photo" not in response.text
    # The Google Street View attribution requirement (evidence.imagery's
    # module docstring) must be genuinely visible text, not just present
    # somewhere in the markup (e.g. tucked into a hover-only title="..."
    # tooltip on the grade badge, which technically contains the
    # substring but shows a resident nothing without hovering).
    assert '<p class="doc-card__attribution">(c) Google Street View, 2024-06</p>' in response.text


def test_street_view_fallback_card_opens_the_lightbox_not_a_new_tab(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    """Founder-reported P1: clicking a Street View fallback image always
    opened it in a new browser tab instead of the same in-page lightbox
    the annotated overlay uses. The fix drops the `<a target="_blank">`
    wrapper for every *image* doc-card (this one included) in favour of a
    keyboard-accessible trigger `app.js`'s lightbox wires up, carrying the
    full-resolution source, alt text, and the attribution caption as
    plain data attributes -- a PDF doc-card is untouched, see
    `test_document_uploaded_pdf_card_still_opens_in_a_new_tab`."""
    case_id = _create_case(client)
    asyncio.run(
        store.append_event(
            case_id,
            "document-uploaded:street-view-fallback",
            "document_uploaded",
            payload={
                "document_id": "street-view-fallback",
                "filename": "Street View fallback ((c) Google Street View, 2024-06)",
                "content_type": "image/jpeg",
                "size_bytes": 4096,
                "provenance_grade": "B",
            },
        )
    )
    response = client.get(f"/cases/{case_id}")
    expected_src = f"/api/cases/{case_id}/documents/street-view-fallback"
    assert 'class="doc-card doc-card--clickable doc-card--lightbox"' in response.text
    assert f'data-lightbox-src="{expected_src}"' in response.text
    assert 'data-lightbox-alt="Street View (archival)"' in response.text
    assert 'data-lightbox-caption="(c) Google Street View, 2024-06"' in response.text
    # Keyboard-accessible: a non-native-button trigger needs both hooks.
    assert 'role="button"' in response.text
    assert 'tabindex="0"' in response.text
    # And, crucially, no more escape hatch to a new tab for this card.
    assert 'target="_blank"' not in response.text


# --- doc-card real thumbnails for uploaded photo evidence -------------------


def test_document_uploaded_photo_renders_a_real_img_thumbnail(client: TestClient) -> None:
    """Regression for the gallery-reported gap: an uploaded photo (a
    resident's own evidence) previously always rendered the generic grey
    placeholder icon -- a judge sees a gallery shot captioned "Test photo"
    next to a file icon, never the actual photo. A photo upload now gets a
    real `<img>` thumbnail, served back from wherever the bytes actually
    live (`EvidenceUploadStore.download_document` -- in-memory in tests,
    GCS in production) via this case's own `/api/cases/{id}/documents/{id}`
    endpoint, so it works identically against the deployed app."""
    case_id = _create_case(client)
    upload = client.post(
        f"/api/cases/{case_id}/documents",
        files={"file": ("garden.jpg", io.BytesIO(_FAKE_JPEG_BYTES), "image/jpeg")},
    )
    document_id = upload.json()["document_id"]

    response = client.get(f"/cases/{case_id}")
    assert response.status_code == 200
    assert "doc-card__thumb--placeholder" not in response.text
    expected_src = f"/api/cases/{case_id}/documents/{document_id}"
    assert f'<img class="doc-card__thumb" src="{expected_src}"' in response.text


def test_document_uploaded_photo_card_opens_the_lightbox_not_a_new_tab(
    client: TestClient,
) -> None:
    """A resident's own photo upload had the exact same new-tab
    inconsistency the founder reported for the Street View card (both go
    through `_render_document_uploaded_item`'s one `is_photo` branch) --
    the same fix covers it: any image evidence card is now a
    keyboard-accessible lightbox trigger, never a new-tab link. A PDF
    still opens in a new tab -- see
    `test_document_uploaded_pdf_card_still_opens_in_a_new_tab` -- since a
    lightbox `<img>` cannot render one."""
    case_id = _create_case(client)
    upload = client.post(
        f"/api/cases/{case_id}/documents",
        files={"file": ("garden.jpg", io.BytesIO(_FAKE_JPEG_BYTES), "image/jpeg")},
    )
    document_id = upload.json()["document_id"]
    response = client.get(f"/cases/{case_id}")
    expected_src = f"/api/cases/{case_id}/documents/{document_id}"
    assert 'class="doc-card doc-card--clickable doc-card--lightbox"' in response.text
    assert f'data-lightbox-src="{expected_src}"' in response.text
    assert 'role="button"' in response.text
    assert 'tabindex="0"' in response.text
    assert 'target="_blank"' not in response.text
    # No attribution caption for a resident's own photo -- nothing to cite.
    assert "data-lightbox-caption" not in response.text


def test_document_uploaded_pdf_card_still_opens_in_a_new_tab(client: TestClient) -> None:
    """A lightbox `<img>` cannot render a PDF, so a PDF doc-card keeps the
    pre-existing new-tab link behaviour (LEO-FEEDBACK-UIUX.md §4) -- only
    image evidence moved to the in-page lightbox."""
    case_id = _create_case(client)
    client.post(
        f"/api/cases/{case_id}/documents",
        files={"file": ("north-elevation.pdf", io.BytesIO(b"%PDF-fake"), "application/pdf")},
    )
    response = client.get(f"/cases/{case_id}")
    expected_href = f'href="/api/cases/{case_id}/documents/'
    assert 'class="doc-card doc-card--clickable"' in response.text
    assert "doc-card--lightbox" not in response.text
    assert expected_href in response.text
    assert 'target="_blank"' in response.text
    assert 'rel="noopener"' in response.text


def test_document_uploaded_pdf_still_renders_the_placeholder_icon(client: TestClient) -> None:
    """Only photo evidence gets a real thumbnail -- a PDF doc-card keeps
    the placeholder icon (no PDF-preview pipeline exists, and none is
    asked for here)."""
    case_id = _create_case(client)
    client.post(
        f"/api/cases/{case_id}/documents",
        files={"file": ("site-plan.pdf", io.BytesIO(b"%PDF-fake"), "application/pdf")},
    )
    response = client.get(f"/cases/{case_id}")
    assert "doc-card__thumb--placeholder" in response.text
    # Scoped to the Evidence tabpanel -- the page header's own QR code
    # (`<img class="case-actions__qr">`) is an unrelated `<img>`. Bounded by
    # the *next* panel's own id (rather than a naive `</div>` split, which
    # would truncate at the first nested closing div inside this panel's
    # own markup) since panels render in `_SECTION_TABS`' fixed order.
    evidence_panel = response.text.split('id="panel-evidence"')[1].split('id="panel-overlay"')[0]
    assert "<img" not in evidence_panel


def test_get_uploaded_document_serves_the_stored_bytes(client: TestClient) -> None:
    case_id = _create_case(client)
    upload = client.post(
        f"/api/cases/{case_id}/documents",
        files={"file": ("garden.jpg", io.BytesIO(_FAKE_JPEG_BYTES), "image/jpeg")},
    )
    document_id = upload.json()["document_id"]

    response = client.get(f"/api/cases/{case_id}/documents/{document_id}")
    assert response.status_code == 200
    assert response.content == _FAKE_JPEG_BYTES
    assert response.headers["content-type"] == "image/jpeg"


def test_get_uploaded_document_unknown_document_is_404(client: TestClient) -> None:
    case_id = _create_case(client)
    response = client.get(f"/api/cases/{case_id}/documents/does-not-exist")
    assert response.status_code == 404


def test_get_uploaded_document_unknown_case_is_404(client: TestClient) -> None:
    response = client.get("/api/cases/does-not-exist/documents/does-not-exist")
    assert response.status_code == 404


# --- QR code / copy-link re-access (LEO-FEEDBACK-UIUX.md §1) ----------------


def test_case_qr_code_returns_a_png(client: TestClient) -> None:
    case_id = _create_case(client)
    response = client.get(f"/api/cases/{case_id}/qr.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_case_qr_code_unknown_case_is_404(client: TestClient) -> None:
    response = client.get("/api/cases/does-not-exist/qr.png")
    assert response.status_code == 404


def test_case_page_has_copy_link_and_qr_code(client: TestClient) -> None:
    case_id = _create_case(client)
    response = client.get(f"/cases/{case_id}")
    assert "Copy link" in response.text
    assert f"/api/cases/{case_id}/qr.png" in response.text


def test_interview_turn_has_no_standalone_server_rendered_section_and_no_raw_json(
    client: TestClient,
) -> None:
    """Wave-9 (LEO-FEEDBACK-UIUX.md §2): the standalone "Interview
    transcript" section is removed -- the chat pane (`#interview-
    transcript`, populated client-side from the JSON interview API) is now
    the ONLY place the transcript renders. This also closes the original
    founder requirement #3 concern (no raw `interview_turn` JSON anywhere
    on the page) simply by removing the section that used to leak it."""
    case_id = _create_case(client)
    client.get(f"/api/cases/{case_id}/interview")
    client.post(
        f"/api/cases/{case_id}/interview",
        json={"answer": "It'll overshadow my garden."},
    )
    response = client.get(f"/cases/{case_id}")
    assert response.status_code == 200
    assert '"role"' not in response.text
    assert '"stage"' not in response.text
    assert "<h3>Interview transcript</h3>" not in response.text
    # The chat pane exists, empty in the server HTML -- app.js populates it
    # from `GET /api/cases/{id}/interview` on load.
    assert 'id="interview-transcript" class="chat-transcript"' in response.text
    transcript_div = response.text.split('id="interview-transcript" class="chat-transcript"')[1]
    assert transcript_div.split(">", 1)[1].split("<", 1)[0].strip() == ""


def test_get_interview_turn_prompt_carries_no_html_markup(client: TestClient) -> None:
    """The interview JSON API (`app.js`'s sole source for the chat pane's
    bubbles, post wave-9) returns plain prompt text -- `app.js` is
    responsible for the AI/resident label+bubble markup client-side; the
    server never emits HTML for a turn."""
    case_id = _create_case(client)
    response = client.get(f"/api/cases/{case_id}/interview")
    body = response.json()
    assert body["turns"][0]["stage"] == "opening"
    assert "<" not in body["turns"][0]["prompt"]


# --- wave 5: suggested_replies (UI-SPEC.md §2.2) -----------------------------


def test_suggested_replies_present_at_confirming_stage(client: TestClient) -> None:
    case_id = _create_case(client)
    client.get(f"/api/cases/{case_id}/interview")
    client.post(f"/api/cases/{case_id}/interview", json={"answer": "Overshadowing my garden."})
    client.post(f"/api/cases/{case_id}/interview", json={"answer": "Loses sun in winter."})
    response = client.post(f"/api/cases/{case_id}/interview", json={"answer": "No photos."})
    body = response.json()
    assert body["stage"] == "confirming"
    assert body["suggested_replies"] == ["Yes, that's right", "No, let me fix that"]


def test_suggested_replies_present_for_requesting_evidence_stage(client: TestClient) -> None:
    case_id = _create_case(client)
    client.get(f"/api/cases/{case_id}/interview")
    response = client.post(
        f"/api/cases/{case_id}/interview", json={"answer": "Overshadowing my garden."}
    )
    assert response.json()["stage"] == "clarifying"
    response = client.post(
        f"/api/cases/{case_id}/interview", json={"answer": "Loses sun in winter."}
    )
    assert response.json()["stage"] == "requesting_evidence"
    assert response.json()["suggested_replies"] == ["Skip for now"]


def test_suggested_replies_absent_for_clarifying_stage(client: TestClient) -> None:
    case_id = _create_case(client)
    client.get(f"/api/cases/{case_id}/interview")
    response = client.post(
        f"/api/cases/{case_id}/interview", json={"answer": "Overshadowing my garden."}
    )
    assert response.json()["stage"] == "clarifying"
    assert response.json()["suggested_replies"] is None


# --- wave 5: ground cards, all 5 GroundStatus values (UI-SPEC.md §2.6/§3.6) --


async def _make_ground(
    store: InMemoryCaseStore, case_id: str, ground_id: str, status: GroundStatus
) -> None:
    await store.propose_ground(case_id, ground_id, claim=f"Claim for {ground_id}")
    if status is not GroundStatus.PROPOSED:
        await store.transition_ground(case_id, ground_id, GroundStatus.UNDER_REVIEW)
    if status not in (GroundStatus.PROPOSED, GroundStatus.UNDER_REVIEW):
        await store.transition_ground(case_id, ground_id, status)


def test_ground_card_covers_all_five_ground_statuses(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    case_id = _create_case(client)
    asyncio.run(_make_ground(store, case_id, "g-proposed", GroundStatus.PROPOSED))
    asyncio.run(_make_ground(store, case_id, "g-under-review", GroundStatus.UNDER_REVIEW))
    asyncio.run(_make_ground(store, case_id, "g-supported", GroundStatus.SUPPORTED))
    asyncio.run(_make_ground(store, case_id, "g-refused", GroundStatus.REFUSED))
    asyncio.run(_make_ground(store, case_id, "g-flagged", GroundStatus.FLAGGED))

    response = client.get(f"/cases/{case_id}")
    body = response.text
    assert 'class="ground-card ground-card--pending"' in body  # proposed + under_review
    assert body.count("ground-card--pending") == 2
    assert 'class="ground-card ground-card--shipped"' in body
    assert 'class="ground-card ground-card--refused"' in body
    assert 'class="ground-card ground-card--flagged"' in body
    assert '<span class="tag tag--shipped">Shipped</span>' in body
    assert '<span class="tag tag--refused">Refused</span>' in body
    assert '<span class="tag tag--flagged">Flagged</span>' in body


def test_ground_card_shows_statutory_basis_and_explanation_from_gate_decision(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    case_id = _create_case(client)
    asyncio.run(_make_ground(store, case_id, "g-shipped", GroundStatus.SUPPORTED))
    asyncio.run(
        store.append_event(
            case_id,
            "gate-decision:g-shipped",
            "gate_decision",
            payload={
                "ground_id": "g-shipped",
                "status": "shipped",
                "category": "environmental_and_social_impacts",
                "explanation": "The shadow diagram shows a clear winter impact.",
                "statutory_basis": "s4.15(1)(b)",
                "citation_issues": [],
            },
        )
    )
    response = client.get(f"/cases/{case_id}")
    body = response.text
    assert "s4.15(1)(b)" in body
    assert "The shadow diagram shows a clear winter impact." in body
    assert 'class="citation-chip citation-chip--clause"' in body


# --- wave 5: refusal card is informational, warm brown, never --error (UI-SPEC.md §2.9) ---


def test_refused_gate_decision_renders_as_a_refusal_card_region(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    case_id = _create_case(client)
    asyncio.run(_make_ground(store, case_id, "g-1", GroundStatus.REFUSED))
    asyncio.run(
        store.append_event(
            case_id,
            "gate-decision:g-1",
            "gate_decision",
            payload={
                "ground_id": "g-1",
                "status": "refused-irrelevant",
                "category": "property_value",
                "explanation": "Property value alone is not a s4.15(1) planning matter.",
                "statutory_basis": "s4.15(1)",
                "citation_issues": [],
            },
        )
    )
    response = client.get(f"/cases/{case_id}")
    body = response.text
    assert 'class="refusal-card" role="region"' in body
    # LEO-FEEDBACK-UIUX.md §3: gate copy must NAME the ground -- never a
    # generic "We didn't include this ground" a resident has to
    # cross-reference against a claim shown somewhere else on the page.
    assert "We didn&rsquo;t include: Claim for g-1" in body
    assert "Property value alone is not a s4.15(1) planning matter." in body
    assert 'role="alert"' not in body  # never framed as an error
    assert 'class="state-card--error"' not in body


def test_refusal_card_states_how_many_other_grounds_are_unaffected(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    case_id = _create_case(client)
    asyncio.run(_make_ground(store, case_id, "g-refused", GroundStatus.REFUSED))
    asyncio.run(_make_ground(store, case_id, "g-supported-1", GroundStatus.SUPPORTED))
    asyncio.run(_make_ground(store, case_id, "g-supported-2", GroundStatus.SUPPORTED))
    asyncio.run(
        store.append_event(
            case_id,
            "gate-decision:g-refused",
            "gate_decision",
            payload={
                "ground_id": "g-refused",
                "status": "refused-unsubstantiated",
                "category": "environmental_and_social_impacts",
                "explanation": "Not well-founded on the material provided.",
                "statutory_basis": "s4.15(1)(b)",
                "citation_issues": [],
            },
        )
    )
    response = client.get(f"/cases/{case_id}")
    assert "Your other 2 grounds are unaffected." in response.text


# --- wave 5: docket board derived status tag (UI-SPEC.md §3.1) --------------


def test_docket_board_shows_just_started_when_no_grounds(client: TestClient) -> None:
    _create_case(client)
    response = client.get("/docket")
    assert '<span class="tag tag--pending">Just started</span>' in response.text


def test_docket_board_shows_needs_your_input_when_a_ground_is_flagged(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    case_id = _create_case(client)
    asyncio.run(_make_ground(store, case_id, "g-1", GroundStatus.FLAGGED))
    response = client.get("/docket")
    assert '<span class="tag tag--flagged">Needs your input</span>' in response.text


def test_docket_board_shows_in_review_while_a_ground_is_still_pending(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    case_id = _create_case(client)
    asyncio.run(_make_ground(store, case_id, "g-1", GroundStatus.PROPOSED))
    response = client.get("/docket")
    assert '<span class="tag tag--pending">In review</span>' in response.text


def test_docket_board_shows_ready_to_submit_once_every_ground_is_terminal(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    case_id = _create_case(client)
    asyncio.run(_make_ground(store, case_id, "g-1", GroundStatus.SUPPORTED))
    asyncio.run(_make_ground(store, case_id, "g-2", GroundStatus.REFUSED))
    response = client.get("/docket")
    assert '<span class="tag tag--shipped">Ready to submit</span>' in response.text


# --- wave 5: disclaimer footer, every page (UI-SPEC.md §2.14) ----------------


def test_disclaimer_footer_present_on_docket_board(client: TestClient) -> None:
    response = client.get("/docket")
    assert 'class="disclaimer-footer"' in response.text
    assert "not a law firm" in response.text
    assert "NSW Government" in response.text


def test_disclaimer_footer_present_on_case_page(client: TestClient) -> None:
    case_id = _create_case(client)
    response = client.get(f"/cases/{case_id}")
    assert 'class="disclaimer-footer"' in response.text


# --- wave 5: check-your-answers summary list (UI-SPEC.md §3.8) --------------


def test_check_answers_absent_before_interview_reaches_done(client: TestClient) -> None:
    case_id = _create_case(client)
    client.get(f"/api/cases/{case_id}/interview")
    response = client.get(f"/cases/{case_id}")
    assert 'class="card check-answers"' not in response.text


def test_check_answers_appears_once_interview_reaches_done(client: TestClient) -> None:
    case_id = _create_case(client)
    client.get(f"/api/cases/{case_id}/interview")
    client.post(f"/api/cases/{case_id}/interview", json={"answer": "Overshadowing my garden."})
    client.post(f"/api/cases/{case_id}/interview", json={"answer": "Loses sun in winter."})
    client.post(f"/api/cases/{case_id}/interview", json={"answer": "No photos."})
    client.post(f"/api/cases/{case_id}/interview", json={"answer": "Yes, correct."})
    response = client.post(f"/api/cases/{case_id}/interview", json={"answer": "No, that's all."})
    assert response.json()["stage"] == "done"

    page = client.get(f"/cases/{case_id}")
    assert 'class="card check-answers"' in page.text
    assert "Ground 1" in page.text
    assert "Change" in page.text
    assert "overshadow" in page.text.lower()


# --- wave 5 / P0 carry-forward: ground claims use redacted_text, never raw PII ---
# (UI-SPEC.md's wave-4 carry-forward: NormalisedConcern.redacted_text, never
# raw resident text, must back every ground claim.)


def test_ground_claim_uses_redacted_text_not_raw_resident_statement(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    case_id = _create_case(client)
    client.get(f"/api/cases/{case_id}/interview")
    client.post(
        f"/api/cases/{case_id}/interview",
        json={"answer": "My name is Jane Smith, it'll overshadow my garden."},
    )
    client.post(f"/api/cases/{case_id}/interview", json={"answer": "Loses sun in winter."})
    client.post(f"/api/cases/{case_id}/interview", json={"answer": "No photos."})
    response = client.post(f"/api/cases/{case_id}/interview", json={"answer": "Yes, correct."})
    assert response.json()["stage"] == "ask_more"

    grounds = asyncio.run(store.list_grounds(case_id))
    assert len(grounds) == 1
    assert "Jane Smith" not in grounds[0].claim
    assert "[NAME]" in grounds[0].claim
    assert "overshadow" in grounds[0].claim.lower()


# --- wave 5 / P0 carry-forward: ConcernNormaliser wired into production InterviewFlow ---


class _RecordingConcernNormaliser:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def normalise(self, text: str) -> list[NormalisedConcern]:
        self.calls.append(text)
        from setback.clerk import ConcernType

        return [
            NormalisedConcern(
                category=ConcernType.OVERSHADOWING,
                target=None,
                qualifiers=[],
                redacted_text=text,
            )
        ]


def test_create_app_wires_an_injected_concern_normaliser_into_the_interview_flow(
    store: InMemoryCaseStore, composer: _FakeComposer
) -> None:
    normaliser = _RecordingConcernNormaliser()
    app = create_app(
        store,
        composer=composer,
        document_source=UserUploadedDocumentSource(),
        concern_normaliser=normaliser,
    )
    client = TestClient(app)
    case_id = _create_case(client)
    client.get(f"/api/cases/{case_id}/interview")
    client.post(f"/api/cases/{case_id}/interview", json={"answer": "It overshadows my garden."})

    assert normaliser.calls == ["It overshadows my garden."]


# --- wave 5: cost visibility -- ledger total exposed on the case page (P0 carry-forward) ---


def test_case_page_exposes_the_run_cost_as_a_data_attribute(
    store: InMemoryCaseStore, composer: _FakeComposer
) -> None:
    from setback.models.client import TokenUsage
    from setback.state.ledger import Ledger

    app = create_app(store, composer=composer, document_source=UserUploadedDocumentSource())
    client = TestClient(app)
    case_id = _create_case(client)

    ledger = Ledger()
    ledger.record(
        stage="test",
        model="gemini-3.5-flash-lite",
        usage=TokenUsage(prompt_tokens=1000, output_tokens=1000),
    )
    asyncio.run(store.save_ledger(case_id, ledger))

    response = client.get(f"/cases/{case_id}")
    assert ledger.total_cost_usd > 0
    expected = f"{ledger.total_cost_usd:.6f}"
    assert f'data-run-cost-usd="{expected}"' in response.text


def test_case_page_exposes_zero_run_cost_with_no_ledger(client: TestClient) -> None:
    case_id = _create_case(client)
    response = client.get(f"/cases/{case_id}")
    assert 'data-run-cost-usd="0.000000"' in response.text


# --- ship-phase smoke fix: three event types were missing from
# `_EVENT_ITEM_RENDERERS`, falling through `_render_events_section`'s
# fallback branch and rendering literal `json.dumps(...)` text -- found
# live on the deployed console (a `tribunal_requested` event rendered as
# a bare " {}" in the "Tribunal" section), violating founder requirement
# #3 (zero raw JSON anywhere user-facing). -----------------------------


def test_tribunal_requested_timestamp_is_converted_from_utc_to_sydney_not_read_verbatim() -> None:
    """Round-2 UI feedback, item 4: the tribunal-start timestamp now lives
    in the case header, in the exact `DD/MM/YYYY HH:MM AM/PM` format
    requested (never a bare ISO string, never the stored UTC value read as
    if it were already local). 03:00 UTC in the southern-hemisphere winter
    (AEST, UTC+10) renders as 1:00 PM the same day -- a bug that read the
    raw UTC hour would show 03:00 AM instead."""
    from datetime import UTC, datetime

    fixed_recorded_at = datetime(2026, 7, 15, 3, 0, tzinfo=UTC)
    fixed_store = InMemoryCaseStore(clock=lambda: fixed_recorded_at)
    app = create_app(
        fixed_store, composer=_FakeComposer(), document_source=UserUploadedDocumentSource()
    )
    fixed_client = TestClient(app)
    case_id = _create_case(fixed_client)
    asyncio.run(
        fixed_store.append_event(case_id, "tribunal-requested:x", "tribunal_requested", payload={})
    )
    response = fixed_client.get(f"/cases/{case_id}")
    assert "Tribunal started 15/07/2026 01:00 PM" in response.text
    assert "03:00 AM" not in response.text


def test_tribunal_requested_renders_in_the_header_meta_line_with_no_raw_json(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    """Round-2 UI feedback, item 4: the standalone "Tribunal" tab/section
    is gone entirely -- its one piece of resident-facing content (the
    start time) now lives in the case header's own `.case-meta` line."""
    case_id = _create_case(client)
    asyncio.run(
        store.append_event(case_id, "tribunal-requested:x", "tribunal_requested", payload={})
    )
    response = client.get(f"/cases/{case_id}")
    assert response.status_code == 200
    assert "{}" not in response.text
    assert '<p class="case-meta">Tribunal started' in response.text
    assert "<h3>Tribunal</h3>" not in response.text


def test_case_meta_line_absent_before_any_tribunal_run(client: TestClient) -> None:
    """No "Tribunal started ..." line should appear before a tribunal has
    ever been requested -- there is nothing yet to report."""
    case_id = _create_case(client)
    response = client.get(f"/cases/{case_id}")
    assert response.status_code == 200
    assert 'class="case-meta"' not in response.text


def test_ingest_resolved_event_renders_live_success_with_no_raw_json(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    """Wave-9 handoff, relocated by round 2, item 4: `job.pipeline`'s
    `ingest_resolved` event (the un-frozen-ingest outcome) must render in
    plain English inside the Grounds tab's small "Notes" card, never fall
    through to the raw-JSON fallback."""
    case_id = _create_case(client)
    asyncio.run(
        store.append_event(
            case_id,
            "ingest-resolved:x",
            "ingest_resolved",
            payload={
                "application_number": "DA2026/0512",
                "council": "Georges River Council",
                "council_application_number": "DA2026-0512",
                "address": "12 Example Street",
                "used_demo_fixture": False,
            },
        )
    )
    response = client.get(f"/cases/{case_id}")
    assert response.status_code == 200
    notes_html = response.text.split('class="card case-notes"')[1].split("</section>")[0]
    assert "{" not in notes_html
    assert "used_demo_fixture" not in response.text
    assert "Fetched live council data for DA2026/0512" in response.text


def test_ingest_resolved_event_renders_demo_fixture_fallback_honestly(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    """When live ingest failed and the run degraded to the demo fixture,
    the resident must be told plainly -- their submission's letterhead
    will not match the DA number they typed -- rather than this being
    silently invisible."""
    case_id = _create_case(client)
    asyncio.run(
        store.append_event(
            case_id,
            "ingest-resolved:x",
            "ingest_resolved",
            payload={
                "application_number": "DA2026/UNKNOWN",
                "council": "Georges River Council",
                "council_application_number": "DA2026-0359",
                "address": "65A Vista Street",
                "used_demo_fixture": True,
                "reason": "could not resolve 'DA2026/UNKNOWN' live",
            },
        )
    )
    response = client.get(f"/cases/{case_id}")
    assert response.status_code == 200
    assert "Could not fetch DA2026/UNKNOWN live" in response.text
    assert "showing the demo case (DA2026-0359) instead" in response.text


def test_tribunal_rerun_ignored_event_renders_with_no_raw_json(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    """A judge double-pressing "Start tribunal" against an already-decided
    case (SMOKE.md's "Fix 4") must see an honest plain-English note, not a
    raw JSON dump of the pipeline's internal reason string."""
    case_id = _create_case(client)
    asyncio.run(
        store.append_event(
            case_id,
            "tribunal-rerun-ignored:x",
            "tribunal_rerun_ignored",
            payload={"reason": "this case's tribunal has already run to completion"},
        )
    )
    response = client.get(f"/cases/{case_id}")
    assert response.status_code == 200
    notes_html = response.text.split('class="card case-notes"')[1].split("</section>")[0]
    assert "{" not in notes_html
    assert "already" in response.text.lower()
    assert "run" in response.text.lower()


def test_ground_rerun_skipped_event_never_appears_in_the_rendered_page(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    """`ground_rerun_skipped` is an internal resume-safety signal with no
    resident-facing value (per the fixer's own cross-lane note) -- it must
    not appear anywhere on the page, raw or otherwise, but its mere
    presence in the event log must not break rendering either."""
    case_id = _create_case(client)
    asyncio.run(
        store.append_event(
            case_id,
            "ground-rerun-skipped:ground-1",
            "ground_rerun_skipped",
            payload={"ground_id": "ground-1", "status": "supported"},
        )
    )
    response = client.get(f"/cases/{case_id}")
    assert response.status_code == 200
    assert "ground_rerun_skipped" not in response.text
    assert "ground-rerun-skipped" not in response.text


def test_adjudication_decision_event_renders_inside_its_ground_with_no_raw_json(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    """Wave-9 (LEO-FEEDBACK-UIUX.md §3): the standalone "Adjudication"
    section is gone -- an adjudicator's ruling on a ground renders INSIDE
    that ground's own accordion, alongside the reviewers' opinions, so a
    resident sees the claim, the opinions, and the final call together
    rather than three flat lists with no visible link between them."""
    case_id = _create_case(client)
    asyncio.run(_make_ground(store, case_id, "ground-1", GroundStatus.FLAGGED))
    asyncio.run(
        store.append_event(
            case_id,
            "adjudication:ground-1",
            "adjudication_decision",
            payload={
                "ground_id": "ground-1",
                "outcome": "resolved",
                "stance": "supports",
                "confidence": 0.82,
                "cited_anchor_ids": ["anchor-1"],
                "rationale": "Both reviewers agreed the shadow diagram supports the claim.",
                "source": "adjudicated",
            },
        )
    )
    response = client.get(f"/cases/{case_id}")
    assert response.status_code == 200
    assert "<h3>Adjudication</h3>" not in response.text
    # Bare field-name substrings, so this catches the leak whether or not
    # `_esc`'s `html.escape` has turned the surrounding quotes into
    # `&quot;` -- a browser renders escaped JSON as readable JSON text just
    # the same, which is exactly what founder requirement #3 forbids.
    assert "cited_anchor_ids" not in response.text
    ground_section = response.text.split('class="ground-card ground-card--flagged"')[1]
    assert "Both reviewers agreed the shadow diagram supports the claim." in ground_section


def test_resident_refusal_feedback_event_renders_with_no_raw_json(
    client: TestClient, store: InMemoryCaseStore
) -> None:
    case_id = _create_case(client)
    asyncio.run(
        store.append_event(
            case_id,
            "refusal-feedback:ground-1:x",
            "resident_refusal_feedback",
            payload={
                "ground_id": "ground-1",
                "pushback": "I still think this should count.",
                "original_explanation": "Property value is not a s4.15(1) matter.",
                "re_rendered_explanation": "I hear you -- but property value still isn't "
                "a matter s4.15(1) lets us weigh, so it can't be included. Your "
                "disagreement is on record.",
            },
        )
    )
    response = client.get(f"/cases/{case_id}")
    assert response.status_code == 200
    assert "re_rendered_explanation" not in response.text
    assert "original_explanation" not in response.text
    assert "I still think this should count." in response.text
    assert "Your disagreement is on record." in response.text


# --- round-2 UI feedback, item 1: real tabs, not a ref-link nav ------------
#
# The founder's own correction: "the tabs rendered on the right side do not
# show which one is selected, and content should only be rendered for the
# selected tab (it's not a ref link for the page block, it's an interactive
# component that renders the associated content when it's selected)." These
# pin the server-rendered half of the contract (real ARIA tablist markup,
# exactly one visible panel, Grounds selected by default) -- the client-side
# `hidden`-toggling behaviour on click/arrow-key is `app.js`'s lane and has
# no Python test harness in this repo (documented convention: CSS/JS
# behaviour is verified live via a real browser, not unit-tested here).


def test_case_page_renders_a_real_tablist_not_a_ref_link_nav(client: TestClient) -> None:
    case_id = _create_case(client)
    response = client.get(f"/cases/{case_id}")
    assert response.status_code == 200
    assert 'role="tablist"' in response.text
    assert response.text.count('role="tab"') == 4
    assert response.text.count('role="tabpanel"') == 4
    # The old anchor-link nav is gone entirely, not just renamed.
    assert "<nav" not in response.text
    assert 'class="section-nav"' not in response.text


def test_case_page_defaults_to_the_grounds_tab_selected(client: TestClient) -> None:
    case_id = _create_case(client)
    response = client.get(f"/cases/{case_id}")
    assert response.status_code == 200
    assert (
        'id="tab-grounds" aria-controls="panel-grounds" aria-selected="true" tabindex="0"'
        in response.text
    )
    # Every non-default tab starts deselected, keyboard-inert, and hidden.
    for tab_id in ("evidence", "overlay", "documents"):
        expected = f'aria-controls="panel-{tab_id}" aria-selected="false" tabindex="-1"'
        assert expected in response.text
    grounds_panel = response.text.split('id="panel-grounds"')[1].split(">", 1)[1]
    assert "hidden" not in response.text.split('id="panel-grounds"')[1].split(">", 1)[0]
    for tab_id in ("evidence", "overlay", "documents"):
        panel_open_tag = response.text.split(f'id="panel-{tab_id}"')[1].split(">", 1)[0]
        assert "hidden" in panel_open_tag
    assert grounds_panel  # sanity: content exists to be shown


def test_case_page_has_no_tribunal_tab_or_section(client: TestClient) -> None:
    """Round-2 UI feedback, item 4: the Tribunal tab/section is removed
    entirely, not merely hidden or relabelled."""
    case_id = _create_case(client)
    response = client.get(f"/cases/{case_id}")
    assert response.status_code == 200
    assert 'id="tab-tribunal"' not in response.text
    assert 'id="panel-tribunal"' not in response.text
    assert "<h3>Tribunal</h3>" not in response.text
    assert ">Tribunal<" not in response.text


# --- round-2 UI feedback, item 3: the chat input row is one line -----------
#
# "Upload button is spilling out of the chat container... remove the
# 'Choose file / No file chosen' part. Make it a one-liner: User answer
# input text | Send button | Upload button."


def test_chat_input_row_is_a_single_form_with_no_native_file_input_text(
    client: TestClient,
) -> None:
    case_id = _create_case(client)
    response = client.get(f"/cases/{case_id}")
    assert response.status_code == 200
    # One form holds the text input, Send, and the upload trigger together
    # -- not two separate `<form>` rows stacked on top of each other.
    assert response.text.count('<form id="interview-form"') == 1
    assert '<form id="upload-form"' not in response.text
    chat_form = response.text.split('<form id="interview-form"')[1].split("</form>")[0]
    assert 'id="interview-input"' in chat_form
    assert 'class="chat-form__send"' in chat_form
    assert 'id="upload-trigger"' in chat_form
    assert 'id="upload-input"' in chat_form
    # The native file input is present (for the browser's own file picker)
    # but visually hidden -- no "Choose file"/"No file chosen" text is ever
    # shown, since that text belongs to the native, non-hidden control.
    assert 'class="visually-hidden" tabindex="-1" aria-hidden="true"' in chat_form
    assert "No file chosen" not in response.text
    # The upload trigger is a styled button (icon + "Upload" label), never
    # a bare submit button with the raw "Upload photo/document" copy the
    # old two-form layout used.
    assert "Upload photo/document" not in response.text
    assert "chat-form__upload-label" in chat_form


def test_chat_input_row_selected_file_feedback_is_a_chip_not_native_text(
    client: TestClient,
) -> None:
    """The selected/uploaded-file feedback element exists as a distinct
    chip/toast (`#upload-status-chip`), separate from the native file
    input's own text, and starts hidden (nothing selected yet)."""
    case_id = _create_case(client)
    response = client.get(f"/cases/{case_id}")
    assert response.status_code == 200
    assert '<p id="upload-status-chip" class="upload-chip" hidden' in response.text


# --- public-abuse guard: privileged session cookie ---------------------------


def test_docket_valid_key_sets_the_privileged_cookie(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETBACK_DOCKET_KEY", "let-me-in")
    response = client.get("/docket?key=let-me-in")
    assert response.status_code == 200
    assert "sb_priv" in response.cookies


def test_docket_wrong_key_never_sets_the_privileged_cookie(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETBACK_DOCKET_KEY", "let-me-in")
    response = client.get("/docket?key=wrong")
    assert response.status_code == 401
    assert "sb_priv" not in response.cookies


def test_docket_with_no_configured_key_never_sets_the_privileged_cookie(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `SETBACK_DOCKET_KEY` at all (local dev) disables the docket gate
    entirely (see `_docket_key_accepted`) -- there is no real secret to
    have proven knowledge of, so no privileged session is ever granted."""
    monkeypatch.delenv("SETBACK_DOCKET_KEY", raising=False)
    response = client.get("/docket")
    assert response.status_code == 200
    assert "sb_priv" not in response.cookies


def test_docket_board_shows_public_spend_percentage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The current spend % against the public ceiling is surfaced only on
    the key-gated docket page (founder/judge-visible), never on any public
    page."""
    monkeypatch.setenv("SETBACK_DOCKET_KEY", "let-me-in")
    response = client.get("/docket?key=let-me-in")
    assert response.status_code == 200
    assert "%" in response.text


# --- public-abuse guard: per-client daily case-creation cap ------------------


def test_case_creation_daily_cap_blocks_the_same_client_after_the_limit(
    store: InMemoryCaseStore, composer: _FakeComposer, job_trigger: _RecordingJobTrigger
) -> None:
    from setback.state.guard_store import InMemoryGuardCounterStore

    app = create_app(
        store,
        composer=composer,
        job_trigger=job_trigger,
        guard_counter_store=InMemoryGuardCounterStore(),
    )
    limited_client = TestClient(app)
    for i in range(5):
        response = limited_client.post(
            "/api/cases",
            json={"application_number": f"PAN-{i}", "resident_session": _REAL_SESSION},
        )
        assert response.status_code == 201, response.text
    sixth = limited_client.post(
        "/api/cases",
        json={"application_number": "PAN-6", "resident_session": _REAL_SESSION},
    )
    assert sixth.status_code == 429


def test_case_creation_daily_cap_reads_the_last_x_forwarded_for_entry(
    store: InMemoryCaseStore, composer: _FakeComposer, job_trigger: _RecordingJobTrigger
) -> None:
    """A forged first `X-Forwarded-For` entry (client-controlled) must not
    let an actor evade the cap by rotating a fake leading address -- only
    the LAST entry (Google Front End's own append) is trusted."""
    from setback.state.guard_store import InMemoryGuardCounterStore

    app = create_app(
        store,
        composer=composer,
        job_trigger=job_trigger,
        guard_counter_store=InMemoryGuardCounterStore(),
    )
    limited_client = TestClient(app)
    for i in range(5):
        response = limited_client.post(
            "/api/cases",
            json={"application_number": f"PAN-{i}", "resident_session": _REAL_SESSION},
            headers={"X-Forwarded-For": f"9.9.9.{i}, 5.5.5.5"},
        )
        assert response.status_code == 201, response.text
    # Same real (last-entry) address, a different forged leading entry --
    # still the same actor, still capped.
    sixth = limited_client.post(
        "/api/cases",
        json={"application_number": "PAN-6", "resident_session": _REAL_SESSION},
        headers={"X-Forwarded-For": "1.2.3.4, 5.5.5.5"},
    )
    assert sixth.status_code == 429


def test_case_creation_daily_cap_tracks_distinct_real_clients_independently(
    store: InMemoryCaseStore, composer: _FakeComposer, job_trigger: _RecordingJobTrigger
) -> None:
    """Two different real (last-entry) client IPs are tracked as separate
    actors by the new daily cap -- kept to one request per IP here so this
    stays isolated from the pre-existing, separately-scoped hourly per-IP
    limiter (`per_ip_case_creation_guard`, also capped at 5), which is
    exercised on its own in `test_guards.py`."""
    from setback.state.guard_store import InMemoryGuardCounterStore

    app = create_app(
        store,
        composer=composer,
        job_trigger=job_trigger,
        guard_counter_store=InMemoryGuardCounterStore(),
    )
    limited_client = TestClient(app)
    first = limited_client.post(
        "/api/cases",
        json={"application_number": "PAN-a0", "resident_session": _REAL_SESSION},
        headers={"X-Forwarded-For": "1.1.1.1"},
    )
    assert first.status_code == 201, first.text
    second = limited_client.post(
        "/api/cases",
        json={"application_number": "PAN-b0", "resident_session": _REAL_SESSION_2},
        headers={"X-Forwarded-For": "2.2.2.2"},
    )
    assert second.status_code == 201, second.text


def test_privileged_session_bypasses_the_daily_case_creation_cap(
    store: InMemoryCaseStore, composer: _FakeComposer, job_trigger: _RecordingJobTrigger
) -> None:
    from setback.state.guard_store import InMemoryGuardCounterStore

    app = create_app(
        store,
        composer=composer,
        job_trigger=job_trigger,
        guard_counter_store=InMemoryGuardCounterStore(),
    )
    # `base_url="https://..."` so the `Secure`-flagged `sb_priv` cookie
    # (correctly withheld by any HTTP client over plain http://) is
    # actually round-tripped on the client's later requests, matching how
    # a real browser behaves against the deployed (HTTPS) Cloud Run service.
    with TestClient(app, base_url="https://testserver") as privileged_client:
        os.environ["SETBACK_DOCKET_KEY"] = "let-me-in"
        try:
            privileged_client.get("/docket?key=let-me-in")
            for i in range(7):  # well past the 5/day cap
                response = privileged_client.post(
                    "/api/cases",
                    json={
                        "application_number": f"PAN-priv-{i}",
                        "resident_session": _REAL_SESSION,
                    },
                )
                assert response.status_code == 201, response.text
        finally:
            del os.environ["SETBACK_DOCKET_KEY"]


def test_tampered_privileged_cookie_does_not_bypass_the_daily_cap(
    store: InMemoryCaseStore, composer: _FakeComposer, job_trigger: _RecordingJobTrigger
) -> None:
    from setback.state.guard_store import InMemoryGuardCounterStore

    app = create_app(
        store,
        composer=composer,
        job_trigger=job_trigger,
        guard_counter_store=InMemoryGuardCounterStore(),
    )
    with TestClient(app, base_url="https://testserver") as tampered_client:
        os.environ["SETBACK_DOCKET_KEY"] = "let-me-in"
        try:
            tampered_client.get("/docket?key=let-me-in")
            tampered_client.cookies.set("sb_priv", "0" * 64)
            for i in range(5):
                tampered_client.post(
                    "/api/cases",
                    json={
                        "application_number": f"PAN-tamper-{i}",
                        "resident_session": _REAL_SESSION,
                    },
                )
            sixth = tampered_client.post(
                "/api/cases",
                json={"application_number": "PAN-tamper-6", "resident_session": _REAL_SESSION},
            )
            assert sixth.status_code == 429
        finally:
            del os.environ["SETBACK_DOCKET_KEY"]


# --- public-abuse guard: 30-turn interview cap -------------------------------


def test_interview_turn_cap_blocks_after_the_limit(client: TestClient) -> None:
    case_id = _create_case(client)
    client.get(f"/api/cases/{case_id}/interview")
    last_status = 200
    for i in range(31):
        response = client.post(
            f"/api/cases/{case_id}/interview", json={"answer": f"answer number {i}"}
        )
        last_status = response.status_code
    assert last_status == 429


def test_interview_turn_cap_does_not_affect_a_different_case(client: TestClient) -> None:
    case_a = _create_case(client, application_number="PAN-A", session=_REAL_SESSION)
    case_b = _create_case(client, application_number="PAN-B", session=_REAL_SESSION_2)
    client.get(f"/api/cases/{case_a}/interview")
    client.get(f"/api/cases/{case_b}/interview")
    for i in range(30):
        client.post(f"/api/cases/{case_a}/interview", json={"answer": f"a{i}"})
    thirty_first_on_a = client.post(f"/api/cases/{case_a}/interview", json={"answer": "one more"})
    assert thirty_first_on_a.status_code == 429
    still_fine_on_b = client.post(f"/api/cases/{case_b}/interview", json={"answer": "hello"})
    assert still_fine_on_b.status_code == 200


# --- public-abuse guard: 5-upload-per-case cap -------------------------------


def test_upload_cap_blocks_after_five_uploads(client: TestClient) -> None:
    case_id = _create_case(client)
    for i in range(5):
        # Distinct content per upload: `document_id` is `sha256(content)`,
        # and `append_event` dedups by exact event id -- identical bytes
        # across uploads would collapse into a single `document_uploaded`
        # event and never actually exercise the cap.
        content = io.BytesIO(_FAKE_JPEG_BYTES + str(i).encode())
        response = client.post(
            f"/api/cases/{case_id}/documents",
            files={"file": (f"doc{i}.jpg", content, "image/jpeg")},
        )
        assert response.status_code == 200, response.text
    sixth = client.post(
        f"/api/cases/{case_id}/documents",
        files={"file": ("doc6.jpg", io.BytesIO(_FAKE_JPEG_BYTES + b"6"), "image/jpeg")},
    )
    assert sixth.status_code == 429


# --- public-abuse guard: global spend ceiling / count backstops -------------


def test_anonymous_mutating_endpoints_are_paused_once_the_ceiling_is_reached(
    store: InMemoryCaseStore, composer: _FakeComposer, job_trigger: _RecordingJobTrigger
) -> None:
    from setback.state.guard_store import InMemoryGuardTotalsStore

    totals_store = InMemoryGuardTotalsStore()
    app = create_app(
        store, composer=composer, job_trigger=job_trigger, guard_totals_store=totals_store
    )
    paused_client = TestClient(app)

    async def _blow_the_ceiling() -> None:
        await totals_store.add_spend(9999.0)

    asyncio.run(_blow_the_ceiling())

    response = paused_client.post(
        "/api/cases", json={"application_number": "PAN-1", "resident_session": _REAL_SESSION}
    )
    assert response.status_code == 429
    assert "key" not in response.text.lower()
    assert "bypass" not in response.text.lower()
    assert "cookie" not in response.text.lower()


def test_reads_stay_open_when_the_ceiling_is_reached(
    store: InMemoryCaseStore, composer: _FakeComposer, job_trigger: _RecordingJobTrigger
) -> None:
    """Landing, an existing case page, and the key-gated docket must all
    stay reachable forever, even once the public guard has paused every
    mutating endpoint."""
    from setback.state.guard_store import InMemoryGuardTotalsStore

    totals_store = InMemoryGuardTotalsStore()
    app = create_app(
        store, composer=composer, job_trigger=job_trigger, guard_totals_store=totals_store
    )
    open_client = TestClient(app)
    case_id = open_client.post(
        "/api/cases", json={"application_number": "PAN-1", "resident_session": _REAL_SESSION}
    ).json()["case_id"]

    async def _blow_the_ceiling() -> None:
        await totals_store.add_spend(9999.0)

    asyncio.run(_blow_the_ceiling())

    assert open_client.get("/").status_code == 200
    assert open_client.get(f"/cases/{case_id}").status_code == 200
    assert open_client.get(f"/api/cases/{case_id}/interview").status_code == 200


def test_landing_page_shows_the_paused_banner_once_the_ceiling_is_reached(
    store: InMemoryCaseStore, composer: _FakeComposer, job_trigger: _RecordingJobTrigger
) -> None:
    from setback.state.guard_store import InMemoryGuardTotalsStore

    totals_store = InMemoryGuardTotalsStore()
    app = create_app(
        store, composer=composer, job_trigger=job_trigger, guard_totals_store=totals_store
    )
    paused_client = TestClient(app)

    async def _blow_the_ceiling() -> None:
        await totals_store.add_spend(9999.0)

    asyncio.run(_blow_the_ceiling())

    response = paused_client.get("/")
    assert response.status_code == 200
    assert "used up" in response.text
    assert "key" not in response.text.lower()


def test_landing_page_has_no_banner_while_under_the_ceiling(client: TestClient) -> None:
    response = client.get("/")
    assert "used up" not in response.text


def test_privileged_session_bypasses_the_spend_ceiling(
    store: InMemoryCaseStore, composer: _FakeComposer, job_trigger: _RecordingJobTrigger
) -> None:
    from setback.state.guard_store import InMemoryGuardTotalsStore

    totals_store = InMemoryGuardTotalsStore()
    app = create_app(
        store, composer=composer, job_trigger=job_trigger, guard_totals_store=totals_store
    )

    async def _blow_the_ceiling() -> None:
        await totals_store.add_spend(9999.0)

    asyncio.run(_blow_the_ceiling())

    # `base_url="https://..."` so the `Secure`-flagged `sb_priv` cookie
    # (correctly withheld by any HTTP client over plain http://) is
    # actually round-tripped on the client's later requests, matching how
    # a real browser behaves against the deployed (HTTPS) Cloud Run service.
    with TestClient(app, base_url="https://testserver") as privileged_client:
        os.environ["SETBACK_DOCKET_KEY"] = "let-me-in"
        try:
            privileged_client.get("/docket?key=let-me-in")
            response = privileged_client.post(
                "/api/cases",
                json={"application_number": "PAN-priv", "resident_session": _REAL_SESSION},
            )
            assert response.status_code == 201, response.text
        finally:
            del os.environ["SETBACK_DOCKET_KEY"]
