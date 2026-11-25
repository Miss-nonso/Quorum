"""DAG validation, references, run status, JSON Schema subset, transforms."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from quorum.domain.dag import DagError, WorkflowDefinition, validate_definition
from quorum.domain.jsonschema import SchemaError, check_schema, validate
from quorum.domain.refs import RefError, find_refs, lookup, resolve_refs
from quorum.domain.run_state import (
    InvalidRunTransitionError,
    RunStatus,
    derive_run_status,
    ensure_run_transition,
)
from quorum.domain.task_state import TaskStatus
from quorum.domain.transform import TransformSpec, apply_transform
from quorum.samples import sample_definition
from quorum.tasks import builtin_registry
from quorum.tasks.pipeline import ReportParams, render_html, render_markdown


def wf(*nodes: dict[str, Any]) -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {"nodes": [{"task_type": "sleep", **n} for n in nodes]}
    )


# ------------------------------------------------------------------ DAG validation


def test_sample_workflow_is_valid_and_ordered() -> None:
    order = builtin_registry().validate_workflow(sample_definition("http://localhost/x"))
    assert order == ["fetch", "validate", "transform", "store", "report", "notify"]


def test_cycles_are_rejected_with_the_cycle_named() -> None:
    with pytest.raises(DagError, match=r"^cycle detected: a -> b -> c -> a$"):
        validate_definition(
            wf(
                {"key": "a", "depends_on": ["b"]},
                {"key": "b", "depends_on": ["c"]},
                {"key": "c", "depends_on": ["a"]},
            )
        )


def test_every_structural_problem_is_reported_at_once() -> None:
    with pytest.raises(DagError) as info:
        validate_definition(
            wf(
                {"key": "a", "depends_on": ["a"]},
                {"key": "b", "depends_on": ["ghost"]},
                {"key": "c", "depends_on": ["b", "b"]},
            )
        )
    assert len(info.value.problems) == 3


def test_duplicate_keys_are_rejected() -> None:
    with pytest.raises(DagError, match="duplicate node keys: a"):
        validate_definition(wf({"key": "a"}, {"key": "a"}))


@pytest.mark.parametrize("key", ["Fetch", "1st", "has-dash", "", "x" * 41])
def test_node_keys_must_be_simple_identifiers(key: str) -> None:
    with pytest.raises(ValueError, match="must match"):
        wf({"key": key})


def test_references_must_point_upstream() -> None:
    with pytest.raises(DagError, match="not upstream"):
        validate_definition(
            WorkflowDefinition.model_validate(
                {
                    "nodes": [
                        {"key": "a", "task_type": "transform", "params": {"input": {"$from": "b"}}},
                        {"key": "b", "task_type": "sleep"},
                    ]
                }
            )
        )


def test_references_to_unknown_nodes_are_rejected() -> None:
    definition = WorkflowDefinition.model_validate(
        {"nodes": [{"key": "a", "task_type": "transform", "params": {"input": {"$from": "zz"}}}]}
    )
    with pytest.raises(DagError, match="unknown node 'zz'"):
        validate_definition(definition)


def test_bad_params_and_unknown_types_are_rejected_at_save_time() -> None:
    definition = WorkflowDefinition.model_validate(
        {
            "nodes": [
                {"key": "a", "task_type": "sleep", "params": {"seconds": -5}},
                {"key": "b", "task_type": "exec_shell"},
            ]
        }
    )
    with pytest.raises(DagError) as info:
        builtin_registry().validate_workflow(definition)
    assert any(p.startswith("a: invalid params") for p in info.value.problems)
    assert any("unknown task type 'exec_shell'" in p for p in info.value.problems)


def test_node_limit_is_enforced() -> None:
    with pytest.raises(ValueError, match="at most 50"):
        wf(*({"key": f"n{i}"} for i in range(51)))


@st.composite
def random_dags(draw: st.DrawFn) -> WorkflowDefinition:
    """Random DAGs: each node may only depend on earlier nodes, so no cycles."""
    size = draw(st.integers(min_value=1, max_value=25))
    nodes = []
    for i in range(size):
        deps = draw(st.sets(st.integers(min_value=0, max_value=i - 1), max_size=3)) if i else set()
        nodes.append({"key": f"n{i}", "depends_on": [f"n{d}" for d in sorted(deps)]})
    order = draw(st.permutations(nodes))  # declaration order must not matter
    return wf(*order)


@given(random_dags())
def test_topological_order_respects_every_dependency(definition: WorkflowDefinition) -> None:
    order = validate_definition(definition)
    position = {key: i for i, key in enumerate(order)}
    assert sorted(order) == sorted(n.key for n in definition.nodes)
    for node in definition.nodes:
        for dep in node.depends_on:
            assert position[dep] < position[node.key]


@given(random_dags(), st.data())
def test_adding_a_back_edge_is_always_detected(
    definition: WorkflowDefinition, data: st.DataObject
) -> None:
    order = validate_definition(definition)
    if len(order) < 2:
        return
    # Pick an edge-closing pair: make an ancestor depend on a descendant reachable from it.
    with_deps = [n for n in definition.nodes if n.depends_on]
    if not with_deps:
        return
    child = data.draw(st.sampled_from(with_deps))
    parent_key = child.depends_on[0]
    nodes = [
        {**n.model_dump(), "depends_on": [*n.depends_on, child.key]}
        if n.key == parent_key
        else n.model_dump()
        for n in definition.nodes
    ]
    with pytest.raises(DagError, match="cycle"):
        validate_definition(WorkflowDefinition.model_validate({"nodes": nodes}))


# ------------------------------------------------------------------ references


def test_find_and_resolve_nested_references() -> None:
    params = {"a": {"$from": "x", "path": "items.1"}, "b": [{"$from": "y"}], "c": 3}
    assert {(r.node, r.path) for r in find_refs(params)} == {("x", "items.1"), ("y", "")}
    resolved = resolve_refs(params, {"x": {"items": [10, 20]}, "y": {"ok": True}})
    assert resolved == {"a": 20, "b": [{"ok": True}], "c": 3}


def test_objects_with_extra_keys_are_not_references() -> None:
    value = {"$from": "x", "path": "", "other": 1}
    assert list(find_refs(value)) == []
    assert resolve_refs(value, {}) == value


@pytest.mark.parametrize(
    ("results", "message"),
    [({}, "no result from upstream node 'x'"), ({"x": {"a": 1}}, "'b' not found")],
)
def test_unresolvable_references_raise(results: dict[str, Any], message: str) -> None:
    with pytest.raises(RefError, match=message):
        resolve_refs({"$from": "x", "path": "b"}, results)


def test_lookup_handles_indices_and_bounds() -> None:
    assert lookup({"a": [1, 2, 3]}, "a.-1") == 3
    with pytest.raises(RefError, match="out of range"):
        lookup({"a": []}, "a.0")


# ------------------------------------------------------------------ run status


def counts(**by_status: int) -> dict[TaskStatus, int]:
    return {TaskStatus(k): v for k, v in by_status.items()}


@pytest.mark.parametrize(
    ("task_counts", "expected"),
    [
        (counts(ready=1, pending=3), RunStatus.RUNNING),
        (counts(running=1, succeeded=2), RunStatus.RUNNING),
        (counts(succeeded=6), RunStatus.SUCCEEDED),
        (counts(succeeded=2, dead=1, pending=3), RunStatus.FAILED),  # blocked behind a DLQ task
        (counts(succeeded=5, failed=1), RunStatus.FAILED),  # a `continue` node failed
        (counts(failed=1, cancelled=5), RunStatus.FAILED),  # fail_run
    ],
)
def test_run_status_follows_task_states(
    task_counts: dict[TaskStatus, int], expected: RunStatus
) -> None:
    assert derive_run_status(task_counts) is expected


def test_failed_runs_can_resume_but_finished_runs_cannot_change() -> None:
    ensure_run_transition(RunStatus.FAILED, RunStatus.RUNNING)
    with pytest.raises(InvalidRunTransitionError):
        ensure_run_transition(RunStatus.SUCCEEDED, RunStatus.RUNNING)
    with pytest.raises(InvalidRunTransitionError):
        ensure_run_transition(RunStatus.CANCELLED, RunStatus.RUNNING)


# ------------------------------------------------------------------ JSON Schema subset


@pytest.mark.parametrize(
    ("instance", "schema", "error"),
    [
        (5, {"type": "string"}, "expected string, got number"),
        (True, {"type": "integer"}, "expected integer, got boolean"),
        ({"a": 1}, {"required": ["b"]}, "missing required property 'b'"),
        ({"a": 1}, {"properties": {}, "additionalProperties": False}, "unexpected property 'a'"),
        ([1], {"minItems": 2}, "at least 2"),
        ("ab", {"pattern": "^a$"}, "does not match"),
        (3, {"exclusiveMaximum": 3}, "less than 3"),
        (1, {"enum": [True]}, "one of"),
        ({"a": [1, "x"]}, {"properties": {"a": {"items": {"type": "number"}}}}, "$.a[1]"),
    ],
)
def test_violations_are_reported_with_paths(
    instance: Any, schema: dict[str, Any], error: str
) -> None:
    check_schema(schema)
    errors = validate(instance, schema)
    assert errors
    assert error in errors[0]


def test_valid_instances_pass() -> None:
    from quorum.samples import DEMO_DATASET, ORDER_SCHEMA

    check_schema(ORDER_SCHEMA)
    assert validate(DEMO_DATASET, ORDER_SCHEMA) == []
    assert validate(2.0, {"type": "integer"}) == []


@pytest.mark.parametrize(
    "schema",
    [
        {"oneOf": []},
        {"type": "date"},
        {"minItems": -1},
        {"pattern": "("},
        {"additionalProperties": {}},
    ],
)
def test_unsupported_or_malformed_schemas_are_refused(schema: dict[str, Any]) -> None:
    with pytest.raises(SchemaError):
        check_schema(schema)


@given(
    st.recursive(
        st.none() | st.booleans() | st.floats(allow_nan=False) | st.text(),
        lambda c: st.lists(c) | st.dictionaries(st.text(), c),
        max_leaves=20,
    )
)
def test_the_empty_schema_accepts_everything(instance: Any) -> None:
    assert validate(instance, {}) == []


# ------------------------------------------------------------------ transforms

ROWS = [
    {"id": 1, "kind": "a", "score": 5, "tags": ["x"]},
    {"id": 2, "kind": "b", "score": 9},
    {"id": 3, "kind": "a", "score": None},
    {"id": 4, "kind": "a", "score": 7, "tags": ["x", "y"]},
]


def test_filter_project_sort_limit() -> None:
    spec = TransformSpec.model_validate(
        {
            "where": [{"field": "kind", "op": "eq", "value": "a"}],
            "select": {"ident": "id", "s": "score"},
            "sort_by": "score",
            "descending": True,
            "limit": 2,
        }
    )
    # Rows without a score sort last even when descending, so the limit drops them.
    assert apply_transform(ROWS, spec) == [{"ident": 4, "s": 7}, {"ident": 1, "s": 5}]
    out = apply_transform(ROWS, spec.model_copy(update={"descending": False}))
    assert out == [{"ident": 1, "s": 5}, {"ident": 4, "s": 7}]


@pytest.mark.parametrize(
    ("cond", "ids"),
    [
        ({"field": "score", "op": "gt", "value": 6}, [2, 4]),
        ({"field": "tags", "op": "contains", "value": "y"}, [4]),
        ({"field": "tags", "op": "exists"}, [1, 4]),
        ({"field": "tags", "op": "exists", "value": False}, [2, 3]),
        ({"field": "kind", "op": "in", "value": ["b"]}, [2]),
        ({"field": "score", "op": "lt", "value": "text"}, []),  # incomparable: no match, no crash
    ],
)
def test_conditions(cond: dict[str, Any], ids: list[int]) -> None:
    spec = TransformSpec.model_validate({"where": [cond]})
    assert [r["id"] for r in apply_transform(ROWS, spec)] == ids


@given(st.lists(st.dictionaries(st.sampled_from(["a", "b"]), st.integers()), max_size=30))
def test_filtering_never_invents_rows(rows: list[dict[str, int]]) -> None:
    spec = TransformSpec.model_validate({"where": [{"field": "a", "op": "gte", "value": 0}]})
    out = apply_transform(rows, spec)
    assert all(r in rows for r in out)
    assert len(out) == sum(1 for r in rows if r.get("a", -1) >= 0)


# ------------------------------------------------------------------ report rendering


def test_html_report_escapes_untrusted_data() -> None:
    params = ReportParams.model_validate(
        {
            "title": "<script>alert(1)</script>",
            "format": "html",
            "sections": [{"heading": "rows", "data": [{"name": "<img onerror=x>"}]}],
        }
    )
    rendered = render_html(params)
    assert "<script>" not in rendered
    assert "&lt;img onerror=x&gt;" in rendered


def test_markdown_report_renders_tables_and_scalars() -> None:
    params = ReportParams.model_validate(
        {
            "title": "T",
            "sections": [
                {"heading": "Count", "data": 3},
                {"heading": "Rows", "data": [{"a": 1, "b": "x|y"}]},
            ],
        }
    )
    md = render_markdown(params)
    assert "**3**" in md
    assert "| a | b |" in md
    assert "x\\|y" in md


@pytest.mark.parametrize(
    ("instance", "schema", "error"),
    [
        ([1, 2, 3], {"maxItems": 2}, "at most 2 items"),
        ("abc", {"minLength": 4}, "shorter than 4"),
        ("abcd", {"maxLength": 3}, "longer than 3"),
        (1, {"minimum": 2}, "less than minimum 2"),
        (3, {"maximum": 2}, "greater than maximum 2"),
        (2, {"exclusiveMinimum": 2}, "greater than 2"),
        ("x", {"const": "y"}, "must equal 'y'"),
        (None, {"type": ["string", "number"]}, "expected string or number, got null"),
        ({}, {"type": "array"}, "got object"),
        ([], {"type": "object"}, "got array"),
        ("s", {"type": "boolean"}, "got string"),
        (1.5, {"type": "null"}, "got number"),
        (True, {"const": 1}, "must equal 1"),  # JSON: true is not 1
    ],
)
def test_more_keyword_violations(instance: Any, schema: dict[str, Any], error: str) -> None:
    check_schema(schema)
    assert error in validate(instance, schema)[0]


@pytest.mark.parametrize(
    "schema",
    [
        "not-a-dict",
        {"type": []},
        {"enum": "x"},
        {"required": "id"},
        {"maximum": "10"},
        {"properties": []},
        {"properties": {"a": {"type": "nope"}}},
        {"items": {"oneOf": []}},
    ],
)
def test_more_malformed_schemas(schema: Any) -> None:
    with pytest.raises(SchemaError):
        check_schema(schema)


def test_error_list_is_capped() -> None:
    errors = validate(list(range(500)), {"items": {"type": "string"}})
    assert len(errors) == 50
