"""Workflow definitions: a validated DAG of task nodes.

A definition is checked once, when it is saved, so a run can never be
materialised from a graph that has a cycle, a dangling dependency, an
unknown task type, bad params, or a reference to data that will not exist.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from conductor.domain.backoff import BackoffPolicy
from conductor.domain.refs import find_refs, is_ref

MAX_NODES = 50
NODE_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class FailurePolicy(StrEnum):
    """What a node's permanent failure means for its run."""

    # Retries, then the dead-letter queue. Dependents wait; replaying the
    # task resumes the run where it stopped.
    RETRY_THEN_DEAD = "retry_then_dead"
    # Retries, then the whole run fails and its unfinished tasks are cancelled.
    FAIL_RUN = "fail_run"
    # Retries, then the node is marked failed but its dependents still run.
    # The run finishes as failed, but finishes.
    CONTINUE = "continue"


class NodeSpec(BaseModel):
    """One task in a workflow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(description="Unique within the workflow: lowercase, digits, underscores.")
    task_type: str
    params: dict[str, Any] = Field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    failure_policy: FailurePolicy = FailurePolicy.RETRY_THEN_DEAD
    priority: int = Field(default=0, ge=-100, le=100)
    max_attempts: int = Field(default=3, ge=1, le=20)
    timeout_s: float = Field(default=60.0, gt=0, le=3600)
    backoff: BackoffPolicy = Field(default_factory=BackoffPolicy)

    @field_validator("key")
    @classmethod
    def _key_shape(cls, value: str) -> str:
        if not NODE_KEY.match(value):
            raise ValueError("must match ^[a-z][a-z0-9_]{0,39}$")
        return value


class WorkflowDefinition(BaseModel):
    """The JSON stored in ``workflows.definition``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: tuple[NodeSpec, ...] = Field(min_length=1, max_length=MAX_NODES)

    def node(self, key: str) -> NodeSpec:
        for node in self.nodes:
            if node.key == key:
                return node
        raise KeyError(key)


class DagError(ValueError):
    """A definition that cannot be run. ``problems`` lists every issue found."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


ParamsValidator = Callable[[str, Mapping[str, Any]], None]
"""Raises if ``params`` are invalid for ``task_type`` (references already replaced)."""


def validate_definition(
    definition: WorkflowDefinition, validate_params: ParamsValidator | None = None
) -> list[str]:
    """Check structure, acyclicity, references and (optionally) params.

    Returns the node keys in a deterministic topological order.

    Raises:
        DagError: listing every problem, not just the first.
    """
    problems: list[str] = []
    keys = [n.key for n in definition.nodes]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        problems.append(f"duplicate node keys: {', '.join(duplicates)}")
    known = set(keys)

    for node in definition.nodes:
        for dep in node.depends_on:
            if dep == node.key:
                problems.append(f"{node.key}: depends on itself")
            elif dep not in known:
                problems.append(f"{node.key}: depends on unknown node {dep!r}")
        if len(set(node.depends_on)) != len(node.depends_on):
            problems.append(f"{node.key}: lists a dependency twice")
    if problems:
        raise DagError(problems)

    order, cycle = _topological_order(definition.nodes)
    if cycle:
        raise DagError([f"cycle detected: {' -> '.join(cycle)}"])

    ancestors = _ancestors(definition.nodes, order)
    for node in definition.nodes:
        for ref in find_refs(node.params):
            if ref.node not in known:
                problems.append(f"{node.key}: references unknown node {ref.node!r}")
            elif ref.node not in ancestors[node.key]:
                problems.append(
                    f"{node.key}: references {ref.node!r}, which is not upstream of it, "
                    "so its result may not exist yet"
                )
        if validate_params is not None:
            try:
                validate_params(node.task_type, _placeholder_refs(node.params))
            except (LookupError, ValueError) as exc:
                problems.append(f"{node.key}: {exc}")
    if problems:
        raise DagError(problems)
    return order


def _topological_order(nodes: Iterable[NodeSpec]) -> tuple[list[str], list[str]]:
    """Kahn's algorithm, ties broken by declaration order. Returns (order, cycle)."""
    nodes = list(nodes)
    position = {n.key: i for i, n in enumerate(nodes)}
    indegree = {n.key: len(n.depends_on) for n in nodes}
    dependents: dict[str, list[str]] = {n.key: [] for n in nodes}
    for n in nodes:
        for dep in n.depends_on:
            dependents[dep].append(n.key)

    ready = sorted((k for k, d in indegree.items() if d == 0), key=position.__getitem__)
    order: list[str] = []
    while ready:
        key = ready.pop(0)
        order.append(key)
        for child in dependents[key]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort(key=position.__getitem__)
    if len(order) == len(nodes):
        return order, []
    return order, _find_cycle(nodes, {k for k, d in indegree.items() if d > 0})


def _find_cycle(nodes: list[NodeSpec], remaining: set[str]) -> list[str]:
    """Walk dependencies inside the unresolved set until a key repeats."""
    deps = {n.key: [d for d in n.depends_on if d in remaining] for n in nodes}
    start = min(remaining)
    path = [start]
    while True:
        nxt = min(deps[path[-1]])
        if nxt in path:
            return [*path[path.index(nxt) :], nxt]
        path.append(nxt)


def _ancestors(nodes: Iterable[NodeSpec], order: list[str]) -> dict[str, set[str]]:
    by_key = {n.key: n for n in nodes}
    result: dict[str, set[str]] = {}
    for key in order:
        acc: set[str] = set()
        for dep in by_key[key].depends_on:
            acc.add(dep)
            acc |= result[dep]
        result[key] = acc
    return result


def _placeholder_refs(value: Any) -> Any:
    """Replace references with ``None`` so params can be schema-checked at save time."""
    if is_ref(value):
        return None
    if isinstance(value, dict):
        return {k: _placeholder_refs(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_placeholder_refs(v) for v in value]
    return value
