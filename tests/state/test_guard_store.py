"""Tests for setback.state.guard_store: the public-abuse-guard's durable
state -- per-actor daily counters (`GuardCounterStore`) and the global
public-spend aggregate (`GuardTotalsStore`), each with an in-memory double
and a Firestore adapter exercised against a minimal fake client (mirroring
`tests/state/test_firestore.py`'s own `_FakeFirestore` pattern).

Fully offline: no live Firestore, no network.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from google.cloud import firestore

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
        if self._root.yield_on_io:
            await asyncio.sleep(0)
        return _FakeSnapshot(self.id, self._root.docs.get(self._path))

    async def set(self, document_data: dict[str, Any], merge: bool = False) -> None:
        """Mirrors real `AsyncDocumentReference.set`: `merge=True` combines
        with any existing document rather than replacing it, and a
        `firestore.Increment` value is resolved server-side as an atomic
        field-transform against whatever the field currently holds --
        unlike a client-side read-modify-write, this resolution happens as
        one indivisible step with no `await` in between, exactly like the
        real service, so concurrent callers can never race each other out
        of an increment the way they can race a get()-then-set() pair.
        """
        if self._root.yield_on_io:
            await asyncio.sleep(0)
        current = dict(self._root.docs.get(self._path, {})) if merge else {}
        for field_name, value in document_data.items():
            if isinstance(value, firestore.Increment):
                current[field_name] = current.get(field_name, 0) + value.value
            else:
                current[field_name] = value
        self._root.docs[self._path] = current

    def collection(self, name: str) -> _FakeCollectionRef:
        return _FakeCollectionRef(self._root, (*self._path, name))


class _FakeFirestore:
    def __init__(self, *, yield_on_io: bool = False) -> None:
        self.docs: dict[tuple[str, ...], dict[str, Any]] = {}
        # `yield_on_io`: when True, `.get()`/`.set()` each yield control back
        # to the event loop (`await asyncio.sleep(0)`) before touching
        # `docs`, so concurrent `asyncio.gather`-driven callers actually
        # interleave the way separate real Firestore round-trips would --
        # needed to make a read-then-write race reproducible against this
        # in-process fake at all. Off by default so every existing
        # single-call test keeps its simple, deterministic control flow.
        self.yield_on_io = yield_on_io

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


# --- adversarial: concurrent writers must never lose an update ---------------
#
# Security-review finding: both Firestore adapters originally did a plain
# `await ref.get()` then, after some Python-side computation, an `await
# ref.set(...)` -- two separate round-trips with an `await` on each side.
# Under real concurrent public traffic (many anonymous requests landing on
# the *same* actor's daily counter doc, or -- worse -- the *one* global
# `guard_totals/public` aggregate doc that every single anonymous request
# writes to) two callers can both read the same "before" value before
# either has written its "after" value back, and the second writer's `set`
# clobbers the first's -- a classic lost update. For a counter that exists
# specifically to cap spend/attempts, losing updates only ever
# *undercounts*, i.e. leaks past the cap the counter exists to enforce --
# the opposite of "acceptable to lose a little accuracy" for a billing-grade
# ceiling the founder is relying on as "an important blocker".
#
# These tests drive real concurrent interleaving (`asyncio.gather` against
# a fake client whose `get`/`set` each yield control once, exactly like a
# real network round-trip would) and assert every single one of `n`
# concurrent writers' contributions survives -- pinning the fix (an atomic
# Firestore field-transform `Increment`, resolved server-side with no
# read-then-write window at all) rather than merely re-asserting the old
# read-then-write shape.


async def test_concurrent_daily_counter_increments_do_not_lose_updates() -> None:
    """20 concurrent case-creation attempts from the same actor, all
    landing in the same pathological lockstep (every one of the 20
    `set()`s completes before any of the 20 `get()`-backs resolves -- the
    single worst-case ordering `asyncio.gather` against a yielding fake can
    produce, deliberately more adversarial than real network jitter would
    ever actually serialize).

    Two properties matter here, and they matter differently:

    1. The stored counter itself must never lose an update -- it must land
       on exactly 20, matching the 20 real attempts that were made. This
       is the actual bug a read-then-write race causes (see the module
       docstring): silently undercounting, which is the wrong direction
       for a cap meant to stop abuse.
    2. The cap must never be exceeded: at most `limit` of the 20 may come
       back allowed. Under this specific pathological ordering, a caller's
       own post-increment read can observe *other* callers' increments
       that landed first, so it can end up denying a request that a
       perfectly-ordered arbiter would have allowed -- fewer than `limit`
       allowed is an acceptable, fail-closed outcome; more than `limit`
       allowed never is.
    """
    fake = _FakeFirestore(yield_on_io=True)
    store = FirestoreGuardCounterStore(fake)  # type: ignore[arg-type]
    day = date(2026, 8, 30)
    limit = 5

    results = await asyncio.gather(
        *(store.try_increment_daily("case", "actor-a", day=day, limit=limit) for _ in range(20))
    )

    ((_key, data),) = fake.docs.items()
    assert data["count"] == 20, "a lost update under-counted concurrent attempts"
    allowed_count = sum(1 for allowed in results if allowed)
    assert allowed_count <= limit, "the cap was exceeded under concurrent load"


async def test_concurrent_spend_increments_do_not_lose_updates() -> None:
    """50 concurrent $0.001 anonymous-turn bookings against the *one*
    global aggregate doc every anonymous request shares -- the doc every
    public request in the whole deployment contends on, so this is the
    highest-contention case in the guard. The total must land on exactly
    50 * 0.001, not less."""
    fake = _FakeFirestore(yield_on_io=True)
    store = FirestoreGuardTotalsStore(fake)  # type: ignore[arg-type]

    await asyncio.gather(*(store.add_spend(0.001) for _ in range(50)))

    totals = await store.get_totals()
    assert totals.spend_usd == pytest.approx(0.05)


async def test_concurrent_anonymous_case_increments_do_not_lose_updates() -> None:
    fake = _FakeFirestore(yield_on_io=True)
    store = FirestoreGuardTotalsStore(fake)  # type: ignore[arg-type]

    await asyncio.gather(*(store.increment_anonymous_cases() for _ in range(30)))

    totals = await store.get_totals()
    assert totals.anonymous_cases == 30
