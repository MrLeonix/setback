"""Tests for setback.state.guard_store: the public-abuse-guard's durable
state -- per-actor daily counters (`GuardCounterStore`) and the global
public-spend aggregate (`GuardTotalsStore`), each with an in-memory double
and a Firestore adapter exercised against a minimal fake client (mirroring
`tests/state/test_firestore.py`'s own `_FakeFirestore` pattern).

Fully offline: no live Firestore, no network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from setback.state.guard_store import (
    FirestoreGuardCounterStore,
    FirestoreGuardTotalsStore,
    GuardCounterStore,
    GuardTotals,
    GuardTotalsStore,
    InMemoryGuardCounterStore,
    InMemoryGuardTotalsStore,
)

# --- a tiny fake of the AsyncClient surface these stores use ----------------


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
    def __init__(self) -> None:
        self.docs: dict[tuple[str, ...], dict[str, Any]] = {}

    def collection(self, name: str) -> _FakeCollectionRef:
        return _FakeCollectionRef(self, (name,))


@dataclass
class _CounterStoreUnderTest:
    store: GuardCounterStore
    label: str
    fake: _FakeFirestore | None = None


def _memory_counter_store() -> _CounterStoreUnderTest:
    return _CounterStoreUnderTest(InMemoryGuardCounterStore(), "in-memory")


def _firestore_counter_store() -> _CounterStoreUnderTest:
    fake = _FakeFirestore()
    return _CounterStoreUnderTest(
        FirestoreGuardCounterStore(fake),  # type: ignore[arg-type]
        "firestore-fake",
        fake=fake,
    )


@pytest.fixture(params=[_memory_counter_store, _firestore_counter_store])
def counter_store_case(request: pytest.FixtureRequest) -> _CounterStoreUnderTest:
    return request.param()


@dataclass
class _TotalsStoreUnderTest:
    store: GuardTotalsStore
    label: str


def _memory_totals_store() -> _TotalsStoreUnderTest:
    return _TotalsStoreUnderTest(InMemoryGuardTotalsStore(), "in-memory")


def _firestore_totals_store() -> _TotalsStoreUnderTest:
    return _TotalsStoreUnderTest(
        FirestoreGuardTotalsStore(_FakeFirestore()),  # type: ignore[arg-type]
        "firestore-fake",
    )


@pytest.fixture(params=[_memory_totals_store, _firestore_totals_store])
def totals_store(request: pytest.FixtureRequest) -> GuardTotalsStore:
    return request.param().store


# --- GuardCounterStore: per-actor daily counters ----------------------------


async def test_try_increment_daily_allows_up_to_the_limit(
    counter_store_case: _CounterStoreUnderTest,
) -> None:
    store = counter_store_case.store
    day = date(2026, 8, 30)
    assert await store.try_increment_daily("case", "abc123", day=day, limit=2) is True
    assert await store.try_increment_daily("case", "abc123", day=day, limit=2) is True
    assert await store.try_increment_daily("case", "abc123", day=day, limit=2) is False


async def test_try_increment_daily_tracks_distinct_actors_independently(
    counter_store_case: _CounterStoreUnderTest,
) -> None:
    store = counter_store_case.store
    day = date(2026, 8, 30)
    assert await store.try_increment_daily("case", "actor-a", day=day, limit=1) is True
    assert await store.try_increment_daily("case", "actor-b", day=day, limit=1) is True
    assert await store.try_increment_daily("case", "actor-a", day=day, limit=1) is False


async def test_try_increment_daily_resets_on_a_new_day(
    counter_store_case: _CounterStoreUnderTest,
) -> None:
    store = counter_store_case.store
    day_one = date(2026, 8, 30)
    day_two = date(2026, 8, 31)
    assert await store.try_increment_daily("case", "actor-a", day=day_one, limit=1) is True
    assert await store.try_increment_daily("case", "actor-a", day=day_one, limit=1) is False
    # A new rolling day is a fresh bucket.
    assert await store.try_increment_daily("case", "actor-a", day=day_two, limit=1) is True


async def test_try_increment_daily_keys_distinct_prefixes_independently(
    counter_store_case: _CounterStoreUnderTest,
) -> None:
    """Different counter kinds (e.g. "case" vs some future prefix) sharing
    the same actor hash and day must not share a bucket."""
    store = counter_store_case.store
    day = date(2026, 8, 30)
    assert await store.try_increment_daily("case", "actor-a", day=day, limit=1) is True
    assert await store.try_increment_daily("other", "actor-a", day=day, limit=1) is True


async def test_firestore_counter_doc_id_never_contains_a_raw_looking_ip() -> None:
    """The stored Firestore doc id must be built from the caller-supplied
    hash, never a raw IP -- this test pins the doc id shape
    (`{prefix}_{hash}_{yyyymmdd}`) so a future change can't accidentally
    start keying on a raw address."""
    fake = _FakeFirestore()
    store = FirestoreGuardCounterStore(fake)  # type: ignore[arg-type]
    actor_hash = "deadbeef" * 8  # sha256-hex-shaped, not an IP
    await store.try_increment_daily("case", actor_hash, day=date(2026, 8, 30), limit=5)
    doc_ids = [key[-1] for key in fake.docs if key[0] == "guard_counters"]
    assert doc_ids == [f"case_{actor_hash}_20260830"]
    # No literal dotted-quad IP shape ever appears in a stored doc id.
    assert "." not in doc_ids[0]


async def test_firestore_counter_doc_carries_an_expire_at_field() -> None:
    fake = _FakeFirestore()
    store = FirestoreGuardCounterStore(fake)  # type: ignore[arg-type]
    await store.try_increment_daily("case", "actor-a", day=date(2026, 8, 30), limit=5)
    ((_key, data),) = fake.docs.items()
    assert isinstance(data["expireAt"], datetime)
    assert data["expireAt"] > datetime.now(UTC) + timedelta(hours=47)


# --- GuardTotalsStore: the global aggregate ----------------------------------


async def test_totals_start_at_zero(totals_store: GuardTotalsStore) -> None:
    totals = await totals_store.get_totals()
    assert totals == GuardTotals(spend_usd=0.0, anonymous_cases=0, anonymous_turns=0)


async def test_add_spend_accumulates(totals_store: GuardTotalsStore) -> None:
    await totals_store.add_spend(1.5)
    updated = await totals_store.add_spend(2.25)
    assert updated.spend_usd == pytest.approx(3.75)


async def test_increment_anonymous_cases_accumulates(totals_store: GuardTotalsStore) -> None:
    await totals_store.increment_anonymous_cases()
    updated = await totals_store.increment_anonymous_cases()
    assert updated.anonymous_cases == 2


async def test_increment_anonymous_turns_accumulates(totals_store: GuardTotalsStore) -> None:
    await totals_store.increment_anonymous_turns()
    updated = await totals_store.increment_anonymous_turns()
    assert updated.anonymous_turns == 2


async def test_mutations_are_independent_fields(totals_store: GuardTotalsStore) -> None:
    await totals_store.add_spend(5.0)
    await totals_store.increment_anonymous_cases()
    totals = await totals_store.get_totals()
    assert totals.spend_usd == pytest.approx(5.0)
    assert totals.anonymous_cases == 1
    assert totals.anonymous_turns == 0


async def test_record_threshold_event_is_idempotent(totals_store: GuardTotalsStore) -> None:
    first = await totals_store.record_threshold_event(50)
    second = await totals_store.record_threshold_event(50)
    assert first is True
    assert second is False


async def test_record_threshold_event_tracks_distinct_thresholds_independently(
    totals_store: GuardTotalsStore,
) -> None:
    assert await totals_store.record_threshold_event(50) is True
    assert await totals_store.record_threshold_event(80) is True
    assert await totals_store.record_threshold_event(50) is False
    assert await totals_store.record_threshold_event(100) is True
