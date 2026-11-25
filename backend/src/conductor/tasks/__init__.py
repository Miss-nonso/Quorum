"""Built-in task types. The public demo can only run these."""

from __future__ import annotations

from conductor.tasks.basic import FLAKY, NOTIFY, SLEEP
from conductor.worker.registry import TaskRegistry


def builtin_registry() -> TaskRegistry:
    """A fresh registry holding every built-in task type."""
    registry = TaskRegistry()
    for task_type in (SLEEP, FLAKY, NOTIFY):
        registry.register(task_type)
    return registry
