from __future__ import annotations

import pytest

from quorum.domain.backoff import BackoffPolicy
from quorum.tasks import builtin_registry
from quorum.tasks.basic import SLEEP
from quorum.worker.registry import InvalidParamsError, TaskRegistry, UnknownTaskTypeError


def test_builtin_registry_exposes_only_the_known_types() -> None:
    assert builtin_registry().names() == [
        "flaky",
        "http_fetch",
        "notify",
        "report",
        "sleep",
        "store",
        "transform",
        "validate",
    ]


def test_unknown_task_type_is_rejected() -> None:
    with pytest.raises(UnknownTaskTypeError, match="shell"):
        builtin_registry().build_spec("shell", {"cmd": "rm -rf /"})


@pytest.mark.parametrize(
    ("task_type", "params"),
    [
        ("sleep", {"seconds": -1}),
        ("sleep", {"seconds": 301}),
        ("sleep", {"seconds": 1, "extra": True}),
        ("notify", {}),
        ("notify", {"message": "x" * 501}),
        ("flaky", {"failure_rate": 1.5}),
    ],
)
def test_bad_params_are_rejected_with_field_details(
    task_type: str, params: dict[str, object]
) -> None:
    with pytest.raises(InvalidParamsError) as info:
        builtin_registry().build_spec(task_type, params)
    assert info.value.task_type == task_type
    assert info.value.errors


def test_build_spec_normalises_params_and_carries_options() -> None:
    policy = BackoffPolicy(base_s=2)
    spec = builtin_registry().build_spec(
        "notify", {"message": "hi"}, priority=7, max_attempts=5, backoff=policy, delay_s=3
    )
    assert spec.params == {"channel": "general", "message": "hi", "vars": {}, "hold_s": 0.0}
    assert (spec.priority, spec.max_attempts, spec.backoff, spec.delay_s) == (7, 5, policy, 3)


def test_duplicate_registration_is_an_error() -> None:
    registry = TaskRegistry()
    registry.register(SLEEP)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(SLEEP)


def test_timing_settings_are_cross_validated() -> None:
    from quorum.core.config import Settings

    with pytest.raises(ValueError, match="half of lease_ttl_s"):
        Settings.from_env({"QUORUM_LEASE_TTL_S": "5", "QUORUM_HEARTBEAT_INTERVAL_S": "3"})
    with pytest.raises(ValueError, match="less than worker_dead_after_s"):
        Settings.from_env(
            {"QUORUM_WORKER_UNHEALTHY_AFTER_S": "60", "QUORUM_WORKER_DEAD_AFTER_S": "30"}
        )
