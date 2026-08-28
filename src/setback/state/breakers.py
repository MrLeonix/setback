"""Per-stage circuit breakers with degrade-not-halt semantics.

Each pipeline stage (interview, bench, clerk, ...) gets its own
:class:`CircuitBreaker` tracking consecutive failures. Opening a breaker
never halts the stage: callers check `is_open`/`state` and fall back to a
degraded alternative — :class:`DegradingBreaker` packages that pattern
directly, e.g. the adjudication bench degrading from ``gemini-3.7-flash``
to ``gemini-3.5-flash-lite`` while its breaker is open.

This module is domain-agnostic: it knows nothing about models, prompts, or
tiers. Callers supply whatever primary/fallback values make sense for their
stage.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum


class CircuitState(StrEnum):
    """A breaker's lifecycle state."""

    CLOSED = "closed"
    """Healthy: calls proceed normally."""

    OPEN = "open"
    """Tripped: callers should degrade rather than call the primary path."""

    HALF_OPEN = "half_open"
    """Cooldown elapsed: one probe is allowed through to test recovery."""


@dataclass
class CircuitBreaker:
    """Tracks consecutive failures for one named stage and gates recovery.

    Not a halt mechanism by itself — it only classifies state. Reaching
    `failure_threshold` consecutive failures opens the breaker; after
    `reset_timeout_seconds` it reports HALF_OPEN to let exactly one probe
    through. A probe's outcome (`record_success`/`record_failure`) decides
    whether it closes again or reopens with the cooldown restarted.
    """

    name: str
    failure_threshold: int = 3
    reset_timeout_seconds: float = 60.0
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False, repr=False)
    _failure_count: int = field(default=0, init=False, repr=False)
    _opened_at: float | None = field(default=None, init=False, repr=False)

    @property
    def state(self) -> CircuitState:
        """The breaker's current state, resolving OPEN -> HALF_OPEN once the
        cooldown has elapsed (a pure function of elapsed time, not a mutation)."""
        cooldown_elapsed = (
            self._opened_at is not None
            and self.clock() - self._opened_at >= self.reset_timeout_seconds
        )
        if self._state is CircuitState.OPEN and cooldown_elapsed:
            return CircuitState.HALF_OPEN
        return self._state

    @property
    def is_open(self) -> bool:
        """True only while genuinely open (HALF_OPEN allows a probe through)."""
        return self.state is CircuitState.OPEN

    def record_success(self) -> None:
        """A call succeeded: close the breaker and clear its failure count."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at = None

    def record_failure(self) -> None:
        """A call failed: count it, tripping (or re-tripping) the breaker as needed."""
        if self.state is CircuitState.HALF_OPEN:
            # The probe failed: reopen immediately and restart the cooldown.
            self._state = CircuitState.OPEN
            self._opened_at = self.clock()
            return
        self._failure_count += 1
        if self._failure_count >= self.failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = self.clock()


@dataclass(frozen=True)
class DegradingBreaker[T]:
    """Pairs a :class:`CircuitBreaker` with a primary/fallback pair of values.

    `current()` is the degrade-not-halt decision point: it returns `primary`
    while the breaker is closed or half-open (letting a recovery probe use
    the primary path), and `fallback` only while genuinely open.
    """

    breaker: CircuitBreaker
    primary: T
    fallback: T

    def current(self) -> T:
        """The value the caller should use for its next call."""
        return self.fallback if self.breaker.is_open else self.primary

    def record_success(self) -> None:
        """Report that a call made with `current()` succeeded."""
        self.breaker.record_success()

    def record_failure(self) -> None:
        """Report that a call made with `current()` failed."""
        self.breaker.record_failure()
