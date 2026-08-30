"""Tests for setback.state.firestore: the case-store port and its two
implementations (InMemoryCaseStore, FirestoreCaseStore).

Behavioural contract tests run against BOTH implementations via a
parametrized `store` fixture: `InMemoryCaseStore` directly, and
`FirestoreCaseStore` wired to `_FakeFirestore`, a minimal in-process double
of the `google.cloud.firestore.AsyncClient` surface this module actually
uses (document/collection refs, get/set, async streaming). This exercises
the Firestore adapter's real translation and idempotency-check code paths
fully offline — no emulator, no ADC, no network — while guaranteeing both
backends agree on behaviour.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from setback.evidence.dossier import ProvenanceGrade
from setback.models.client import TokenUsage
from setback.state.breakers import CircuitBreaker, CircuitState
from setback.state.firestore import (
    BreakerSnapshot,
    CaseNotFoundError,
    CaseStore,
    EventType,
    FirestoreCaseStore,
    GroundEvidenceAnchor,
    GroundNotFoundError,
    GroundStatus,
    InMemoryCaseStore,
    InvalidGroundTransitionError,
    LedgerCallSnapshot,
    LedgerSnapshot,
    case_id_for,
    restore_breaker,
    restore_ledger,
    resume_case,
    snapshot_breaker,
    snapshot_ledger,
)
from setback.state.ledger import Ledger

# --- A tiny fake of the AsyncClient surface FirestoreCaseStore uses ---------


class _FakeSnapshot:
    def __init__(self, doc_id: str, data: dict[str, Any] | None) -> None:
        self.id = doc_id
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return dict(self._data) if self._data is not None else None


class _FakeCollectionRef:
    def __init__(self, root: _FakeFirestore, path: tuple[str, ...]) -> None:
        self._root = root
        self._path = path

    def document(self, document_id: str) -> _FakeDocumentRef:
        return _FakeDocumentRef(self._root, (*self._path, document_id))

    async def stream(self) -> AsyncIterator[_FakeSnapshot]:
        prefix_len = len(self._path) + 1
        for key in list(self._root.docs):
            if len(key) == prefix_len and key[:-1] == self._path:
                yield _FakeSnapshot(key[-1], self._root.docs[key])

    def order_by(self, field: str, direction: str = "ASCENDING") -> _FakeQuery:
        return _FakeQuery(self._root, self._path, field, direction)


class _FakeQuery:
    """Minimal stand-in for the chained `order_by().limit().stream()` surface
    `FirestoreCaseStore.list_cases` uses against the root `cases` collection."""

    def __init__(
        self,
        root: _FakeFirestore,
        path: tuple[str, ...],
        field: str,
        direction: str,
        limit: int | None = None,
    ) -> None:
        self._root = root
        self._path = path
        self._field = field
        self._direction = direction
        self._limit = limit

    def limit(self, count: int) -> _FakeQuery:
        return _FakeQuery(self._root, self._path, self._field, self._direction, count)

    async def stream(self) -> AsyncIterator[_FakeSnapshot]:
        prefix_len = len(self._path) + 1
        matches = [
            (key[-1], self._root.docs[key])
            for key in list(self._root.docs)
            if len(key) == prefix_len and key[:-1] == self._path
        ]
        matches.sort(key=lambda kv: kv[1][self._field], reverse=self._direction == "DESCENDING")
        if self._limit is not None:
            matches = matches[: self._limit]
        for doc_id, data in matches:
            yield _FakeSnapshot(doc_id, data)


class _FakeDocumentRef:
    def __init__(self, root: _FakeFirestore, path: tuple[str, ...]) -> None:
        self._root = root
        self._path = path
        self.id = path[-1]

    async def get(self) -> _FakeSnapshot:
        return _FakeSnapshot(self.id, self._root.docs.get(self._path))

    async def set(self, document_data: dict[str, Any], merge: bool = False) -> None:
        self._root.docs[self._path] = dict(document_data)

    def collection(self, name: str) -> _FakeCollectionRef:
        return _FakeCollectionRef(self._root, (*self._path, name))


class _FakeFirestore:
    """Stands in for `google.cloud.firestore.AsyncClient`: enough surface
    (`.collection()` at the root, `.document()`/`.get()`/`.set()`/`.stream()`
    beneath it) to run `FirestoreCaseStore` against, with no network."""

    def __init__(self) -> None:
        self.docs: dict[tuple[str, ...], dict[str, Any]] = {}

    def collection(self, name: str) -> _FakeCollectionRef:
        return _FakeCollectionRef(self, (name,))


class _FakeClock:
    """A deterministic, monotonically-advancing clock for reproducible
    ordering assertions without relying on real wall-clock time."""

    def __init__(self) -> None:
        self._t = 0

    def __call__(self) -> datetime:
        self._t += 1
        return datetime(2026, 8, 29, tzinfo=UTC) + timedelta(seconds=self._t)


@dataclass
class _StoreUnderTest:
    """A `CaseStore` plus a human-readable id for pytest's -k/-v output."""

    store: CaseStore
    label: str


