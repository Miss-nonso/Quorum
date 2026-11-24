"""Structured logging on top of the standard library.

Log records are rendered as one JSON object per line. Contextual identifiers
(request, run, task, worker) live in context variables and are attached to
every record emitted while they are bound, so call sites never have to pass
them around by hand.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from conductor.core.config import LogFormat

_log_context: ContextVar[dict[str, str]] = ContextVar("conductor_log_context", default={})  # noqa: B039

# Attributes present on every LogRecord; anything else was passed via ``extra=``.
_STANDARD_ATTRS = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


@contextmanager
def bind_log_context(**fields: str) -> Iterator[None]:
    """Attach ``fields`` to every log record emitted inside the block."""
    token = _log_context.set({**_log_context.get(), **fields})
    try:
        yield
    finally:
        _log_context.reset(token)


def current_log_context() -> dict[str, str]:
    """Return a copy of the identifiers currently bound to this context."""
    return dict(_log_context.get())


class JsonFormatter(logging.Formatter):
    """Render a record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **_log_context.get(),
        }
        payload.update({k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRS})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Human-friendly format for local debugging."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        ctx = _log_context.get()
        if ctx:
            line += " " + " ".join(f"{k}={v}" for k, v in ctx.items())
        return line


def configure_logging(level: str, fmt: LogFormat) -> None:
    """Route all logging (including uvicorn's) through one stdout handler."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt is LogFormat.JSON else TextFormatter())

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())

    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    # The API writes its own access log with request IDs and timings.
    logging.getLogger("uvicorn.access").disabled = True
