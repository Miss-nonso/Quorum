"""The sample workflow every visitor sees first.

Six steps with a visible parallel branch::

    fetch ─► validate ─► transform ─┬─► store ──┐
                                    └─► report ─┴─► notify

It fetches a small dataset of orders, checks its shape, keeps the paid
orders over a threshold, then *in parallel* stores them and renders a
report, and finally posts a notification that names both results.
"""

from __future__ import annotations

from typing import Any

from quorum.domain.dag import WorkflowDefinition

SAMPLE_WORKFLOW_NAME = "order-pipeline"

ORDER_SCHEMA: dict[str, Any] = {
    "type": "array",
    "minItems": 1,
    "items": {
        "type": "object",
        "required": ["id", "customer", "total", "status"],
        "properties": {
            "id": {"type": "string", "minLength": 1},
            "customer": {"type": "string"},
            "total": {"type": "number", "minimum": 0},
            "status": {"type": "string", "enum": ["paid", "pending", "refunded"]},
        },
    },
}

# Served by the API at /api/v1/demo/dataset (and by a local server in tests).
DEMO_DATASET: list[dict[str, Any]] = [
    {"id": "ord-1001", "customer": "Ada", "total": 182.5, "status": "paid"},
    {"id": "ord-1002", "customer": "Grace", "total": 42.0, "status": "paid"},
    {"id": "ord-1003", "customer": "Linus", "total": 310.0, "status": "pending"},
    {"id": "ord-1004", "customer": "Barbara", "total": 99.99, "status": "paid"},
    {"id": "ord-1005", "customer": "Ken", "total": 720.0, "status": "paid"},
    {"id": "ord-1006", "customer": "Margaret", "total": 15.0, "status": "refunded"},
    {"id": "ord-1007", "customer": "Edsger", "total": 256.0, "status": "paid"},
    {"id": "ord-1008", "customer": "Frances", "total": 64.0, "status": "pending"},
]


def sample_definition(dataset_url: str) -> WorkflowDefinition:
    """The order pipeline, fetching its input from ``dataset_url``."""
    nodes: list[dict[str, Any]] = [
        {"key": "fetch", "task_type": "http_fetch", "params": {"url": dataset_url}},
        {
            "key": "validate",
            "task_type": "validate",
            "depends_on": ["fetch"],
            "params": {"schema": ORDER_SCHEMA, "data": {"$from": "fetch", "path": "json"}},
        },
        {
            "key": "transform",
            "task_type": "transform",
            "depends_on": ["validate"],
            "params": {
                "input": {"$from": "validate", "path": "data"},
                "where": [
                    {"field": "status", "op": "eq", "value": "paid"},
                    {"field": "total", "op": "gte", "value": 50},
                ],
                "select": {"order": "id", "customer": "customer", "total": "total"},
                "sort_by": "total",
                "descending": True,
            },
        },
        {
            "key": "store",
            "task_type": "store",
            "depends_on": ["transform"],
            "params": {
                "collection": "large-paid-orders",
                "records": {"$from": "transform", "path": "items"},
            },
        },
        {
            "key": "report",
            "task_type": "report",
            "depends_on": ["transform"],
            "params": {
                "title": "Large paid orders",
                "sections": [
                    {"heading": "Count", "data": {"$from": "transform", "path": "count"}},
                    {
                        "heading": "Orders",
                        "data": {"$from": "transform", "path": "items"},
                        "columns": ["order", "customer", "total"],
                    },
                ],
            },
        },
        {
            "key": "notify",
            "task_type": "notify",
            "depends_on": ["store", "report"],
            "params": {
                "channel": "orders",
                "message": "Stored ${stored} large paid orders; report is ${bytes} bytes.",
                "vars": {
                    "stored": {"$from": "store", "path": "stored"},
                    "bytes": {"$from": "report", "path": "bytes"},
                },
            },
        },
    ]
    return WorkflowDefinition.model_validate({"nodes": nodes})
