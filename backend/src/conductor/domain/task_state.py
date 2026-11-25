"""Task lifecycle as an explicit state machine.

::

    pending ──► ready ──► running ──► succeeded
       │          ▲  │       │ │
       │          │  │       │ ├──► failed      (non-retryable outcome, Phase 2 policies)
       │          │  │       │ └──► dead ───┐  (attempts exhausted → dead-letter queue)
       │          │  │       │              │
       │          └──┼───────┘ retry /      │
       │          ▲  │         lease lost / │
       │          │  │         released     │
       │          └──┼──────────────────────┘ replay
       ▼             ▼
    cancelled ◄──────┘  (also from running)

The database enforces the *shape* (CHECK constraints); this module is the
single source of truth for which *moves* are legal, and every status change
in the store goes through :func:`ensure_transition`.
"""

from __future__ import annotations

from enum import StrEnum


class TaskStatus(StrEnum):
    """Where a task instance is in its lifecycle."""

    PENDING = "pending"  # waiting for dependencies
    READY = "ready"  # claimable once not_before has passed
    RUNNING = "running"  # leased by a worker
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"  # in the dead-letter queue; replayable
    CANCELLED = "cancelled"


TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.READY, TaskStatus.CANCELLED}),
    TaskStatus.READY: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.DEAD,
            TaskStatus.READY,  # retry with backoff, lease expired, or released on shutdown
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.DEAD: frozenset({TaskStatus.READY}),  # replay
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}

TERMINAL: frozenset[TaskStatus] = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)


class InvalidTransitionError(ValueError):
    """Raised when code attempts a status change the state machine forbids."""

    def __init__(self, current: TaskStatus, target: TaskStatus) -> None:
        super().__init__(f"illegal task transition {current.value} -> {target.value}")
        self.current = current
        self.target = target


def can_transition(current: TaskStatus, target: TaskStatus) -> bool:
    """Whether ``current -> target`` is a legal move."""
    return target in TRANSITIONS[current]


def ensure_transition(current: TaskStatus, target: TaskStatus) -> None:
    """Raise :class:`InvalidTransitionError` unless ``current -> target`` is legal."""
    if not can_transition(current, target):
        raise InvalidTransitionError(current, target)
