"""Durable case state: the Firestore-backed case store and its in-memory twin.

This module defines the **case-store port** (:class:`CaseStore`, a narrow
structural `Protocol`) plus two implementations that satisfy it identically:

* :class:`InMemoryCaseStore` — a dict-backed test double, used throughout
  the test suite and safe for fully offline use.
* :class:`FirestoreCaseStore` — a thin adapter over
  ``google.cloud.firestore.AsyncClient``. It holds no business logic beyond
  translating port calls into document reads/writes: lifecycle validation
  and idempotency (deterministic ids, "already applied" checks) live in
  module-level pure helpers shared by both implementations, so the two
  backends are guaranteed to agree on behaviour.

Firestore layout (all under the ``cases`` root collection)::

    cases/{case_id}                                  -> CaseRecord
    cases/{case_id}/grounds/{ground_id}               -> GroundRecord
    cases/{case_id}/events/{event_id}                 -> CaseEvent
    cases/{case_id}/breakers/{stage}                  -> BreakerSnapshot
    cases/{case_id}/ledger/snapshot                   -> LedgerSnapshot
    cases/{case_id}/heartbeats/{stage}                -> Heartbeat

``case_id`` is deterministic (a hash of the application number and the
resident session, see :func:`case_id_for`), and every mutating operation is
idempotent against its own natural key (case id, ground id, or a
caller/derived event id). Together this gives **resume semantics**: a run
interrupted mid-case can call the exact same sequence of operations again
and land in the same state, with no duplicate grounds or events.

Configuration note: :data:`setback.config.GCP_PROJECT` now defaults to
``vexcourt-agent`` — this hackathon's actual GCP project id (its Cloud
Console display name is "Setback", but the project id itself was never
renamed to match). Fixed at the integration checkpoint that merged this
module; ``SETBACK_GCP_PROJECT`` still overrides it for any environment that
needs a different project.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from google.cloud import firestore

from setback.config import GCP_PROJECT
from setback.evidence.dossier import ProvenanceGrade
from setback.models.client import TokenUsage
from setback.state.breakers import CircuitBreaker, CircuitState
from setback.state.ledger import Ledger

# --- Domain: grounds -------------------------------------------------------


class GroundStatus(StrEnum):
    """A ground's position in its review lifecycle.

    Allowed forward transitions: ``proposed -> under_review -> {supported,
    refused, flagged}``. The three post-review states are terminal.
    Re-applying the ground's current status is a no-op, not an error, so
    that resuming an interrupted run never fails on work already done.
    """

    PROPOSED = "proposed"
    UNDER_REVIEW = "under_review"
    SUPPORTED = "supported"
    REFUSED = "refused"
    FLAGGED = "flagged"


_ALLOWED_GROUND_TRANSITIONS: dict[GroundStatus, frozenset[GroundStatus]] = {
    GroundStatus.PROPOSED: frozenset({GroundStatus.UNDER_REVIEW}),
    GroundStatus.UNDER_REVIEW: frozenset(
        {GroundStatus.SUPPORTED, GroundStatus.REFUSED, GroundStatus.FLAGGED}
    ),
    GroundStatus.SUPPORTED: frozenset(),
    GroundStatus.REFUSED: frozenset(),
    GroundStatus.FLAGGED: frozenset(),
}


class CaseStoreError(Exception):
    """Base class for case-store errors."""


class CaseNotFoundError(CaseStoreError):
    """Raised by a mutating call against a case id that has never been created."""


class GroundNotFoundError(CaseStoreError):
    """Raised by a mutating call against a ground id that has never been proposed."""


class InvalidGroundTransitionError(CaseStoreError):
    """Raised when a ground status change is not on its allowed lifecycle path."""


def _validate_ground_transition(
    ground_id: str, current: GroundStatus, target: GroundStatus
) -> None:
    """Raise :class:`InvalidGroundTransitionError` unless `target` is reachable
    from `current` (repeating the current status is always allowed)."""
    if current == target:
        return
    if target not in _ALLOWED_GROUND_TRANSITIONS[current]:
        raise InvalidGroundTransitionError(
            f"ground {ground_id!r} cannot transition from {current.value!r} to {target.value!r}"
        )


@dataclass(frozen=True)
class GroundEvidenceAnchor:
    """A single piece of evidence anchoring a ground to its source material."""

    source_doc: str
    """Identifier of the source document (e.g. an exhibited PDF's filename or id)."""

    page: int
    """1-indexed page number within `source_doc`."""

    bbox: tuple[float, float, float, float]
    """Bounding box on that page, as ``(x0, y0, x1, y1)``."""

    provenance_grade: ProvenanceGrade
    """How directly this anchor evidences the claim (see :class:`ProvenanceGrade`)."""


@dataclass(frozen=True)
class GroundRecord:
    """A persisted objection ground and its current review status."""

    ground_id: str
    case_id: str
    claim: str
    status: GroundStatus
    anchors: tuple[GroundEvidenceAnchor, ...]
    created_at: datetime
    updated_at: datetime


# --- Domain: cases, events, heartbeats --------------------------------------


@dataclass(frozen=True)
class CaseRecord:
    """A persisted case, keyed by a deterministic id (see :func:`case_id_for`)."""

    case_id: str
    application_number: str
    resident_session: str
    created_at: datetime


class EventType(StrEnum):
    """Event types emitted automatically by the built-in lifecycle operations.

    `append_event` also accepts plain `str` event types from callers outside
    this module (e.g. interview or ingestion stages logging their own
    domain events) — this enum only names the ones this module itself emits.
    """

    CASE_CREATED = "case_created"
    GROUND_PROPOSED = "ground_proposed"
    GROUND_STATUS_CHANGED = "ground_status_changed"
    EVIDENCE_ANCHORED = "evidence_anchored"


@dataclass(frozen=True)
class CaseEvent:
    """One entry in a case's append-only event log.

    `event_id` is the idempotency key: appending an event whose id already
    exists for the case is a no-op that returns the existing entry
    unchanged, so replaying a run's operations never duplicates it.
    """

    event_id: str
    case_id: str
    event_type: str
    payload: Mapping[str, Any]
    sequence: int
    recorded_at: datetime


@dataclass(frozen=True)
class Heartbeat:
    """The most recent liveness ping for one stage of one case's run."""

    case_id: str
    stage: str
    at: datetime


def _case_created_event_id(case_id: str) -> str:
    return f"case-created:{case_id}"


def _ground_proposed_event_id(ground_id: str) -> str:
    return f"ground-proposed:{ground_id}"


def _ground_status_event_id(ground_id: str, status: GroundStatus) -> str:
    return f"ground-status:{ground_id}:{status.value}"


def _evidence_anchor_event_id(ground_id: str, anchor: GroundEvidenceAnchor) -> str:
    bbox_key = ",".join(f"{v:.6f}" for v in anchor.bbox)
    return f"evidence-anchored:{ground_id}:{anchor.source_doc}:{anchor.page}:{bbox_key}"


def case_id_for(application_number: str, resident_session: str) -> str:
    """A deterministic, collision-resistant case id for this (application,
    resident session) pair. Calling this twice with the same inputs always
    yields the same id, which is what makes `create_case` idempotent and a
    re-run of the same demo case resume rather than fork a new case."""
    digest = hashlib.sha256(f"{application_number}:{resident_session}".encode()).hexdigest()
    return digest[:32]


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --- Breaker and ledger persistence -----------------------------------------


@dataclass(frozen=True)
class BreakerSnapshot:
    """A serializable snapshot of one :class:`CircuitBreaker`'s state."""

    stage: str
    state: CircuitState
    failure_threshold: int
    reset_timeout_seconds: float


def snapshot_breaker(breaker: CircuitBreaker) -> BreakerSnapshot:
    """Capture `breaker`'s current state via its public API only."""
    return BreakerSnapshot(
        stage=breaker.name,
        state=breaker.state,
        failure_threshold=breaker.failure_threshold,
        reset_timeout_seconds=breaker.reset_timeout_seconds,
    )


def restore_breaker(
    snapshot: BreakerSnapshot, *, clock: Callable[[], float] = time.monotonic
) -> CircuitBreaker:
    """Reconstruct a breaker from `snapshot`, driven entirely through
    :class:`CircuitBreaker`'s public API (its failure/cooldown bookkeeping is
    private, by design, so this module never reaches into it directly).

    A breaker restored as OPEN or HALF_OPEN starts a fresh cooldown window
    from the moment of restore rather than resuming the exact elapsed time
    it had before the interruption — a deliberately conservative choice: an
    unnecessarily-degraded stage is a smaller failure than an under-cautious
    probe against a primary that may still be failing.
    """
    breaker = CircuitBreaker(
        name=snapshot.stage,
        failure_threshold=snapshot.failure_threshold,
        reset_timeout_seconds=snapshot.reset_timeout_seconds,
        clock=clock,
    )
    if snapshot.state is not CircuitState.CLOSED:
        for _ in range(snapshot.failure_threshold):
            breaker.record_failure()
    return breaker


@dataclass(frozen=True)
class LedgerCallSnapshot:
    """One persisted :class:`~setback.state.ledger.CallRecord`, decomposed
    into plain fields so it can be re-priced on restore rather than trusting
    a stored cost figure that could drift from the live pricing table."""

    stage: str
    model: str
    prompt_tokens: int
    output_tokens: int
    thinking_tokens: int


@dataclass(frozen=True)
class LedgerSnapshot:
    """A serializable snapshot of a :class:`Ledger`'s ceiling and call history."""

    ceiling_usd: float
    calls: tuple[LedgerCallSnapshot, ...]


def snapshot_ledger(ledger: Ledger) -> LedgerSnapshot:
    """Capture `ledger`'s state via its public `records`/`ceiling_usd` API."""
    calls = tuple(
        LedgerCallSnapshot(
            stage=r.stage,
            model=r.model,
            prompt_tokens=r.usage.prompt_tokens,
            output_tokens=r.usage.output_tokens,
            thinking_tokens=r.usage.thinking_tokens,
        )
        for r in ledger.records
    )
    return LedgerSnapshot(ceiling_usd=ledger.ceiling_usd, calls=calls)


def restore_ledger(snapshot: LedgerSnapshot) -> Ledger:
    """Reconstruct a ledger from `snapshot` by replaying each call through
    :meth:`Ledger.record` (its public API), re-pricing from token counts
    rather than trusting a stored cost figure."""
    ledger = Ledger(ceiling_usd=snapshot.ceiling_usd)
    for call in snapshot.calls:
        usage = TokenUsage(
            prompt_tokens=call.prompt_tokens,
            output_tokens=call.output_tokens,
            thinking_tokens=call.thinking_tokens,
        )
        ledger.record(stage=call.stage, model=call.model, usage=usage)
    return ledger


# --- The port ----------------------------------------------------------------


class CaseStore(Protocol):
    """The narrow repository interface both `InMemoryCaseStore` (the test
    double) and `FirestoreCaseStore` (the production adapter) satisfy.

    Every mutating method is idempotent against its natural key: calling it
    again with the same identifying arguments returns the existing record
    unchanged rather than erroring or duplicating state. This is what makes
    a resumed run safe to replay from its start.
    """

    async def create_case(
        self, *, application_number: str, resident_session: str
    ) -> CaseRecord: ...

    async def get_case(self, case_id: str) -> CaseRecord | None: ...

    async def propose_ground(
        self,
        case_id: str,
        ground_id: str,
        *,
        claim: str,
        anchors: Sequence[GroundEvidenceAnchor] = (),
    ) -> GroundRecord: ...

    async def transition_ground(
        self, case_id: str, ground_id: str, status: GroundStatus
    ) -> GroundRecord: ...

    async def add_evidence_anchor(
        self, case_id: str, ground_id: str, anchor: GroundEvidenceAnchor
    ) -> GroundRecord: ...

    async def get_ground(self, case_id: str, ground_id: str) -> GroundRecord | None: ...

    async def list_grounds(self, case_id: str) -> tuple[GroundRecord, ...]: ...

    async def append_event(
        self, case_id: str, event_id: str, event_type: str, *, payload: Mapping[str, Any]
    ) -> CaseEvent: ...

    async def list_events(self, case_id: str) -> tuple[CaseEvent, ...]: ...

    async def save_breaker(self, case_id: str, breaker: CircuitBreaker) -> None: ...

    async def load_breakers(self, case_id: str) -> Mapping[str, CircuitBreaker]: ...

    async def save_ledger(self, case_id: str, ledger: Ledger) -> None: ...

    async def load_ledger(self, case_id: str) -> Ledger | None: ...

    async def heartbeat(self, case_id: str, stage: str) -> Heartbeat: ...

    async def list_heartbeats(self, case_id: str) -> Mapping[str, Heartbeat]: ...


@dataclass(frozen=True)
class ResumeState:
    """Everything a caller needs to continue an interrupted case run rather
    than start it over: which grounds and events already exist, the live
    ledger and breakers restored from their last snapshot, and the last
    heartbeat seen per stage."""

    case: CaseRecord | None
    grounds: Mapping[str, GroundRecord]
    events: tuple[CaseEvent, ...]
    ledger: Ledger | None
    breakers: Mapping[str, CircuitBreaker]
    heartbeats: Mapping[str, Heartbeat]


async def resume_case(store: CaseStore, case_id: str) -> ResumeState:
    """Assemble the full resumable state for `case_id` from `store`.

    Safe to call for a case id that does not exist: returns an empty
    `ResumeState` (`case is None`, empty collections) rather than raising,
    so a caller can use it uniformly for "first run" and "resumed run".
    """
    case = await store.get_case(case_id)
    grounds = await store.list_grounds(case_id)
    events = await store.list_events(case_id)
    ledger = await store.load_ledger(case_id)
    breakers = await store.load_breakers(case_id)
    heartbeats = await store.list_heartbeats(case_id)
    return ResumeState(
        case=case,
        grounds={g.ground_id: g for g in grounds},
        events=events,
        ledger=ledger,
        breakers=breakers,
        heartbeats=heartbeats,
    )


# --- InMemoryCaseStore: the test double --------------------------------------


@dataclass
class _CaseData:
    case: CaseRecord
    grounds: dict[str, GroundRecord] = field(default_factory=dict)
    events: dict[str, CaseEvent] = field(default_factory=dict)
    breaker_snapshots: dict[str, BreakerSnapshot] = field(default_factory=dict)
    ledger_snapshot: LedgerSnapshot | None = None
    heartbeats: dict[str, Heartbeat] = field(default_factory=dict)


class InMemoryCaseStore:
    """A dict-backed :class:`CaseStore` test double: no I/O, fully
    deterministic given an injected `clock`, safe for offline tests."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or _utcnow
        self._cases: dict[str, _CaseData] = {}

    def _get(self, case_id: str) -> _CaseData | None:
        return self._cases.get(case_id)

    def _require(self, case_id: str) -> _CaseData:
        data = self._cases.get(case_id)
        if data is None:
            raise CaseNotFoundError(case_id)
        return data

    async def create_case(self, *, application_number: str, resident_session: str) -> CaseRecord:
        case_id = case_id_for(application_number, resident_session)
        existing = self._get(case_id)
        if existing is not None:
            return existing.case
        record = CaseRecord(
            case_id=case_id,
            application_number=application_number,
            resident_session=resident_session,
            created_at=self._clock(),
        )
        self._cases[case_id] = _CaseData(case=record)
        await self.append_event(
            case_id,
            _case_created_event_id(case_id),
            EventType.CASE_CREATED,
            payload={"application_number": application_number},
        )
        return record

    async def get_case(self, case_id: str) -> CaseRecord | None:
        data = self._get(case_id)
        return data.case if data is not None else None

    async def propose_ground(
        self,
        case_id: str,
        ground_id: str,
        *,
        claim: str,
        anchors: Sequence[GroundEvidenceAnchor] = (),
    ) -> GroundRecord:
        data = self._require(case_id)
        existing = data.grounds.get(ground_id)
        if existing is not None:
            return existing
        now = self._clock()
        record = GroundRecord(
            ground_id=ground_id,
            case_id=case_id,
            claim=claim,
            status=GroundStatus.PROPOSED,
            anchors=tuple(anchors),
            created_at=now,
            updated_at=now,
        )
        data.grounds[ground_id] = record
        await self.append_event(
            case_id,
            _ground_proposed_event_id(ground_id),
            EventType.GROUND_PROPOSED,
            payload={"ground_id": ground_id, "claim": claim},
        )
        return record

    async def transition_ground(
        self, case_id: str, ground_id: str, status: GroundStatus
    ) -> GroundRecord:
        data = self._require(case_id)
        current = data.grounds.get(ground_id)
        if current is None:
            raise GroundNotFoundError(ground_id)
        if current.status == status:
            return current
        _validate_ground_transition(ground_id, current.status, status)
        updated = replace(current, status=status, updated_at=self._clock())
        data.grounds[ground_id] = updated
        await self.append_event(
            case_id,
            _ground_status_event_id(ground_id, status),
            EventType.GROUND_STATUS_CHANGED,
            payload={"ground_id": ground_id, "from": current.status.value, "to": status.value},
        )
        return updated

    async def add_evidence_anchor(
        self, case_id: str, ground_id: str, anchor: GroundEvidenceAnchor
    ) -> GroundRecord:
        data = self._require(case_id)
        current = data.grounds.get(ground_id)
        if current is None:
            raise GroundNotFoundError(ground_id)
        if anchor in current.anchors:
            return current
        updated = replace(current, anchors=(*current.anchors, anchor), updated_at=self._clock())
        data.grounds[ground_id] = updated
        await self.append_event(
            case_id,
            _evidence_anchor_event_id(ground_id, anchor),
            EventType.EVIDENCE_ANCHORED,
            payload={"ground_id": ground_id, "source_doc": anchor.source_doc, "page": anchor.page},
        )
        return updated

    async def get_ground(self, case_id: str, ground_id: str) -> GroundRecord | None:
        data = self._get(case_id)
        return data.grounds.get(ground_id) if data is not None else None

    async def list_grounds(self, case_id: str) -> tuple[GroundRecord, ...]:
        data = self._get(case_id)
        return tuple(data.grounds.values()) if data is not None else ()

    async def append_event(
        self, case_id: str, event_id: str, event_type: str, *, payload: Mapping[str, Any]
    ) -> CaseEvent:
        data = self._require(case_id)
        existing = data.events.get(event_id)
        if existing is not None:
            return existing
        event = CaseEvent(
            event_id=event_id,
            case_id=case_id,
            event_type=str(event_type),
            payload=dict(payload),
            sequence=len(data.events),
            recorded_at=self._clock(),
        )
        data.events[event_id] = event
        return event

    async def list_events(self, case_id: str) -> tuple[CaseEvent, ...]:
        data = self._get(case_id)
        return tuple(data.events.values()) if data is not None else ()

    async def save_breaker(self, case_id: str, breaker: CircuitBreaker) -> None:
        data = self._require(case_id)
        data.breaker_snapshots[breaker.name] = snapshot_breaker(breaker)

    async def load_breakers(self, case_id: str) -> Mapping[str, CircuitBreaker]:
        data = self._get(case_id)
        if data is None:
            return {}
        return {stage: restore_breaker(snap) for stage, snap in data.breaker_snapshots.items()}

    async def save_ledger(self, case_id: str, ledger: Ledger) -> None:
        data = self._require(case_id)
        data.ledger_snapshot = snapshot_ledger(ledger)

    async def load_ledger(self, case_id: str) -> Ledger | None:
        data = self._get(case_id)
        if data is None or data.ledger_snapshot is None:
            return None
        return restore_ledger(data.ledger_snapshot)

    async def heartbeat(self, case_id: str, stage: str) -> Heartbeat:
        data = self._require(case_id)
        beat = Heartbeat(case_id=case_id, stage=stage, at=self._clock())
        data.heartbeats[stage] = beat
        return beat

    async def list_heartbeats(self, case_id: str) -> Mapping[str, Heartbeat]:
        data = self._get(case_id)
        return dict(data.heartbeats) if data is not None else {}


# --- FirestoreCaseStore: the thin production adapter ------------------------


def get_firestore_client() -> firestore.AsyncClient:
    """Build the default Firestore async client for `setback.config.GCP_PROJECT`.

    Uses Application Default Credentials and the database's default
    ("(default)") database id. Never called by the test suite, which always
    injects a fake or uses `InMemoryCaseStore` instead. See the module
    docstring for the `SETBACK_GCP_PROJECT` note.
    """
    return firestore.AsyncClient(project=GCP_PROJECT)


def _case_to_dict(case: CaseRecord) -> dict[str, Any]:
    return {
        "application_number": case.application_number,
        "resident_session": case.resident_session,
        "created_at": case.created_at,
    }


def _case_from_dict(case_id: str, data: Mapping[str, Any]) -> CaseRecord:
    return CaseRecord(
        case_id=case_id,
        application_number=data["application_number"],
        resident_session=data["resident_session"],
        created_at=data["created_at"],
    )


def _anchor_to_dict(anchor: GroundEvidenceAnchor) -> dict[str, Any]:
    return {
        "source_doc": anchor.source_doc,
        "page": anchor.page,
        "bbox": list(anchor.bbox),
        "provenance_grade": anchor.provenance_grade.value,
    }


def _anchor_from_dict(data: Mapping[str, Any]) -> GroundEvidenceAnchor:
    x0, y0, x1, y1 = data["bbox"]
    return GroundEvidenceAnchor(
        source_doc=data["source_doc"],
        page=data["page"],
        bbox=(x0, y0, x1, y1),
        provenance_grade=ProvenanceGrade(data["provenance_grade"]),
    )


def _ground_to_dict(ground: GroundRecord) -> dict[str, Any]:
    return {
        "claim": ground.claim,
        "status": ground.status.value,
        "anchors": [_anchor_to_dict(a) for a in ground.anchors],
        "created_at": ground.created_at,
        "updated_at": ground.updated_at,
    }


def _ground_from_dict(case_id: str, ground_id: str, data: Mapping[str, Any]) -> GroundRecord:
    return GroundRecord(
        ground_id=ground_id,
        case_id=case_id,
        claim=data["claim"],
        status=GroundStatus(data["status"]),
        anchors=tuple(_anchor_from_dict(a) for a in data["anchors"]),
        created_at=data["created_at"],
        updated_at=data["updated_at"],
    )


def _event_to_dict(event: CaseEvent) -> dict[str, Any]:
    return {
        "event_type": event.event_type,
        "payload": dict(event.payload),
        "sequence": event.sequence,
        "recorded_at": event.recorded_at,
    }


def _event_from_dict(case_id: str, event_id: str, data: Mapping[str, Any]) -> CaseEvent:
    return CaseEvent(
        event_id=event_id,
        case_id=case_id,
        event_type=data["event_type"],
        payload=dict(data["payload"]),
        sequence=data["sequence"],
        recorded_at=data["recorded_at"],
    )


def _breaker_to_dict(snapshot: BreakerSnapshot) -> dict[str, Any]:
    return {
        "state": snapshot.state.value,
        "failure_threshold": snapshot.failure_threshold,
        "reset_timeout_seconds": snapshot.reset_timeout_seconds,
    }


def _breaker_from_dict(stage: str, data: Mapping[str, Any]) -> BreakerSnapshot:
    return BreakerSnapshot(
        stage=stage,
        state=CircuitState(data["state"]),
        failure_threshold=data["failure_threshold"],
        reset_timeout_seconds=data["reset_timeout_seconds"],
    )


def _ledger_to_dict(snapshot: LedgerSnapshot) -> dict[str, Any]:
    return {
        "ceiling_usd": snapshot.ceiling_usd,
        "calls": [
            {
                "stage": c.stage,
                "model": c.model,
                "prompt_tokens": c.prompt_tokens,
                "output_tokens": c.output_tokens,
                "thinking_tokens": c.thinking_tokens,
            }
            for c in snapshot.calls
        ],
    }


def _ledger_from_dict(data: Mapping[str, Any]) -> LedgerSnapshot:
    calls = tuple(
        LedgerCallSnapshot(
            stage=c["stage"],
            model=c["model"],
            prompt_tokens=c["prompt_tokens"],
            output_tokens=c["output_tokens"],
            thinking_tokens=c["thinking_tokens"],
        )
        for c in data["calls"]
    )
    return LedgerSnapshot(ceiling_usd=data["ceiling_usd"], calls=calls)


def _heartbeat_to_dict(beat: Heartbeat) -> dict[str, Any]:
    return {"at": beat.at}


def _heartbeat_from_dict(case_id: str, stage: str, data: Mapping[str, Any]) -> Heartbeat:
    return Heartbeat(case_id=case_id, stage=stage, at=data["at"])


class FirestoreCaseStore:
    """The production :class:`CaseStore`: a thin adapter over
    `google.cloud.firestore.AsyncClient`. See the module docstring for the
    document layout. Holds no lifecycle or idempotency logic of its own —
    it reuses the exact same pure helpers as `InMemoryCaseStore`.
    """

    def __init__(
        self,
        client: firestore.AsyncClient | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """`client` defaults to `get_firestore_client()`, which requires ADC
        and is never exercised by the test suite — tests always inject a
        fake client double instead."""
        self._client = client if client is not None else get_firestore_client()
        self._clock = clock or _utcnow

    def _case_ref(self, case_id: str) -> firestore.AsyncDocumentReference:
        return self._client.collection("cases").document(case_id)

    async def _require_case(self, case_id: str) -> None:
        snapshot = await self._case_ref(case_id).get()
        if not snapshot.exists:
            raise CaseNotFoundError(case_id)

    async def create_case(self, *, application_number: str, resident_session: str) -> CaseRecord:
        case_id = case_id_for(application_number, resident_session)
        ref = self._case_ref(case_id)
        snapshot = await ref.get()
        if snapshot.exists:
            data = snapshot.to_dict()
            assert data is not None
            return _case_from_dict(case_id, data)
        record = CaseRecord(
            case_id=case_id,
            application_number=application_number,
            resident_session=resident_session,
            created_at=self._clock(),
        )
        await ref.set(_case_to_dict(record))
        await self.append_event(
            case_id,
            _case_created_event_id(case_id),
            EventType.CASE_CREATED,
            payload={"application_number": application_number},
        )
        return record

    async def get_case(self, case_id: str) -> CaseRecord | None:
        snapshot = await self._case_ref(case_id).get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict()
        assert data is not None
        return _case_from_dict(case_id, data)

    async def propose_ground(
        self,
        case_id: str,
        ground_id: str,
        *,
        claim: str,
        anchors: Sequence[GroundEvidenceAnchor] = (),
    ) -> GroundRecord:
        await self._require_case(case_id)
        ground_ref = self._case_ref(case_id).collection("grounds").document(ground_id)
        snapshot = await ground_ref.get()
        if snapshot.exists:
            data = snapshot.to_dict()
            assert data is not None
            return _ground_from_dict(case_id, ground_id, data)
        now = self._clock()
        record = GroundRecord(
            ground_id=ground_id,
            case_id=case_id,
            claim=claim,
            status=GroundStatus.PROPOSED,
            anchors=tuple(anchors),
            created_at=now,
            updated_at=now,
        )
        await ground_ref.set(_ground_to_dict(record))
        await self.append_event(
            case_id,
            _ground_proposed_event_id(ground_id),
            EventType.GROUND_PROPOSED,
            payload={"ground_id": ground_id, "claim": claim},
        )
        return record

    async def transition_ground(
        self, case_id: str, ground_id: str, status: GroundStatus
    ) -> GroundRecord:
        await self._require_case(case_id)
        ground_ref = self._case_ref(case_id).collection("grounds").document(ground_id)
        snapshot = await ground_ref.get()
        if not snapshot.exists:
            raise GroundNotFoundError(ground_id)
        data = snapshot.to_dict()
        assert data is not None
        current = _ground_from_dict(case_id, ground_id, data)
        if current.status == status:
            return current
        _validate_ground_transition(ground_id, current.status, status)
        updated = replace(current, status=status, updated_at=self._clock())
        await ground_ref.set(_ground_to_dict(updated))
        await self.append_event(
            case_id,
            _ground_status_event_id(ground_id, status),
            EventType.GROUND_STATUS_CHANGED,
            payload={"ground_id": ground_id, "from": current.status.value, "to": status.value},
        )
        return updated

    async def add_evidence_anchor(
        self, case_id: str, ground_id: str, anchor: GroundEvidenceAnchor
    ) -> GroundRecord:
        await self._require_case(case_id)
        ground_ref = self._case_ref(case_id).collection("grounds").document(ground_id)
        snapshot = await ground_ref.get()
        if not snapshot.exists:
            raise GroundNotFoundError(ground_id)
        data = snapshot.to_dict()
        assert data is not None
        current = _ground_from_dict(case_id, ground_id, data)
        if anchor in current.anchors:
            return current
        updated = replace(current, anchors=(*current.anchors, anchor), updated_at=self._clock())
        await ground_ref.set(_ground_to_dict(updated))
        await self.append_event(
            case_id,
            _evidence_anchor_event_id(ground_id, anchor),
            EventType.EVIDENCE_ANCHORED,
            payload={"ground_id": ground_id, "source_doc": anchor.source_doc, "page": anchor.page},
        )
        return updated

    async def get_ground(self, case_id: str, ground_id: str) -> GroundRecord | None:
        snapshot = await self._case_ref(case_id).collection("grounds").document(ground_id).get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict()
        assert data is not None
        return _ground_from_dict(case_id, ground_id, data)

    async def list_grounds(self, case_id: str) -> tuple[GroundRecord, ...]:
        grounds: list[GroundRecord] = []
        async for snapshot in self._case_ref(case_id).collection("grounds").stream():
            data = snapshot.to_dict()
            if data is not None:
                grounds.append(_ground_from_dict(case_id, snapshot.id, data))
        return tuple(grounds)

    async def append_event(
        self, case_id: str, event_id: str, event_type: str, *, payload: Mapping[str, Any]
    ) -> CaseEvent:
        await self._require_case(case_id)
        events_ref = self._case_ref(case_id).collection("events")
        event_ref = events_ref.document(event_id)
        snapshot = await event_ref.get()
        if snapshot.exists:
            data = snapshot.to_dict()
            assert data is not None
            return _event_from_dict(case_id, event_id, data)
        # Sequence numbers are derived by counting existing events. Fine at
        # this project's per-case scale (a handful of grounds/events); a
        # higher-volume system would need a dedicated counter document to
        # avoid this O(n)-per-append scan and the race window between the
        # count and the write below (acceptable here: one job writes to a
        # given case at a time).
        sequence = 0
        async for _existing in events_ref.stream():
            sequence += 1
        event = CaseEvent(
            event_id=event_id,
            case_id=case_id,
            event_type=str(event_type),
            payload=dict(payload),
            sequence=sequence,
            recorded_at=self._clock(),
        )
        await event_ref.set(_event_to_dict(event))
        return event

    async def list_events(self, case_id: str) -> tuple[CaseEvent, ...]:
        events: list[CaseEvent] = []
        async for snapshot in self._case_ref(case_id).collection("events").stream():
            data = snapshot.to_dict()
            if data is not None:
                events.append(_event_from_dict(case_id, snapshot.id, data))
        return tuple(sorted(events, key=lambda e: e.sequence))

    async def save_breaker(self, case_id: str, breaker: CircuitBreaker) -> None:
        await self._require_case(case_id)
        snapshot = snapshot_breaker(breaker)
        ref = self._case_ref(case_id).collection("breakers").document(breaker.name)
        await ref.set(_breaker_to_dict(snapshot))

    async def load_breakers(self, case_id: str) -> Mapping[str, CircuitBreaker]:
        breakers: dict[str, CircuitBreaker] = {}
        async for snapshot in self._case_ref(case_id).collection("breakers").stream():
            data = snapshot.to_dict()
            if data is not None:
                breakers[snapshot.id] = restore_breaker(_breaker_from_dict(snapshot.id, data))
        return breakers

    async def save_ledger(self, case_id: str, ledger: Ledger) -> None:
        await self._require_case(case_id)
        snapshot = snapshot_ledger(ledger)
        ref = self._case_ref(case_id).collection("ledger").document("snapshot")
        await ref.set(_ledger_to_dict(snapshot))

    async def load_ledger(self, case_id: str) -> Ledger | None:
        snapshot = await self._case_ref(case_id).collection("ledger").document("snapshot").get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict()
        assert data is not None
        return restore_ledger(_ledger_from_dict(data))

    async def heartbeat(self, case_id: str, stage: str) -> Heartbeat:
        await self._require_case(case_id)
        beat = Heartbeat(case_id=case_id, stage=stage, at=self._clock())
        ref = self._case_ref(case_id).collection("heartbeats").document(stage)
        await ref.set(_heartbeat_to_dict(beat))
        return beat

    async def list_heartbeats(self, case_id: str) -> Mapping[str, Heartbeat]:
        heartbeats: dict[str, Heartbeat] = {}
        async for snapshot in self._case_ref(case_id).collection("heartbeats").stream():
            data = snapshot.to_dict()
            if data is not None:
                heartbeats[snapshot.id] = _heartbeat_from_dict(case_id, snapshot.id, data)
        return heartbeats


if TYPE_CHECKING:
    # Static-only conformance check: both stores must satisfy the port.
    def _conforms_in_memory() -> CaseStore:
        return InMemoryCaseStore()

    def _conforms_firestore(client: firestore.AsyncClient) -> CaseStore:
        return FirestoreCaseStore(client)
