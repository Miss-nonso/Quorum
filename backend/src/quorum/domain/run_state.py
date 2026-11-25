"""Workflow run lifecycle and how it follows from its tasks' states."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from quorum.domain.task_state import TaskStatus


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}),
    # A run that stopped on a dead-lettered task resumes when the task is replayed.
    RunStatus.FAILED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


class InvalidRunTransitionError(ValueError):
    def __init__(self, current: RunStatus, target: RunStatus) -> None:
        super().__init__(f"illegal run transition {current.value} -> {target.value}")


def ensure_run_transition(current: RunStatus, target: RunStatus) -> None:
    if target not in RUN_TRANSITIONS[current]:
        raise InvalidRunTransitionError(current, target)


def derive_run_status(task_counts: Mapping[TaskStatus, int]) -> RunStatus:
    """The status a non-cancelled run should have, given how many tasks are in each state.

    A run is *running* while any task can still make progress (ready or
    running; ``pending`` tasks only progress through those). Once nothing can
    progress it is *succeeded* if every task succeeded, otherwise *failed*;
    that includes runs blocked behind a dead-lettered task, which resume
    (back to running) if the task is replayed.
    """
    if task_counts.get(TaskStatus.READY, 0) or task_counts.get(TaskStatus.RUNNING, 0):
        return RunStatus.RUNNING
    total = sum(task_counts.values())
    if total and task_counts.get(TaskStatus.SUCCEEDED, 0) == total:
        return RunStatus.SUCCEEDED
    return RunStatus.FAILED
