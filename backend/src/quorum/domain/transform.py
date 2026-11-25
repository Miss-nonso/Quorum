"""Declarative record transformation for the ``transform`` task.

A transform is data, not code: filter rows by simple predicates, project and
rename fields, sort, and cap the output. That is enough for real pipelines
and keeps the public demo free of arbitrary code execution (ADR 0007).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from quorum.domain.refs import RefError, lookup

_MISSING = object()


class Op(StrEnum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    CONTAINS = "contains"
    EXISTS = "exists"


class Condition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str = Field(min_length=1, max_length=200)
    op: Op
    value: Any = None


class TransformSpec(BaseModel):
    """Filter, then project (``select`` maps output name to input path), then sort, then limit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    where: tuple[Condition, ...] = ()
    select: dict[str, str] | None = None
    sort_by: str | None = None
    descending: bool = False
    limit: int | None = Field(default=None, ge=0, le=10_000)


def _get(record: Any, path: str) -> Any:
    try:
        return lookup(record, path)
    except RefError:
        return _MISSING


def _matches(record: Any, cond: Condition) -> bool:
    actual = _get(record, cond.field)
    if cond.op is Op.EXISTS:
        return (actual is not _MISSING) == bool(cond.value if cond.value is not None else True)
    if actual is _MISSING:
        return False
    try:
        match cond.op:
            case Op.EQ:
                return bool(actual == cond.value)
            case Op.NE:
                return bool(actual != cond.value)
            case Op.GT:
                return bool(actual > cond.value)
            case Op.GTE:
                return bool(actual >= cond.value)
            case Op.LT:
                return bool(actual < cond.value)
            case Op.LTE:
                return bool(actual <= cond.value)
            case Op.IN:
                return isinstance(cond.value, list) and actual in cond.value
            case Op.CONTAINS:
                return isinstance(actual, str | list) and cond.value in actual
    except TypeError:
        return False  # incomparable types never match, rather than crash the task
    return False  # pragma: no cover - exhaustive over Op


def _sort_key(value: Any) -> tuple[int, Any]:
    # Numbers before strings; mixed types never raise.
    if isinstance(value, bool | int | float):
        return (0, float(value))
    return (1, str(value))


def apply_transform(records: Any, spec: TransformSpec) -> list[Any]:
    """Apply ``spec`` to a list of records (a single object is treated as one record)."""
    rows = records if isinstance(records, list) else [records]
    rows = [r for r in rows if all(_matches(r, c) for c in spec.where)]
    if spec.sort_by is not None:
        key = spec.sort_by
        # Rows without a value always sort last, whichever the direction.
        present = [r for r in rows if _get(r, key) not in (_MISSING, None)]
        absent = [r for r in rows if _get(r, key) in (_MISSING, None)]
        present.sort(key=lambda r: _sort_key(_get(r, key)), reverse=spec.descending)
        rows = present + absent
    if spec.select is not None:
        projected = []
        for r in rows:
            out = {}
            for name, path in spec.select.items():
                value = _get(r, path)
                out[name] = None if value is _MISSING else value
            projected.append(out)
        rows = projected
    if spec.limit is not None:
        rows = rows[: spec.limit]
    return rows
