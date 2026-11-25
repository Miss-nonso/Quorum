"""Data-pipeline task types: ``http_fetch``, ``validate``, ``transform``, ``store``, ``report``.

Inputs from upstream nodes arrive through param references
(``{"$from": "node", "path": "..."}``), resolved by the worker before the
handler runs, so handlers see plain values.
"""

from __future__ import annotations

import html
import json
from enum import StrEnum
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from quorum.domain.jsonschema import SchemaError, check_schema, validate
from quorum.domain.transform import Condition, TransformSpec, apply_transform
from quorum.worker.registry import JsonDict, PermanentTaskError, TaskContext, TaskType

_USER_AGENT = "Quorum/0.1 (+https://github.com/; workflow engine demo)"


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ------------------------------------------------------------------ http_fetch


class HttpFetchParams(_Params):
    url: str = Field(min_length=1, max_length=2000)


def _allowed(url: httpx.URL, allowlist: frozenset[str]) -> bool:
    host = (url.host or "").lower()
    return host in allowlist or f"{host}:{url.port}" in allowlist


async def _http_fetch(ctx: TaskContext, params: HttpFetchParams) -> JsonDict:
    try:
        url = httpx.URL(params.url)
    except httpx.InvalidURL as exc:
        raise PermanentTaskError(f"invalid URL: {exc}") from exc
    if url.scheme not in ("http", "https"):
        raise PermanentTaskError("only http and https URLs are allowed")
    if not _allowed(url, ctx.env.http_allowlist):
        raise PermanentTaskError(f"host {url.host!r} is not on the allowlist")

    limit = ctx.env.http_max_bytes
    # Redirects are not followed: a redirect could leave the allowlist.
    async with (
        httpx.AsyncClient(timeout=ctx.env.http_timeout_s, follow_redirects=False) as client,
        client.stream(
            "GET", url, headers={"Accept": "application/json", "User-Agent": _USER_AGENT}
        ) as response,
    ):
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > limit:
                raise PermanentTaskError(f"response exceeds the {limit}-byte limit")
    if response.status_code >= 500:
        # Transient by convention: raise a plain error so the attempt is retried.
        raise RuntimeError(f"upstream returned HTTP {response.status_code}")
    if response.status_code >= 300:
        raise PermanentTaskError(f"upstream returned HTTP {response.status_code}")

    content_type = response.headers.get("content-type", "")
    data: Any = None
    text_body: str | None = None
    if "json" in content_type:
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise PermanentTaskError(f"response is not valid JSON: {exc}") from exc
    else:
        text_body = body.decode(response.encoding or "utf-8", errors="replace")
    return {
        "status": response.status_code,
        "content_type": content_type,
        "bytes": len(body),
        "json": data,
        "text": text_body,
    }


HTTP_FETCH = TaskType(
    name="http_fetch",
    params_model=HttpFetchParams,
    handler=_http_fetch,
    description="GET a URL on the operator's allowlist (timeout, size cap, no redirects).",
)


# ------------------------------------------------------------------ validate