def _memory_store() -> _StoreUnderTest:
    return _StoreUnderTest(InMemoryCaseStore(clock=_FakeClock()), "in-memory")


def _firestore_store() -> _StoreUnderTest:
    return _StoreUnderTest(
        FirestoreCaseStore(_FakeFirestore(), clock=_FakeClock()),  # type: ignore[arg-type]
        "firestore-fake",
    )


@pytest.fixture(params=[_memory_store, _firestore_store])
def store(request: pytest.FixtureRequest) -> CaseStore:
    return request.param().store


# --- case_id_for -------------------------------------------------------------


def test_case_id_for_is_deterministic() -> None:
    first = case_id_for("PAN-661190", "session-abc")
    second = case_id_for("PAN-661190", "session-abc")
    assert first == second


def test_case_id_for_differs_by_input() -> None:
    a = case_id_for("PAN-661190", "session-abc")
    b = case_id_for("PAN-661190", "session-xyz")
    c = case_id_for("PAN-000000", "session-abc")
    assert len({a, b, c}) == 3


# --- create_case / get_case ---------------------------------------------------


async def test_create_case_returns_a_new_case_record(store: CaseStore) -> None:
    case = await store.create_case(application_number="PAN-661190", resident_session="s1")

    assert case.case_id == case_id_for("PAN-661190", "s1")
    assert case.application_number == "PAN-661190"
    assert case.resident_session == "s1"


async def test_create_case_is_idempotent(store: CaseStore) -> None:
    first = await store.create_case(application_number="PAN-661190", resident_session="s1")
    second = await store.create_case(application_number="PAN-661190", resident_session="s1")

    assert first == second
    events = await store.list_events(first.case_id)
    created_events = [e for e in events if e.event_type == EventType.CASE_CREATED.value]
    assert len(created_events) == 1


async def test_get_case_returns_none_for_unknown_case(store: CaseStore) -> None:
    assert await store.get_case("does-not-exist") is None


async def test_create_case_defaults_to_not_public_origin(store: CaseStore) -> None:
    case = await store.create_case(application_number="PAN-661190", resident_session="s1")

    events = await store.list_events(case.case_id)
    created_event = next(e for e in events if e.event_type == EventType.CASE_CREATED.value)
    assert created_event.payload["public_origin"] is False


async def test_create_case_records_public_origin_when_requested(store: CaseStore) -> None:
    """A `public_origin=True` case (an anonymous console visitor) records
    that on its own `case_created` event -- `job.pipeline`'s public-guard
    cost booking reads exactly this event/payload back to decide whether a
    completed tribunal run's ledger cost should count against the public
    spend ceiling."""
    case = await store.create_case(
        application_number="PAN-661190", resident_session="s1", public_origin=True
    )

    events = await store.list_events(case.case_id)
    created_event = next(e for e in events if e.event_type == EventType.CASE_CREATED.value)
    assert created_event.payload["public_origin"] is True


