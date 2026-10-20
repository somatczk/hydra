"""Every route of the real app refuses an anonymous caller, except the explicit public list.

Routes come from ``app.openapi()``: in FastAPI 0.146 ``app.routes`` holds lazy included-router
wrappers, so iterating it would check almost nothing.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable

import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.routing import WebSocketRoute
from starlette.testclient import WebSocketDenialResponse
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus
from websockets.typing import Origin

from hydra.core.config import HydraConfig
from hydra.dashboard.api import app
from hydra.dashboard.security import PUBLIC_PATHS

from .conftest import TEST_API_TOKEN

EXPECTED_PUBLIC = {"/health", "/livez", "/readyz", "/metrics"}
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
            # probes answer 503 without a lifespan; public means never 401
            assert resp.status_code in (200, 503), (method, path)
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
def test_every_websocket_upgrade_is_denied_401_without_credentials(path: str) -> None:
    with (
        pytest.raises(WebSocketDenialResponse) as exc,
        TestClient(app).websocket_connect(path, headers={"Origin": "http://localhost:3000"}),
    ):
        pass
    assert exc.value.status_code == 401
    assert exc.value.headers["www-authenticate"] == "Bearer"
    assert exc.value.json()["detail"]["code"] == "unauthenticated"


@pytest.fixture()
async def live_server() -> AsyncIterator[str]:
    """The real app under real uvicorn, with the image's proxy_headers=False."""
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_config=None, proxy_headers=False, lifespan="off"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@pytest.mark.parametrize(
    ("env", "origin", "status", "code"),
    [
        pytest.param({}, "http://localhost:3000", 401, "unauthenticated", id="anonymous"),
        pytest.param(
            {"HYDRA_API__AUTH_MODE": "dev"},
            "http://evil.example",
            403,
            "ws_origin_rejected",
            id="foreign-origin",
        ),
        pytest.param(
            {"HYDRA_API__AUTH_MODE": "dev"}, None, 403, "ws_origin_rejected", id="no-origin"
        ),
    ],
)
async def test_real_server_denies_the_upgrade_with_the_envelope(
    bind_api_config: Bind,
    live_server: str,
    env: dict[str, str],
    origin: str | None,
    status: int,
    code: str,
) -> None:
    if env:
        bind_api_config(**env)
    with pytest.raises(InvalidStatus) as exc:
        async with connect(f"{live_server}/ws/market", origin=Origin(origin) if origin else None):
            pass
    response = exc.value.response
    assert response.status_code == status
    assert json.loads(response.body)["detail"]["code"] == code


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
