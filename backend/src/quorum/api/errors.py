"""One error shape for every failure the API returns.

Every non-2xx JSON response (except health probes) has the form::

    {"error": {"code": "not_found", "message": "...", "request_id": "...", "details": null}}

``code`` is a stable, machine-readable string; ``message`` is for humans and
may change. ``request_id`` matches the ``X-Request-ID`` response header and
the ``request_id`` field in server logs.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from quorum.api.request_context import REQUEST_ID_HEADER, request_id_of

logger = logging.getLogger(__name__)


class ErrorBody(BaseModel):
    """The ``error`` object inside an error response."""

    code: str
    message: str
    request_id: str | None
    details: Any = None


class ErrorResponse(BaseModel):
    """Envelope for every API error; referenced in the OpenAPI schema."""

    error: ErrorBody


class ApiError(Exception):
    """Raise from route handlers to return a structured error."""

    def __init__(self, status: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.headers: dict[str, str] = {}


def _code_for_status(status: int) -> str:
    """Derive a stable code from an HTTP status, e.g. 404 -> ``not_found``."""
    try:
        return HTTPStatus(status).phrase.lower().replace(" ", "_").replace("-", "_")
    except ValueError:
        return "error"


def error_response(
    request: Request, status: int, code: str, message: str, details: Any = None
) -> JSONResponse:
    """Build a JSON error response carrying the request's ID."""
    request_id = request_id_of(request.scope)
    body = ErrorResponse(
        error=ErrorBody(code=code, message=message, request_id=request_id, details=details)
    )
    headers = {REQUEST_ID_HEADER: request_id} if request_id else None
    return JSONResponse(body.model_dump(mode="json"), status_code=status, headers=headers)


async def _api_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)  # noqa: S101 - narrowing for mypy
    response = error_response(request, exc.status, exc.code, exc.message, exc.details)
    response.headers.update(exc.headers)
    return response


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101
    message = exc.detail if isinstance(exc.detail, str) else HTTPStatus(exc.status_code).phrase
    response = error_response(request, exc.status_code, _code_for_status(exc.status_code), message)
    if exc.headers:
        response.headers.update(exc.headers)
    return response


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    details = [
        {"loc": list(err.get("loc", ())), "msg": err.get("msg"), "type": err.get("type")}
        for err in exc.errors()
    ]
    return error_response(request, 422, "validation_error", "Request validation failed.", details)


async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error", exc_info=exc)
    return error_response(request, 500, "internal_error", "An unexpected error occurred.")


def install_error_handlers(app: FastAPI) -> None:
    """Register handlers so every error leaves the API in the shared shape."""
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unhandled_error)