# --- list_cases ------------------------------------------------------------


async def test_list_cases_orders_newest_first(store: CaseStore) -> None:
    first = await store.create_case(application_number="PAN-1", resident_session="s1")
    second = await store.create_case(application_number="PAN-2", resident_session="s2")
    third = await store.create_case(application_number="PAN-3", resident_session="s3")

    cases = await store.list_cases()

    assert [c.case_id for c in cases] == [third.case_id, second.case_id, first.case_id]


async def test_list_cases_respects_limit(store: CaseStore) -> None:
    await store.create_case(application_number="PAN-1", resident_session="s1")
    await store.create_case(application_number="PAN-2", resident_session="s2")
    await store.create_case(application_number="PAN-3", resident_session="s3")

    cases = await store.list_cases(limit=2)

    assert len(cases) == 2


async def test_list_cases_empty_store_returns_empty(store: CaseStore) -> None:
    assert await store.list_cases() == ()


# --- propose_ground ------------------------------------------------------------


async def test_propose_ground_on_unknown_case_raises(store: CaseStore) -> None:
    with pytest.raises(CaseNotFoundError):
        await store.propose_ground("no-such-case", "g1", claim="height exceeds LEP limit")


async def test_propose_ground_creates_proposed_ground(store: CaseStore) -> None:
    case = await store.create_case(application_number="PAN-661190", resident_session="s1")

    ground = await store.propose_ground(
        case.case_id, "height-exceedance", claim="Height exceeds the 9m LEP limit"
    )

    assert ground.ground_id == "height-exceedance"
    assert ground.case_id == case.case_id
    assert ground.status is GroundStatus.PROPOSED
    assert ground.anchors == ()


async def test_propose_ground_is_idempotent(store: CaseStore) -> None:
    case = await store.create_case(application_number="PAN-661190", resident_session="s1")

    first = await store.propose_ground(case.case_id, "height-exceedance", claim="claim A")
    second = await store.propose_ground(case.case_id, "height-exceedance", claim="claim B")

    assert first == second
    assert second.claim == "claim A"  # the second call did not overwrite it
    events = await store.list_events(case.case_id)
    proposed = [e for e in events if e.event_type == EventType.GROUND_PROPOSED.value]
    assert len(proposed) == 1


async def test_list_grounds_for_unknown_case_is_empty(store: CaseStore) -> None:
    assert await store.list_grounds("no-such-case") == ()


async def test_get_ground_returns_none_when_absent(store: CaseStore) -> None:
    case = await store.create_case(application_number="PAN-661190", resident_session="s1")
    assert await store.get_ground(case.case_id, "missing") is None


# --- transition_ground ---------------------------------------------------------


async def test_transition_ground_follows_lifecycle(store: CaseStore) -> None:
    case = await store.create_case(application_number="PAN-661190", resident_session="s1")
    await store.propose_ground(case.case_id, "height-exceedance", claim="claim")

    under_review = await store.transition_ground(
        case.case_id, "height-exceedance", GroundStatus.UNDER_REVIEW
    )
    assert under_review.status is GroundStatus.UNDER_REVIEW

    supported = await store.transition_ground(
        case.case_id, "height-exceedance", GroundStatus.SUPPORTED
    )
    assert supported.status is GroundStatus.SUPPORTED


async def test_transition_ground_rejects_skipping_under_review(store: CaseStore) -> None:
    case = await store.create_case(application_number="PAN-661190", resident_session="s1")
    await store.propose_ground(case.case_id, "height-exceedance", claim="claim")

    with pytest.raises(InvalidGroundTransitionError):
        await store.transition_ground(case.case_id, "height-exceedance", GroundStatus.SUPPORTED)


