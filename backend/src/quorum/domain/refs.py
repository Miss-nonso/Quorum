"""References from one node's params to an upstream node's result.

A reference is a JSON object of exactly this shape::

    {"$from": "fetch", "path": "json.items"}

``path`` is dot-separated (list indices are numbers, e.g. ``items.0.id``) and
may be omitted to take the whole result. References are resolved by the
worker just before execution, from the results of already-succeeded nodes.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

REF_KEY = "$from"


@dataclass(frozen=True, slots=True)
class Ref:
    node: str
    path: str


class RefError(ValueError):
    """A reference could not be resolved."""


def is_ref(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and REF_KEY in value
        and set(value) <= {REF_KEY, "path"}
        and isinstance(value[REF_KEY], str)
        and isinstance(value.get("path", ""), str)
    )


def find_refs(value: Any) -> Iterator[Ref]:
    """Yield every reference nested anywhere in ``value``."""
    if is_ref(value):
        yield Ref(node=value[REF_KEY], path=value.get("path", ""))
    elif isinstance(value, dict):
        for item in value.values():
            yield from find_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from find_refs(item)


def lookup(document: Any, path: str) -> Any:
    """Follow a dotted ``path`` into ``document``."""
    current = document
    for part in (p for p in path.split(".") if p) if path else ():
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.lstrip("-").isdigit():
            index = int(part)
            if not -len(current) <= index < len(current):
                raise RefError(f"index {index} out of range at {part!r}")
            current = current[index]
        else:
            raise RefError(f"path segment {part!r} not found")
    return current


def resolve_refs(value: Any, results: Mapping[str, Any]) -> Any:
    """Return ``value`` with every reference replaced by the referenced data.

    Raises:
        RefError: an upstream result is missing or a path does not exist.
    """
    if is_ref(value):
        node = value[REF_KEY]
        if node not in results:
            raise RefError(f"no result from upstream node {node!r}")
        try:
            return lookup(results[node], value.get("path", ""))
        except RefError as exc:
            raise RefError(f"{node}.{value.get('path', '')}: {exc}") from None
    if isinstance(value, dict):
        return {k: resolve_refs(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_refs(v, results) for v in value]
    return value
