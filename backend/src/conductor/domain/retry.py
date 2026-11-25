"""What happens to a task after an attempt fails."""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum

from conductor.domain.backoff import BackoffPolicy


class FailureKind(StrEnum):
    """Why an attempt ended without success."""

    ERROR = "error"  # the task raised
    TIMEOUT = "timeout"  # the task exceeded its timeout
    LEASE_EXPIRED = "lease_expired"  # the worker vanished or stalled (detected by the reaper)
    PERMANENT = "permanent"  # the task declared the failure non-retryable


@dataclass(frozen=True, slots=True)
class Requeue:
    """Retry the task after ``delay_s`` seconds."""

    delay_s: float


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """Stop retrying and move the task to the dead-letter queue."""

    reason: str


RetryDecision = Requeue | DeadLetter


def decide_after_failure(
    *,
    attempt: int,
    max_attempts: int,
    kind: FailureKind,
    policy: BackoffPolicy,
    rng: random.Random,
) -> RetryDecision:
    """Decide whether failed attempt ``attempt`` (1-based) is retried.

    Lease expiry counts as a used attempt: a task that reliably crashes its
    worker (a "poison pill") must eventually stop being retried, or it would
    take down the pool forever.
    """
    if attempt < 1 or max_attempts < 1:
        raise ValueError("attempt and max_attempts are 1-based")
    if kind is FailureKind.PERMANENT:
        return DeadLetter(reason=f"permanent failure on attempt {attempt}")
    if attempt >= max_attempts:
        return DeadLetter(reason=f"{kind.value}: attempts exhausted ({attempt}/{max_attempts})")
    return Requeue(delay_s=policy.delay(attempt, rng))