async def test_transition_ground_rejects_leaving_a_terminal_state(store: CaseStore) -> None:
    case = await store.create_case(application_number="PAN-661190", resident_session="s1")
    await store.propose_ground(case.case_id, "height-exceedance", claim="claim")
    await store.transition_ground(case.case_id, "height-exceedance", GroundStatus.UNDER_REVIEW)
    await store.transition_ground(case.case_id, "height-exceedance", GroundStatus.REFUSED)

    with pytest.raises(InvalidGroundTransitionError):
        await store.transition_ground(case.case_id, "height-exceedance", GroundStatus.FLAGGED)


async def test_transition_ground_repeating_current_status_is_idempotent(
    store: CaseStore,
) -> None:
    case = await store.create_case(application_number="PAN-661190", resident_session="s1")
    await store.propose_ground(case.case_id, "height-exceedance", claim="claim")
    await store.transition_ground(case.case_id, "height-exceedance", GroundStatus.UNDER_REVIEW)

    repeated = await store.transition_ground(
        case.case_id, "height-exceedance", GroundStatus.UNDER_REVIEW
    )

    assert repeated.status is GroundStatus.UNDER_REVIEW
    events = await store.list_events(case.case_id)
    status_events = [e for e in events if e.event_type == EventType.GROUND_STATUS_CHANGED.value]
    assert len(status_events) == 1  # the repeat did not log a second transition


async def test_transition_ground_unknown_ground_raises(store: CaseStore) -> None:
    case = await store.create_case(application_number="PAN-661190", resident_session="s1")
    with pytest.raises(GroundNotFoundError):
        await store.transition_ground(case.case_id, "missing", GroundStatus.UNDER_REVIEW)


# --- add_evidence_anchor --------------------------------------------------------


def _anchor(page: int = 3) -> GroundEvidenceAnchor:
    return GroundEvidenceAnchor(
        source_doc="elevations.pdf",
        page=page,
        bbox=(10.0, 20.0, 110.0, 220.0),
        provenance_grade=ProvenanceGrade.DOCUMENTS_ONLY,
    )


async def test_add_evidence_anchor_appends_anchor(store: CaseStore) -> None:
    case = await store.create_case(application_number="PAN-661190", resident_session="s1")
    await store.propose_ground(case.case_id, "height-exceedance", claim="claim")

    updated = await store.add_evidence_anchor(case.case_id, "height-exceedance", _anchor())

    assert updated.anchors == (_anchor(),)


async def test_add_evidence_anchor_is_idempotent(store: CaseStore) -> None:
    case = await store.create_case(application_number="PAN-661190", resident_session="s1")
    await store.propose_ground(case.case_id, "height-exceedance", claim="claim")

    await store.add_evidence_anchor(case.case_id, "height-exceedance", _anchor())
    twice = await store.add_evidence_anchor(case.case_id, "height-exceedance", _anchor())

    assert len(twice.anchors) == 1
    events = await store.list_events(case.case_id)
    anchored = [e for e in events if e.event_type == EventType.EVIDENCE_ANCHORED.value]
    assert len(anchored) == 1


async def test_add_evidence_anchor_unknown_ground_raises(store: CaseStore) -> None:
    case = await store.create_case(application_number="PAN-661190", resident_session="s1")
    with pytest.raises(GroundNotFoundError):
        await store.add_evidence_anchor(case.case_id, "missing", _anchor())


# --- append-only event log ------------------------------------------------------


async def test_append_event_is_idempotent_by_event_id(store: CaseStore) -> None:
    case = await store.create_case(application_number="PAN-661190", resident_session="s1")

    first = await store.append_event(
        case.case_id, "custom-event-1", "interview_turn_recorded", payload={"turn": 1}
    )
    second = await store.append_event(
        case.case_id, "custom-event-1", "interview_turn_recorded", payload={"turn": 999}
    )

    assert first == second
    assert second.payload == {"turn": 1}  # the second call did not overwrite it


async def test_append_event_on_unknown_case_raises(store: CaseStore) -> None:
    with pytest.raises(CaseNotFoundError):
        await store.append_event("no-such-case", "e1", "custom", payload={})


