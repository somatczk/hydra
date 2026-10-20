"""Every route of the real app refuses an anonymous caller, except the explicit public list.

Routes come from ``app.openapi()``: in FastAPI 0.146 ``app.routes`` holds lazy included-router
wrappers, so iterating it would check almost nothing.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from hydra.core.config import HydraConfig
from hydra.dashboard.api import app
from hydra.dashboard.security import PUBLIC_PATHS

from .conftest import TEST_API_TOKEN

EXPECTED_PUBLIC = {"/health", "/metrics"}
DOC_PATHS = ["/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"]
EXPECTED_WS = {"/ws/market", "/ws/trades", "/ws/portfolio", "/ws/signals", "/ws/risk"}
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}

Bind = Callable[..., HydraConfig]


@pytest.fixture()
def default_api_auth() -> None:
    """Anonymous callers only."""


@pytest.fixture(autouse=True)
def _proxy_mode(bind_api_config: Bind) -> None:
    bind_api_config(
        HYDRA_API__AUTH_MODE="proxy",
        HYDRA_API__TRUSTED_PROXY_CIDRS="10.10.16.0/24",
        HYDRA_API_TOKEN=TEST_API_TOKEN,
    )


def _operations() -> list[tuple[str, str]]:
    ops = []
    for path, item in app.openapi()["paths"].items():
        concrete = re.sub(r"\{[^}]+\}", "x", path)
        ops.extend((method.upper(), concrete) for method in item if method in HTTP_METHODS)
    return ops


def test_public_paths_are_exactly_the_reviewed_list() -> None:
    assert set(PUBLIC_PATHS) == EXPECTED_PUBLIC


def test_every_operation_needs_credentials() -> None:
    client = TestClient(app, client=("10.10.16.2", 41000))
    ops = _operations()
    assert len({path for _, path in ops}) >= 70, "route enumeration found too few paths"
    unprotected = []
    for method, path in ops:
        resp = client.request(method, path, headers={"X-Hydra-Request": "1"})
        if path in EXPECTED_PUBLIC:
            assert resp.status_code == 200, (method, path)
            continue
        body = resp.json() if resp.headers.get("content-type") == "application/json" else None
        if resp.status_code != 401 or body["detail"]["code"] != "unauthenticated":
            unprotected.append((method, path, resp.status_code))
    assert unprotected == []


@pytest.mark.parametrize("path", DOC_PATHS)
def test_api_docs_need_credentials(path: str) -> None:
    assert TestClient(app).get(path).status_code == 401


def test_unknown_paths_are_401_not_404() -> None:
    assert TestClient(app).get("/api/does-not-exist").status_code == 401


def test_websocket_routes_are_the_known_five() -> None:
    found = {r.path for r in app.router.routes if isinstance(r, WebSocketRoute)}
    assert found == EXPECTED_WS


@pytest.mark.parametrize("path", sorted(EXPECTED_WS))
def test_every_websocket_closes_4401_without_credentials(path: str) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        TestClient(app).websocket_connect(path, headers={"Origin": "http://localhost:3000"}),
    ):
        pass
    assert exc.value.code == 4401


def test_validation_errors_use_the_envelope_through_the_full_stack() -> None:
    resp = TestClient(app).post(
        "/api/trading/sessions",
        json={"trading_mode": "paper"},
        headers={"Authorization": f"Bearer {TEST_API_TOKEN}"},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert (detail["code"], detail["state"]) == ("validation_error", "invalid")
    assert detail["errors"][0]["loc"] == ["body", "strategy_id"]
