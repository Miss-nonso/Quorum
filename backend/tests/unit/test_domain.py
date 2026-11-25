"""Unit and property tests for the pure domain layer."""

from __future__ import annotations

import random
import uuid

import pytest
from hypothesis import given
from hypothesis import strategies as st

from quorum.domain.backoff import BackoffPolicy
from quorum.domain.idempotency import derive_idempotency_key, validate_idempotency_key
from quorum.domain.retry import DeadLetter, FailureKind, Requeue, decide_after_failure
from quorum.domain.task_state import (
    TERMINAL,
    TRANSITIONS,
    InvalidTransitionError,
    TaskStatus,
    can_transition,
    ensure_transition,
)

# ------------------------------------------------------------------ state machine


def test_every_status_has_a_transition_entry() -> None:
    assert set(TRANSITIONS) == set(TaskStatus)


def test_terminal_states_are_exactly_succeeded_failed_cancelled() -> None:
    assert {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED} == TERMINAL


def test_dead_is_not_terminal_because_it_can_be_replayed() -> None:
    assert can_transition(TaskStatus.DEAD, TaskStatus.READY)
    assert TaskStatus.DEAD not in TERMINAL


def test_only_ready_tasks_can_be_claimed() -> None:
    sources = {s for s in TaskStatus if can_transition(s, TaskStatus.RUNNING)}
    assert sources == {TaskStatus.READY}


def test_only_running_tasks_can_finish() -> None:
    for outcome in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.DEAD):
        assert {s for s in TaskStatus if can_transition(s, outcome)} == {TaskStatus.RUNNING}


@given(st.sampled_from(TaskStatus), st.sampled_from(TaskStatus))
def test_ensure_transition_agrees_with_can_transition(a: TaskStatus, b: TaskStatus) -> None:
    if can_transition(a, b):
        ensure_transition(a, b)
    else:
        with pytest.raises(InvalidTransitionError, match=f"{a.value} -> {b.value}"):
            ensure_transition(a, b)


@given(st.lists(st.sampled_from(TaskStatus), max_size=30))
def test_no_path_leaves_a_terminal_state(path: list[TaskStatus]) -> None:
    state = TaskStatus.PENDING
    for target in path:
        if can_transition(state, target):
            state = target
        if state in TERMINAL:
            assert not any(can_transition(state, t) for t in TaskStatus)


# ------------------------------------------------------------------ backoff

policies = st.builds(
    BackoffPolicy,
    base_s=st.floats(min_value=0.001, max_value=3600),
    factor=st.floats(min_value=1, max_value=10),
    max_s=st.floats(min_value=0.001, max_value=86_400),
    jitter=st.sampled_from(["full", "none"]),
)


@given(policies, st.integers(min_value=1, max_value=10_000), st.integers())
def test_delay_is_within_zero_and_cap(policy: BackoffPolicy, attempt: int, seed: int) -> None:
    delay = policy.delay(attempt, random.Random(seed))
    assert 0 <= delay <= policy.ceiling(attempt) <= policy.max_s


@given(policies, st.integers(min_value=1, max_value=5_000))
def test_ceiling_never_decreases(policy: BackoffPolicy, attempt: int) -> None:
    assert policy.ceiling(attempt) <= policy.ceiling(attempt + 1)


def test_ceiling_doubles_until_capped() -> None:
    policy = BackoffPolicy(base_s=1, factor=2, max_s=10, jitter="none")
    assert [policy.ceiling(n) for n in range(1, 7)] == [1, 2, 4, 8, 10, 10]


def test_huge_attempt_counts_do_not_overflow() -> None:
    assert BackoffPolicy(max_s=60).ceiling(1_000_000) == 60


def test_attempt_is_one_based() -> None:
    with pytest.raises(ValueError, match="1-based"):
        BackoffPolicy().ceiling(0)


def test_full_jitter_spreads_delays() -> None:
    rng = random.Random(42)
    delays = {round(BackoffPolicy(base_s=10, max_s=10).delay(1, rng), 3) for _ in range(50)}
    assert len(delays) > 40


def test_policy_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="extra"):
        BackoffPolicy.model_validate({"base_s": 1, "bogus": 2})


# ------------------------------------------------------------------ retry decisions


@given(
    policies,
    st.integers(min_value=1, max_value=50),
    st.integers(min_value=1, max_value=50),
    st.sampled_from([FailureKind.ERROR, FailureKind.TIMEOUT, FailureKind.LEASE_EXPIRED]),
    st.integers(),
)
def test_retryable_failures_retry_until_attempts_exhausted(
    policy: BackoffPolicy, attempt: int, max_attempts: int, kind: FailureKind, seed: int
) -> None:
    decision = decide_after_failure(
        attempt=attempt,
        max_attempts=max_attempts,
        kind=kind,
        policy=policy,
        rng=random.Random(seed),
    )
    if attempt < max_attempts:
        assert isinstance(decision, Requeue)
        assert 0 <= decision.delay_s <= policy.ceiling(attempt)
    else:
        assert isinstance(decision, DeadLetter)
        assert kind.value in decision.reason


def test_permanent_failure_dead_letters_immediately() -> None:
    decision = decide_after_failure(
        attempt=1,
        max_attempts=10,
        kind=FailureKind.PERMANENT,
        policy=BackoffPolicy(),
        rng=random.Random(0),
    )
    assert isinstance(decision, DeadLetter)


def test_retry_decision_rejects_zero_attempt() -> None:
    with pytest.raises(ValueError, match="1-based"):
        decide_after_failure(
            attempt=0,
            max_attempts=3,
            kind=FailureKind.ERROR,
            policy=BackoffPolicy(),
            rng=random.Random(0),
        )


# ------------------------------------------------------------------ idempotency keys


@given(st.uuids(), st.from_regex(r"[a-z][a-z0-9_]{0,40}", fullmatch=True))
def test_derived_keys_are_stable_and_distinct_per_node(run_id: uuid.UUID, node: str) -> None:
    key = derive_idempotency_key(run_id, node)
    assert key == derive_idempotency_key(run_id, node)
    assert key != derive_idempotency_key(run_id, node + "x")
    assert key != derive_idempotency_key(uuid.uuid4(), node)


@pytest.mark.parametrize("bad", ["", "has space", "x" * 201, "tab\there", "ünicode"])
def test_invalid_override_keys_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="idempotency key"):
        validate_idempotency_key(bad)
