"""Built-in task types. The public demo can only run these."""

from __future__ import annotations

from quorum.tasks.basic import FLAKY, NOTIFY, SLEEP
from quorum.tasks.pipeline import HTTP_FETCH, REPORT, STORE, TRANSFORM, VALIDATE
from quorum.worker.registry import TaskRegistry


def builtin_registry() -> TaskRegistry:
    """A fresh registry holding every built-in task type."""
    registry = TaskRegistry()
    for task_type in (SLEEP, FLAKY, NOTIFY, HTTP_FETCH, VALIDATE, TRANSFORM, STORE, REPORT):
        registry.register(task_type)
    return registry
