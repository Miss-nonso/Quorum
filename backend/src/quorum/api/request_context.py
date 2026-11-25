"""Request IDs and access logging, as a pure ASGI middleware.

A client may send ``X-Request-ID`` (useful for tracing a request through a
proxy); otherwise one is generated. The ID is stored in the ASGI scope so it
survives into exception handlers that run outside this middleware, is bound
to the log context for the duration of the request, and is echoed back on
the response.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from quorum.core.logging import bind_log_context

REQUEST_ID_HEADER = "X-Request-ID"
_SCOPE_KEY = "quorum.request_id"
# Accept caller-supplied IDs only if they are short and log-safe.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

access_logger = logging.getLogger("quorum.access")


def request_id_of(scope: Scope) -> str | None:
    """Return the request ID assigned to ``scope`` by the middleware, if any."""
    value = scope.get(_SCOPE_KEY)
    return value if isinstance(value, str) else None


def _incoming_request_id(scope: Scope) -> str | None:
    for name, value in scope.get("headers", ()):
        if name == b"x-request-id":
            candidate: str = value.decode("latin-1")
            return candidate if _VALID_REQUEST_ID.match(candidate) else None
    return None


class RequestContextMiddleware:
    """Assign a request ID, bind it to logs, and write one access log line."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or uuid.uuid4().hex
        scope[_SCOPE_KEY] = request_id
        status = 500
        started = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers: list[Any] = list(message.get("headers", []))
                if not any(k.lower() == b"x-request-id" for k, _ in headers):
                    headers.append((b"x-request-id", request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        with bind_log_context(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_id)
            finally:
                access_logger.info(
                    "request",
                    extra={
                        "method": scope["method"],
                        "path": scope["path"],
                        "status": status,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    },
                )
