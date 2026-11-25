"""A small, strict subset of JSON Schema, used by the ``validate`` task.

Supported keywords: ``type`` (string or list), ``enum``, ``const``,
``properties``, ``required``, ``additionalProperties`` (boolean),
``items`` (single schema), ``minItems``, ``maxItems``, ``minLength``,
``maxLength``, ``pattern``, ``minimum``, ``maximum``, ``exclusiveMinimum``,
``exclusiveMaximum``. Any other keyword is rejected when the schema is
checked, so a schema never silently means less than its author intended.

Written in-house to avoid a dependency (see ADR 0008). Booleans are not
numbers here, matching the JSON Schema specification rather than Python.
"""

from __future__ import annotations

import math
import re
from typing import Any

SUPPORTED = frozenset(
    {
        "type",
        "enum",
        "const",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "title",
        "description",
    }
)
TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})
MAX_ERRORS = 50


class SchemaError(ValueError):
    """The schema itself is malformed or uses an unsupported keyword."""


def check_schema(schema: Any, path: str = "$") -> None:
    """Raise :class:`SchemaError` unless ``schema`` is a well-formed supported schema."""
    if not isinstance(schema, dict):
        raise SchemaError(f"{path}: schema must be an object")
    unknown = set(schema) - SUPPORTED
    if unknown:
        raise SchemaError(f"{path}: unsupported keyword(s): {', '.join(sorted(unknown))}")
    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not names or not all(isinstance(n, str) and n in TYPES for n in names):
            raise SchemaError(f"{path}: invalid type {declared!r}")
    if "enum" in schema and not isinstance(schema["enum"], list):
        raise SchemaError(f"{path}: enum must be a list")
    if "required" in schema and not (
        isinstance(schema["required"], list) and all(isinstance(r, str) for r in schema["required"])
    ):
        raise SchemaError(f"{path}: required must be a list of strings")
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        raise SchemaError(f"{path}: additionalProperties must be a boolean in this subset")
    for key in ("minItems", "maxItems", "minLength", "maxLength"):
        if key in schema and not (_is_int(schema[key]) and schema[key] >= 0):
            raise SchemaError(f"{path}: {key} must be a non-negative integer")
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if key in schema and not _is_number(schema[key]):
            raise SchemaError(f"{path}: {key} must be a number")
    if "pattern" in schema:
        try:
            re.compile(schema["pattern"])
        except (re.error, TypeError) as exc:
            raise SchemaError(f"{path}: invalid pattern: {exc}") from None
    props = schema.get("properties", {})
    if not isinstance(props, dict):
        raise SchemaError(f"{path}: properties must be an object")
    for name, sub in props.items():
        check_schema(sub, f"{path}.properties.{name}")
    if "items" in schema:
        check_schema(schema["items"], f"{path}.items")


def validate(instance: Any, schema: dict[str, Any]) -> list[str]:
    """Return a list of human-readable violations (empty means valid)."""
    errors: list[str] = []
    _validate(instance, schema, "$", errors)
    return errors[:MAX_ERRORS]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and not (isinstance(value, float) and not math.isfinite(value))
    )


def _type_matches(value: Any, name: str) -> bool:
    match name:
        case "object":
            return isinstance(value, dict)
        case "array":
            return isinstance(value, list)
        case "string":
            return isinstance(value, str)
        case "integer":
            return _is_int(value) or (isinstance(value, float) and value.is_integer())
        case "number":
            return _is_number(value)
        case "boolean":
            return isinstance(value, bool)
        case "null":
            return value is None
    return False  # pragma: no cover - check_schema rejects unknown names


def _validate(value: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    if len(errors) >= MAX_ERRORS:
        return
    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not any(_type_matches(value, n) for n in names):
            errors.append(f"{path}: expected {' or '.join(names)}, got {_json_type(value)}")
            return
    if "const" in schema and not _equal(value, schema["const"]):
        errors.append(f"{path}: must equal {schema['const']!r}")
    if "enum" in schema and not any(_equal(value, option) for option in schema["enum"]):
        errors.append(f"{path}: must be one of {schema['enum']!r}")

    if isinstance(value, dict):
        props: dict[str, Any] = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}: missing required property {name!r}")
        for name, sub_value in value.items():
            if name in props:
                _validate(sub_value, props[name], f"{path}.{name}", errors)
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected property {name!r}")
    elif isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: allows at most {schema['maxItems']} items")
        if "items" in schema:
            for index, item in enumerate(value):
                _validate(item, schema["items"], f"{path}[{index}]", errors)
    elif isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: does not match pattern {schema['pattern']!r}")
    elif _is_number(value):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: less than minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: greater than maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: must be greater than {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: must be less than {schema['exclusiveMaximum']}")


def _equal(a: Any, b: Any) -> bool:
    # JSON equality: true != 1, unlike Python.
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return bool(a == b)


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__
