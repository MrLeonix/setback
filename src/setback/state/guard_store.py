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

**Documented approximation**, both stores: every mutation is a plain
read-then-write against `google.cloud.firestore.AsyncClient`, not a
transaction -- matching `FirestoreCaseStore.append_event`'s own accepted
race window ("acceptable here" per that module's docstring) rather than
adding transaction machinery for an abuse-mitigation counter, not a
billing-grade ledger. A lost update under real concurrent traffic only ever
under-counts (biasing toward the demo staying open a little longer than
exactly $26, never toward closing early) or lets a per-actor cap be
exceeded by a small margin -- acceptable trade-offs at this hackathon
build's traffic scale, not a hard security boundary.
"""

from __future__ import annotations

from collections.abc import Callable
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
        snapshot = await ref.get()
        current = 0
        if snapshot.exists:
            data = snapshot.to_dict() or {}
            current = int(data.get("count", 0))
        if current >= limit:
            return False
        await ref.set({"count": current + 1, "expireAt": _utcnow() + timedelta(hours=ttl_hours)})
        return True


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

    async def _mutate(self, fn: Callable[[GuardTotals], GuardTotals]) -> GuardTotals:
        current = await self.get_totals()
        updated = fn(current)
        await self._doc_ref().set(_totals_to_dict(updated))
        return updated

    async def add_spend(self, amount_usd: float) -> GuardTotals:
        return await self._mutate(lambda t: replace(t, spend_usd=t.spend_usd + amount_usd))

    async def increment_anonymous_cases(self) -> GuardTotals:
        return await self._mutate(lambda t: replace(t, anonymous_cases=t.anonymous_cases + 1))

    async def increment_anonymous_turns(self) -> GuardTotals:
        return await self._mutate(lambda t: replace(t, anonymous_turns=t.anonymous_turns + 1))

    async def record_threshold_event(self, threshold_pct: int) -> bool:
        event_ref = self._doc_ref().collection("events").document(f"threshold-{threshold_pct}")
        snapshot = await event_ref.get()
        if snapshot.exists:
            return False
        await event_ref.set({"threshold_pct": threshold_pct, "recorded_at": _utcnow()})
        return True


__all__ = [
    "FirestoreGuardCounterStore",
    "FirestoreGuardTotalsStore",
    "GuardCounterStore",
    "GuardTotals",
    "GuardTotalsStore",
    "InMemoryGuardCounterStore",
    "InMemoryGuardTotalsStore",
]
