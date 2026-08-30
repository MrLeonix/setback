"""Durable state for the public-abuse guard (`console.guards`): per-actor
daily counters and the global public-spend aggregate.

Deliberately its own small module, separate from `state.firestore`: these
documents live under their own root collections (`guard_counters`,
`guard_totals`), not under any single case's own `cases/{case_id}` subtree,
so this has no natural home inside `CaseStore`'s per-case-scoped port.

Two independent stores, each with an in-memory test/local-dev double and a
thin Firestore adapter:

1. :class:`GuardCounterStore` -- one doc per (counter kind, salted-hashed
   actor id, calendar day), e.g. ``guard_counters/case_<sha256-hex>_20260830``,
   used for the per-client daily case-creation cap. Never keyed by a raw IP
   (GDPR/PII rule) -- the caller (`console.guards.hashed_client_id`) already
   salts and hashes before this module ever sees the identity.
2. :class:`GuardTotalsStore` -- a single aggregate doc, ``guard_totals/public``,
   plus one idempotent marker doc per threshold crossing under
   ``guard_totals/public/events/{event_id}`` (the same append-only,
   idempotent-by-natural-key shape `state.firestore.CaseStore.append_event`
   already uses, just at the deployment-wide level instead of per-case).

**Concurrency**: every counting mutation below (the daily per-actor counter,
and every field of the global spend/count aggregate) is a single atomic
Firestore field-transform (`firestore.Increment`, via `.set(..., merge=True)`),
never a client-side read-then-write. `FirestoreCaseStore.append_event`'s own
accepted read-then-write race window ("acceptable here" per that module's
docstring) is a materially different case -- it assumes one job writes to a
given case at a time. The aggregate here has the opposite shape: *every*
anonymous request across the whole public deployment writes to the exact
same `guard_totals/public` document, the single highest-contention write in
this system, so a read-then-write race there is not a rare edge case but
the expected common case under real traffic -- and for a spend *ceiling*,
a lost update undercounts spend, which lets real spending run past the
ceiling rather than stopping at it (the wrong direction for a cost cap).
An earlier revision of this module did read-then-write here; a security
review (2026-08-30) demonstrated concurrent requests losing updates
(`tests/state/test_guard_store.py::test_concurrent_*`) and this was fixed
to the current atomic-increment shape before the guard shipped. Only
`record_threshold_event`'s existence-check-then-set keeps a (accepted,
low-stakes) race: at worst a threshold's one-time observability event is
written twice, never a cap bypass.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from google.cloud import firestore

from setback.config import FIRESTORE_DB, GCP_PROJECT


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _get_client() -> firestore.AsyncClient:
    """Build the default Firestore async client, exactly like
    `state.firestore.get_firestore_client` -- same project, same named
    database (`setback-au` in this wave's deployment). Never called by the
    test suite, which always injects a fake client double instead."""
    return firestore.AsyncClient(project=GCP_PROJECT, database=FIRESTORE_DB)


# --- (a) per-actor daily counters --------------------------------------------


class GuardCounterStore(Protocol):
    """Counts anonymous-actor attempts against a per-day rolling cap, keyed
    by an already salted-and-hashed actor id (never a raw IP)."""

    async def try_increment_daily(
        self, prefix: str, key_hash: str, *, day: date, limit: int, ttl_hours: float = 48.0
    ) -> bool:
        """Atomically-enough increment the (prefix, key_hash, day) counter
        and return whether this attempt is allowed under `limit`. A refused
        attempt (already at `limit`) is not itself counted, matching
        `SlidingWindowRateLimiter.allow`'s own semantics."""
        ...


def _daily_counter_doc_id(prefix: str, key_hash: str, day: date) -> str:
    return f"{prefix}_{key_hash}_{day:%Y%m%d}"


class InMemoryGuardCounterStore:
    """A dict-backed `GuardCounterStore` test double / local-dev fallback.
    Not distributed or durable across a process restart -- correct for this
    build's single-Cloud-Run-instance topology (see
    `console.guards.SlidingWindowRateLimiter`'s own docstring for the same
    caveat), and the production default is `FirestoreGuardCounterStore`
    regardless."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    async def try_increment_daily(
        self, prefix: str, key_hash: str, *, day: date, limit: int, ttl_hours: float = 48.0
    ) -> bool:
        doc_id = _daily_counter_doc_id(prefix, key_hash, day)
        current = self._counts.get(doc_id, 0)
        if current >= limit:
            return False
        self._counts[doc_id] = current + 1
        return True


class FirestoreGuardCounterStore:
    """The production `GuardCounterStore`. See the module docstring for the
    document layout and the accepted read-then-write race window."""

    def __init__(self, client: firestore.AsyncClient | None = None) -> None:
        self._client = client if client is not None else _get_client()

    async def try_increment_daily(
        self, prefix: str, key_hash: str, *, day: date, limit: int, ttl_hours: float = 48.0
    ) -> bool:
        doc_id = _daily_counter_doc_id(prefix, key_hash, day)
        ref = self._client.collection("guard_counters").document(doc_id)
        # An atomic field-transform, not a read-then-write: `count` is
        # incremented server-side with no window for a concurrent caller to
        # read the same "before" value (see the module docstring's
        # "Concurrency" section). Every attempt increments -- including one
        # that ends up refused below -- which is why the comparison is
        # `<=`: this is the Nth attempt is allowed iff N <= limit, a
        # definition that (unlike "read current, compare, then write") is
        # correct under arbitrary concurrency.
        await ref.set(
            {"count": firestore.Increment(1), "expireAt": _utcnow() + timedelta(hours=ttl_hours)},
            merge=True,
        )
        snapshot = await ref.get()
        data = snapshot.to_dict() or {}
        current = int(data.get("count", 0))
        return current <= limit


# --- (b) the global public-spend aggregate -----------------------------------


@dataclass(frozen=True)
class GuardTotals:
    """A snapshot of the running public-spend aggregate (`guard_totals/public`)."""

    spend_usd: float = 0.0
    anonymous_cases: int = 0
    anonymous_turns: int = 0


class GuardTotalsStore(Protocol):
    """The global aggregate the public-spend ceiling gate reads (via
    `console.guards.CachedGuardTotalsReader`) and every anonymous cost
    event updates."""

    async def get_totals(self) -> GuardTotals: ...

    async def add_spend(self, amount_usd: float) -> GuardTotals: ...

    async def increment_anonymous_cases(self) -> GuardTotals: ...

    async def increment_anonymous_turns(self) -> GuardTotals: ...

    async def record_threshold_event(self, threshold_pct: int) -> bool:
        """Idempotently record that `threshold_pct` (50/80/100) has been
        crossed. Returns `True` the first time this threshold is recorded,
        `False` on every later call -- the same append-only-idempotent
        shape as `state.firestore.CaseStore.append_event`."""
        ...


_GUARD_TOTALS_DOC_ID = "public"


def _totals_from_dict(data: dict[str, Any] | None) -> GuardTotals:
    data = data or {}
    return GuardTotals(
        spend_usd=float(data.get("spend_usd", 0.0)),
        anonymous_cases=int(data.get("anonymous_cases", 0)),
        anonymous_turns=int(data.get("anonymous_turns", 0)),
    )


def _totals_to_dict(totals: GuardTotals) -> dict[str, Any]:
    return {
        "spend_usd": totals.spend_usd,
        "anonymous_cases": totals.anonymous_cases,
        "anonymous_turns": totals.anonymous_turns,
    }


class InMemoryGuardTotalsStore:
    """A dict-backed `GuardTotalsStore` test double / local-dev fallback."""

    def __init__(self) -> None:
        self._totals = GuardTotals()
        self._threshold_events: set[int] = set()

    async def get_totals(self) -> GuardTotals:
        return self._totals

    async def add_spend(self, amount_usd: float) -> GuardTotals:
        self._totals = replace(self._totals, spend_usd=self._totals.spend_usd + amount_usd)
        return self._totals

    async def increment_anonymous_cases(self) -> GuardTotals:
        self._totals = replace(self._totals, anonymous_cases=self._totals.anonymous_cases + 1)
        return self._totals

    async def increment_anonymous_turns(self) -> GuardTotals:
        self._totals = replace(self._totals, anonymous_turns=self._totals.anonymous_turns + 1)
        return self._totals

    async def record_threshold_event(self, threshold_pct: int) -> bool:
        if threshold_pct in self._threshold_events:
            return False
        self._threshold_events.add(threshold_pct)
        return True


class FirestoreGuardTotalsStore:
    """The production `GuardTotalsStore`. See the module docstring for the
    document layout and the accepted read-then-write race window."""

    def __init__(self, client: firestore.AsyncClient | None = None) -> None:
        self._client = client if client is not None else _get_client()

    def _doc_ref(self) -> firestore.AsyncDocumentReference:
        return self._client.collection("guard_totals").document(_GUARD_TOTALS_DOC_ID)

    async def get_totals(self) -> GuardTotals:
        snapshot = await self._doc_ref().get()
        return _totals_from_dict(snapshot.to_dict() if snapshot.exists else None)

    async def _increment_field(self, field_name: str, amount: float) -> GuardTotals:
        """Atomically add `amount` to one field of the single
        `guard_totals/public` document -- the one doc *every* anonymous
        request in the whole deployment writes to, so this is the
        highest-contention write in the guard (see the module docstring's
        "Concurrency" section). A `firestore.Increment` field-transform,
        not a read-then-write: correct under any number of simultaneous
        callers, with no lost-update window at all."""
        await self._doc_ref().set({field_name: firestore.Increment(amount)}, merge=True)
        return await self.get_totals()

    async def add_spend(self, amount_usd: float) -> GuardTotals:
        return await self._increment_field("spend_usd", amount_usd)

    async def increment_anonymous_cases(self) -> GuardTotals:
        return await self._increment_field("anonymous_cases", 1)

    async def increment_anonymous_turns(self) -> GuardTotals:
        return await self._increment_field("anonymous_turns", 1)

    async def record_threshold_event(self, threshold_pct: int) -> bool:
        event_ref = self._doc_ref().collection("events").document(f"threshold-{threshold_pct}")
        snapshot = await event_ref.get()
        if snapshot.exists:
            return False
        await event_ref.set({"threshold_pct": threshold_pct, "recorded_at": _utcnow()})
        return True


# --- (c) the global judge-gated live-Veo generation cap ---------------------
#
# Wave 13 (founder-authorized, 2026-08-29/31): judge-gated LIVE Veo 3.1
# generation is real, metered spend on a genuinely shared quota -- unlike
# `GuardCounterStore` above (per-actor, per-day), this is ONE deployment-wide
# ceiling on total attempts ever, so it is a single document rather than a
# keyed collection. `VeoLiveCounterStore.try_increment` mirrors `GuardCounter
# Store.try_increment_daily`'s exact semantics and atomicity guarantee (a
# single Firestore `Increment` field-transform, never a read-then-write --
# see the module docstring's "Concurrency" section, which applies here
# unchanged): every attempt increments the stored count, including one that
# ends up refused, and the caller passes `limit` on each call rather than the
# store owning it, so `job.pipeline.RealPipelineRunner` can size the cap from
# `VEO_LIVE_MAX_GENERATIONS` without this store needing to know about env
# vars at all.


class VeoLiveCounterStore(Protocol):
    """One global, atomic attempt counter, hard-capping real Veo 3.1 spend
    across every judge-gated case in the whole deployment."""

    async def try_increment(self, *, limit: int) -> bool:
        """Atomically increment the single global counter and return
        whether this attempt (the Nth) is allowed under `limit` -- i.e.
        `N <= limit`. Every call increments, whether or not it is allowed,
        exactly like `GuardCounterStore.try_increment_daily`."""
        ...


_VEO_LIVE_COUNTER_DOC_ID = "veo_live"
"""Lives under the same `guard_totals` root collection as the public-spend
aggregate (`guard_totals/public`) -- a sibling ceiling, not a per-case
document, per the brief's own naming (`guard_totals/veo_live`)."""


class InMemoryVeoLiveCounterStore:
    """A single-int-backed `VeoLiveCounterStore` test double / local-dev
    fallback. Not distributed or durable across a process restart -- the
    production default is `FirestoreVeoLiveCounterStore` regardless (see
    `InMemoryGuardCounterStore`'s own docstring for the identical caveat)."""

    def __init__(self) -> None:
        self._count = 0

    async def try_increment(self, *, limit: int) -> bool:
        self._count += 1
        return self._count <= limit


class FirestoreVeoLiveCounterStore:
    """The production `VeoLiveCounterStore`: `guard_totals/veo_live`,
    incremented via a single atomic `firestore.Increment` field-transform
    (never a read-then-write -- see the module docstring's "Concurrency"
    section)."""

    def __init__(self, client: firestore.AsyncClient | None = None) -> None:
        self._client = client if client is not None else _get_client()

    def _doc_ref(self) -> firestore.AsyncDocumentReference:
        return self._client.collection("guard_totals").document(_VEO_LIVE_COUNTER_DOC_ID)

    async def try_increment(self, *, limit: int) -> bool:
        ref = self._doc_ref()
        await ref.set({"count": firestore.Increment(1)}, merge=True)
        snapshot = await ref.get()
        data = snapshot.to_dict() or {}
        current = int(data.get("count", 0))
        return current <= limit


__all__ = [
    "FirestoreGuardCounterStore",
    "FirestoreGuardTotalsStore",
    "FirestoreVeoLiveCounterStore",
    "GuardCounterStore",
    "GuardTotals",
    "GuardTotalsStore",
    "InMemoryGuardCounterStore",
    "InMemoryGuardTotalsStore",
    "InMemoryVeoLiveCounterStore",
    "VeoLiveCounterStore",
]