async def test_events_are_ordered_by_sequence(store: CaseStore) -> None:
    case = await store.create_case(application_number="PAN-661190", resident_session="s1")
    await store.append_event(case.case_id, "e1", "custom", payload={})
    await store.append_event(case.case_id, "e2", "custom", payload={})
    await store.append_event(case.case_id, "e3", "custom", payload={})

    events = await store.list_events(case.case_id)

    sequences = [e.sequence for e in events]
    assert sequences == sorted(sequences)
    event_ids = [e.event_id for e in events]
    # case-created is always first; the three custom events follow in order.
    assert event_ids[1:] == ["e1", "e2", "e3"]


async def test_list_events_for_unknown_case_is_empty(store: CaseStore) -> None:
    assert await store.list_events("no-such-case") == ()


# --- breaker persistence ---------------------------------------------------------


async def test_save_and_load_breaker_round_trips_closed_state(store: CaseStore) -> None:
    case = await store.create_case(application_number="PAN-661190", resident_session="s1")
    breaker = CircuitBreaker(name="bench", failure_threshold=3, reset_timeout_seconds=30.0)

    await store.save_breaker(case.case_id, breaker)
    restored = await store.load_breakers(case.case_id)

    assert restored["bench"].state is CircuitState.CLOSED
    assert restored["bench"].failure_threshold == 3
    assert restored["bench"].reset_timeout_seconds == 30.0


async def test_save_and_load_breaker_round_trips_open_state(store: CaseStore) -> None:
    case = await store.create_case(application_number="PAN-661190", resident_session="s1")
    breaker = CircuitBreaker(name="bench", failure_threshold=2, reset_timeout_seconds=30.0)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open

    await store.save_breaker(case.case_id, breaker)
    restored = await store.load_breakers(case.case_id)

    assert restored["bench"].is_open


async def test_load_breakers_for_unknown_case_is_empty(store: CaseStore) -> None:
    assert await store.load_breakers("no-such-case") == {}


def test_snapshot_and_restore_breaker_are_pure_round_trip() -> None:
    breaker = CircuitBreaker(name="interview", failure_threshold=4, reset_timeout_seconds=45.0)
    breaker.record_failure()

    snapshot = snapshot_breaker(breaker)
    restored = restore_breaker(snapshot)

    assert snapshot == BreakerSnapshot(
        stage="interview",
        state=CircuitState.CLOSED,
        failure_threshold=4,
        reset_timeout_seconds=45.0,
    )
    assert restored.state is CircuitState.CLOSED
    assert restored.name == "interview"


# --- ledger persistence ------------------------------------------------------------


async def test_save_and_load_ledger_round_trips_records(store: CaseStore) -> None:
    case = await store.create_case(application_number="PAN-661190", resident_session="s1")
    ledger = Ledger(ceiling_usd=5.0)
    ledger.record(
        stage="interview",
        model="gemini-3.5-flash-lite",
        usage=TokenUsage(prompt_tokens=1000, output_tokens=500),
    )

    await store.save_ledger(case.case_id, ledger)
    restored = await store.load_ledger(case.case_id)

    assert restored is not None
    assert restored.ceiling_usd == 5.0
    assert restored.total_cost_usd == pytest.approx(ledger.total_cost_usd)
    assert len(restored.records) == 1
    assert restored.records[0].stage == "interview"


async def test_load_ledger_returns_none_when_never_saved(store: CaseStore) -> None:
    case = await store.create_case(application_number="PAN-661190", resident_session="s1")
    assert await store.load_ledger(case.case_id) is None


