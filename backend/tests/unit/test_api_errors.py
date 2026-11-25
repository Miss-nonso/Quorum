"""The API's error envelope and request-ID behaviour, without a database."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Query

from quorum.api.app import create_app
from quorum.api.errors import ApiError
from quorum.core.config import Settings
from tests.conftest import make_client


def _assert_envelope(response: httpx.Response, status: int, code: str) -> dict[str, object]:
    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert error["message"]
    assert error["request_id"] == response.headers["x-request-id"]
    return error  # type: ignore[no-any-return]


@pytest.fixture
def app_with_test_routes(offline_settings: Settings) -> FastAPI:
    app = create_app(offline_settings)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    @app.get("/conflict")
    async def conflict() -> None:
        raise ApiError(409, "already_exists", "Workflow exists.", details={"id": "wf_1"})

    @app.get("/typed")
    async def typed(limit: int = Query(ge=1)) -> dict[str, int]:
        return {"limit": limit}

    return app


async def test_unknown_route_returns_not_found_envelope(
    offline_client: httpx.AsyncClient,
) -> None:
    _assert_envelope(await offline_client.get("/api/v1/nope"), 404, "not_found")


async def test_wrong_method_keeps_allow_header(offline_client: httpx.AsyncClient) -> None:
    response = await offline_client.post("/api/v1/health")
    _assert_envelope(response, 405, "method_not_allowed")
    assert response.headers["allow"] == "GET"


async def test_api_error_carries_code_and_details(app_with_test_routes: FastAPI) -> None:
    async with make_client(app_with_test_routes) as client:
        error = _assert_envelope(await client.get("/conflict"), 409, "already_exists")
    assert error["details"] == {"id": "wf_1"}


async def test_validation_error_lists_offending_fields(app_with_test_routes: FastAPI) -> None:
    async with make_client(app_with_test_routes) as client:
        error = _assert_envelope(await client.get("/typed?limit=0"), 422, "validation_error")
    assert error["details"] == [
        {
            "loc": ["query", "limit"],
            "msg": "Input should be greater than or equal to 1",
            "type": "greater_than_equal",
        }
    ]


async def test_unhandled_error_is_generic_and_does_not_leak(
    app_with_test_routes: FastAPI,
) -> None:
    async with make_client(app_with_test_routes) as client:
        response = await client.get("/boom")
    _assert_envelope(response, 500, "internal_error")
    assert "secret" not in response.text


async def test_request_id_is_generated_when_absent(offline_client: httpx.AsyncClient) -> None:
    first = await offline_client.get("/api/v1/nope")
    second = await offline_client.get("/api/v1/nope")
    assert len(first.headers["x-request-id"]) == 32
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


async def test_valid_incoming_request_id_is_propagated(
    offline_client: httpx.AsyncClient,
) -> None:
    response = await offline_client.get("/api/v1/nope", headers={"X-Request-ID": "edge-abc.123"})
    assert response.headers["x-request-id"] == "edge-abc.123"
    assert response.json()["error"]["request_id"] == "edge-abc.123"


@pytest.mark.parametrize("bad_id", ["has space", "x" * 129, "line\tbreak", 'quote"d'])
async def test_unsafe_incoming_request_id_is_replaced(
    offline_client: httpx.AsyncClient, bad_id: str
) -> None:
    response = await offline_client.get("/api/v1/nope", headers={"X-Request-ID": bad_id})
    assert response.headers["x-request-id"] != bad_id
    assert len(response.headers["x-request-id"]) == 32


async def test_health_reports_unavailable_when_database_unreachable(
    offline_client: httpx.AsyncClient,
) -> None:
    response = await offline_client.get("/api/v1/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["checks"] == {"database": "unreachable"}


async def test_openapi_documents_error_envelope(offline_client: httpx.AsyncClient) -> None:
    schema = (await offline_client.get("/openapi.json")).json()
    assert "ErrorResponse" in schema["components"]["schemas"]
    assert "/api/v1/health" in schema["paths"]