class ValidateParams(_Params):
    json_schema: dict[str, Any] = Field(alias="schema")
    data: Any = None

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @field_validator("json_schema")
    @classmethod
    def _supported(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            check_schema(value)
        except SchemaError as exc:
            raise ValueError(str(exc)) from None
        return value


async def _validate(ctx: TaskContext, params: ValidateParams) -> JsonDict:
    errors = validate(params.data, params.json_schema)
    if errors:
        # Bad data will not become good by retrying.
        raise PermanentTaskError(f"{len(errors)} validation error(s): " + "; ".join(errors[:10]))
    count = len(params.data) if isinstance(params.data, list) else 1
    return {"valid": True, "count": count, "data": params.data}


VALIDATE = TaskType(
    name="validate",
    params_model=ValidateParams,
    handler=_validate,
    description="Check data against a JSON Schema (supported subset) and pass it through.",
)


# ------------------------------------------------------------------ transform


class TransformParams(_Params):
    input: Any = None
    where: tuple[Condition, ...] = ()
    select: dict[str, str] | None = None
    sort_by: str | None = None
    descending: bool = False
    limit: int | None = Field(default=None, ge=0, le=10_000)


async def _transform(ctx: TaskContext, params: TransformParams) -> JsonDict:
    spec = TransformSpec(
        where=params.where,
        select=params.select,
        sort_by=params.sort_by,
        descending=params.descending,
        limit=params.limit,
    )
    items = apply_transform(params.input if params.input is not None else [], spec)
    return {"items": items, "count": len(items)}


TRANSFORM = TaskType(
    name="transform",
    params_model=TransformParams,
    handler=_transform,
    description="Filter, project, sort and limit records declaratively.",
)


# ------------------------------------------------------------------ store


class StoreParams(_Params):
    collection: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    records: Any = None


_STORE = text(
    """
    INSERT INTO stored_records (idempotency_key, run_id, task_id, collection, records, record_count)
    VALUES (:key, :run_id, :task_id, :collection, :records, :count)
    ON CONFLICT (idempotency_key) DO NOTHING
    RETURNING id
    """
).bindparams(bindparam("records", type_=JSONB))


async def _store(ctx: TaskContext, params: StoreParams) -> JsonDict:
    records = params.records if isinstance(params.records, list) else [params.records]
    async with ctx.engine.begin() as conn:
        inserted = (
            await conn.execute(
                _STORE,
                {
                    "key": ctx.idempotency_key,
                    "run_id": ctx.run_id,
                    "task_id": ctx.task_id,
                    "collection": params.collection,
                    "records": records,
                    "count": len(records),
                },
            )
        ).scalar_one_or_none()
    return {
        "collection": params.collection,
        "stored": len(records),
        "duplicate_suppressed": inserted is None,
    }


STORE = TaskType(
    name="store",
    params_model=StoreParams,
    handler=_store,
    description="Persist records to a demo collection, exactly once per idempotency key.",
    has_side_effects=True,
)


# ------------------------------------------------------------------ report


class ReportFormat(StrEnum):
    MARKDOWN = "markdown"
    HTML = "html"


class ReportSection(_Params):
    heading: str = Field(min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=5000)
    data: Any = None
    # Column order for tables. JSON objects stored as jsonb do not keep key
    # order, so without this the columns would appear in an arbitrary order.
    columns: tuple[str, ...] | None = Field(default=None, max_length=30)


class ReportParams(_Params):
    title: str = Field(min_length=1, max_length=200)
    format: ReportFormat = ReportFormat.MARKDOWN
    sections: tuple[ReportSection, ...] = Field(default=(), max_length=20)


_MAX_TABLE_ROWS = 50


def _cell(value: Any) -> str:
    if isinstance(value, dict | list):
        return json.dumps(value, separators=(",", ":"))[:80]
    return "" if value is None else str(value)


def _rows(
    data: Any, columns: tuple[str, ...] | None = None
) -> tuple[list[str], list[list[str]]] | None:
    """Tabulate a list of objects, or an object as key/value rows."""
    if isinstance(data, list) and data and all(isinstance(r, dict) for r in data):
        if columns is None:
            columns = tuple(sorted(dict.fromkeys(k for r in data for k in r)))
        rows = [[_cell(r.get(c)) for c in columns] for r in data[:_MAX_TABLE_ROWS]]
        return list(columns), rows
    if isinstance(data, dict):
        return ["key", "value"], [[str(k), _cell(v)] for k, v in data.items()]
    return None


def render_markdown(params: ReportParams) -> str:
    out = [f"# {params.title}", ""]
    for section in params.sections:
        out += [f"## {section.heading}", ""]
        if section.body:
            out += [section.body, ""]
        table = _rows(section.data, section.columns)
        if table is not None:
            columns, rows = table
            esc = [[c.replace("|", "\\|") for c in row] for row in [columns, *rows]]
            out.append("| " + " | ".join(esc[0]) + " |")
            out.append("|" + "---|" * len(columns))
            out += ["| " + " | ".join(row) + " |" for row in esc[1:]]
            out.append("")
        elif section.data is not None:
            out += [f"**{_cell(section.data)}**", ""]
    return "\n".join(out).rstrip() + "\n"


def render_html(params: ReportParams) -> str:
    e = html.escape
    out = [f"<article><h1>{e(params.title)}</h1>"]
    for section in params.sections:
        out.append(f"<section><h2>{e(section.heading)}</h2>")
        if section.body:
            out.append(f"<p>{e(section.body)}</p>")
        table = _rows(section.data, section.columns)
        if table is not None:
            columns, rows = table
            head = "".join(f'<th scope="col">{e(c)}</th>' for c in columns)
            body = "".join("<tr>" + "".join(f"<td>{e(v)}</td>" for v in r) + "</tr>" for r in rows)
            out.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
        elif section.data is not None:
            out.append(f"<p><strong>{e(_cell(section.data))}</strong></p>")
        out.append("</section>")
    out.append("</article>")
    return "".join(out)


async def _report(ctx: TaskContext, params: ReportParams) -> JsonDict:
    content = render_html(params) if params.format is ReportFormat.HTML else render_markdown(params)
    return {"format": params.format.value, "content": content, "bytes": len(content.encode())}


REPORT = TaskType(
    name="report",
    params_model=ReportParams,
    handler=_report,
    description="Render a Markdown or HTML summary from upstream results (HTML is escaped).",
)