def test_snapshot_and_restore_ledger_are_pure_round_trip() -> None:
    ledger = Ledger(ceiling_usd=2.0)
    ledger.record(
        stage="clerk",
        model="gemma-4-26b-a4b-it-maas",
        usage=TokenUsage(prompt_tokens=100, output_tokens=50, thinking_tokens=0),
    )

    snapshot = snapshot_ledger(ledger)
    restored = restore_ledger(snapshot)

    assert snapshot == LedgerSnapshot(
        ceiling_usd=2.0,
        calls=(
            LedgerCallSnapshot(
                stage="clerk",
                model="gemma-4-26b-a4b-it-maas",
                prompt_tokens=100,
                output_tokens=50,
                thinking_tokens=0,
            ),
        ),
    )
    assert restored.total_cost_usd == pytest.approx(ledger.total_cost_usd)


# --- heartbeats ----------------------------------------------------------------------


async def test_heartbeat_records_latest_ping_per_stage(store: CaseStore) -> None:
    case = await store.create_case(application_number="PAN-661190", resident_session="s1")

    await store.heartbeat(case.case_id, "bench")
    second = await store.heartbeat(case.case_id, "bench")

    heartbeats = await store.list_heartbeats(case.case_id)
    assert heartbeats["bench"] == second  # only the latest ping survives


async def test_heartbeat_on_unknown_case_raises(store: CaseStore) -> None:
    with pytest.raises(CaseNotFoundError):
        await store.heartbeat("no-such-case", "bench")


async def test_list_heartbeats_for_unknown_case_is_empty(store: CaseStore) -> None:
    assert await store.list_heartbeats("no-such-case") == {}


# --- resume semantics --------------------------------------------------------------


async def test_resume_case_for_unknown_case_is_empty(store: CaseStore) -> None:
    resumed = await resume_case(store, "no-such-case")

    assert resumed.case is None
    assert resumed.grounds == {}
    assert resumed.events == ()
    assert resumed.ledger is None
    assert resumed.breakers == {}
    assert resumed.heartbeats == {}


async def test_resume_case_reconstructs_full_state(store: CaseStore) -> None:
    case = await store.create_case(application_number="PAN-661190", resident_session="s1")
    await store.propose_ground(case.case_id, "height-exceedance", claim="claim")
    await store.transition_ground(case.case_id, "height-exceedance", GroundStatus.UNDER_REVIEW)
    breaker = CircuitBreaker(name="bench")
    await store.save_breaker(case.case_id, breaker)
    ledger = Ledger()
    ledger.record(
        stage="bench",
        model="gemini-3.7-flash",
        usage=TokenUsage(prompt_tokens=10, output_tokens=10),
    )
    await store.save_ledger(case.case_id, ledger)
    await store.heartbeat(case.case_id, "bench")

    resumed = await resume_case(store, case.case_id)

    assert resumed.case == case
    assert resumed.grounds["height-exceedance"].status is GroundStatus.UNDER_REVIEW
    assert len(resumed.events) >= 3  # case created, ground proposed, status changed
    assert resumed.ledger is not None
    assert resumed.ledger.total_cost_usd == pytest.approx(ledger.total_cost_usd)
    assert "bench" in resumed.breakers
    assert "bench" in resumed.heartbeats


async def test_resuming_and_replaying_the_same_run_is_idempotent(store: CaseStore) -> None:
    """The central promise: interrupt a run after partial progress, then
    replay the exact same sequence of calls from scratch. No duplicates."""

    async def run_case_setup() -> str:
        case = await store.create_case(application_number="PAN-661190", resident_session="s1")
        await store.propose_ground(case.case_id, "height-exceedance", claim="claim")
        await store.transition_ground(case.case_id, "height-exceedance", GroundStatus.UNDER_REVIEW)
        return case.case_id

    case_id = await run_case_setup()
    first_grounds = await store.list_grounds(case_id)
    first_events = await store.list_events(case_id)

    # Simulate a crash-and-restart: replay the identical operation sequence.
    replayed_case_id = await run_case_setup()

    assert replayed_case_id == case_id
    second_grounds = await store.list_grounds(case_id)
    second_events = await store.list_events(case_id)
    assert second_grounds == first_grounds
    assert second_events == first_events
    assert len(second_grounds) == 1
