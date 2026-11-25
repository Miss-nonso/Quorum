"""The task-type registry: the complete list of things a task can do.

There is no arbitrary code execution. A task names a registered type, and
its params are validated against that type's Pydantic model, both when it
is enqueued and again when it is executed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from quorum.domain.backoff import BackoffPolicy
from quorum.domain.dag import WorkflowDefinition, validate_definition
from quorum.domain.idempotency import validate_idempotency_key
from quorum.store.tasks import TaskSpec

JsonDict = dict[str, Any]


class UnknownTaskTypeError(LookupError):
    """The task names a type that is not registered."""


class InvalidParamsError(ValueError):
    """The task's params fail its type's schema."""

    def __init__(self, task_type: str, error: ValidationError) -> None:
        super().__init__(f"invalid params for {task_type!r}: {error}")
        self.task_type = task_type
        self.errors = error.errors(include_url=False)


class PermanentTaskError(Exception):
    """Raise from a handler when retrying cannot help (dead-letters immediately)."""


@dataclass(frozen=True, slots=True)
class TaskEnvironment:
    """Operator-controlled limits that built-in tasks must respect."""

    # Hosts http_fetch may contact: "host" (any port) or "host:port".
    http_allowlist: frozenset[str] = frozenset({"localhost", "127.0.0.1"})
    http_timeout_s: float = 10.0
    http_max_bytes: int = 256 * 1024


@dataclass(frozen=True, slots=True)
class TaskContext:
    """What a handler knows about the attempt it is running."""

    task_id: UUID
    run_id: UUID
    attempt: int
    max_attempts: int
    idempotency_key: str
    worker_id: UUID
    engine: AsyncEngine  # for side effects recorded under idempotency_key
    env: TaskEnvironment = field(default_factory=TaskEnvironment)


Handler = Callable[[TaskContext, Any], Awaitable[JsonDict]]


@dataclass(frozen=True, slots=True)
class TaskType:
    """A registered task type: a name, a params schema and an async handler."""

    name: str
    params_model: type[BaseModel]
    handler: Handler
    description: str
    has_side_effects: bool = False


class TaskRegistry:
    """Name -> :class:`TaskType` mapping."""

    def __init__(self, types: Mapping[str, TaskType] | None = None) -> None:
        self._types: dict[str, TaskType] = dict(types or {})

    def register(self, task_type: TaskType) -> None:
        if task_type.name in self._types:
            raise ValueError(f"task type {task_type.name!r} is already registered")
        self._types[task_type.name] = task_type

    def get(self, name: str) -> TaskType:
        try:
            return self._types[name]
        except KeyError:
            raise UnknownTaskTypeError(f"unknown task type {name!r}") from None

    def names(self) -> list[str]:
        return sorted(self._types)

    def parse_params(self, name: str, params: Mapping[str, Any]) -> BaseModel:
        """Validate ``params`` for type ``name``; raise on unknown type or bad params."""
        task_type = self.get(name)
        try:
            return task_type.params_model.model_validate(dict(params))
        except ValidationError as exc:
            raise InvalidParamsError(name, exc) from exc

    def validate_workflow(self, definition: WorkflowDefinition) -> list[str]:
        """Validate a workflow's graph *and* every node's params; return topological order.

        Raises:
            quorum.domain.dag.DagError: listing every problem found.
        """

        def check(task_type: str, params: Mapping[str, Any]) -> None:
            self.parse_params(task_type, params)

        return validate_definition(definition, check)

    def build_spec(
        self,
        task_type: str,
        params: Mapping[str, Any] | None = None,
        *,
        priority: int = 0,
        max_attempts: int = 3,
        backoff: BackoffPolicy | None = None,
        timeout_s: float = 60.0,
        idempotency_key: str | None = None,
        delay_s: float = 0.0,
    ) -> TaskSpec:
        """Validate and normalise a task before it is enqueued.

        Raises:
            UnknownTaskTypeError, InvalidParamsError: bad type or params.
            ValueError: an invalid idempotency key override.
        """
        parsed = self.parse_params(task_type, params or {})
        if idempotency_key is not None:
            validate_idempotency_key(idempotency_key)
        return TaskSpec(
            task_type=task_type,
            params=parsed.model_dump(mode="json"),
            priority=priority,
            max_attempts=max_attempts,
            backoff=backoff or BackoffPolicy(),
            timeout_s=timeout_s,
            idempotency_key=idempotency_key,
            delay_s=delay_s,
        )
