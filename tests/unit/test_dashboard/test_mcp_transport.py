"""The MCP transport over the mounted app: initialize, tools/list, the auth and host gates.

The positive tests run through a real FastAPI app assembled the way the engine assembles
it — the real SecurityMiddleware (so the ``/mcp`` bypass is on the request path), the real
mount and the real session manager entered by the app's lifespan — while the no-token case
runs against the real API app. Bearer failures are asserted on the shared error envelope.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from starlette.websockets import WebSocketDisconnect

from hydra.core.config import HydraConfig
from hydra.dashboard.api import app
from hydra.dashboard.mcp import mount_mcp
from hydra.dashboard.security import install_security

from .conftest import TEST_API_TOKEN

TEST_MCP_TOKEN = "m" + secrets.token_urlsafe(48)
ACCEPT_BOTH = "application/json, text/event-stream"
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "hydra-tests", "version": "0.0.0"},
    },
}

Bind = Callable[..., HydraConfig]


def _mounted_app(cfg: HydraConfig) -> FastAPI:
    """A FastAPI app with the security stack and the MCP mount, as the engine assembles it."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Iterator[None]:
        mount = mount_mcp(app, cfg)
        async with mount.session_manager.run():
            yield

    mcp_app = FastAPI(lifespan=lifespan)
    install_security(mcp_app)
    mcp_app.state.config = cfg
    return mcp_app


def _post_mcp(
    client: TestClient,
    payload: dict[str, Any],
    token: str | None = TEST_MCP_TOKEN,
    session_id: str | None = None,
) -> Response:
    headers: dict[str, str] = {"Content-Type": "application/json", "Accept": ACCEPT_BOTH}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if session_id is not None:
        headers["Mcp-Session-Id"] = session_id
    return client.post("/mcp/", json=payload, headers=headers)


def _sse_json(response: Response) -> Any:
    """The first JSON-RPC message framed in an SSE body (``event: message`` + ``data:``)."""
    assert response.headers["content-type"].startswith("text/event-stream")
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line.removeprefix("data:").strip())
    raise AssertionError(f"no data frame in SSE body: {response.text!r}")


@pytest.fixture()
def mcp_app(bind_api_config: Bind) -> FastAPI:
    cfg = bind_api_config(
        HYDRA_API_TOKEN=TEST_API_TOKEN,
        HYDRA_MCP_TOKEN=TEST_MCP_TOKEN,
        HYDRA_MCP__ALLOWED_HOSTS="testserver",
    )
    return _mounted_app(cfg)


def test_no_token_mounts_no_mcp_surface() -> None:
    """Without HYDRA_MCP_TOKEN nothing is mounted: /mcp 404s for an authenticated caller."""
    assert getattr(app.state, "mcp", None) is None
    client = TestClient(app)  # the conftest's default: the REST bearer service principal
    assert client.get("/mcp").status_code == 404
    assert client.post("/mcp/", json=INITIALIZE).status_code == 404


def test_initialize_returns_server_info_and_capabilities(mcp_app: FastAPI) -> None:
    with TestClient(mcp_app) as client:
        response = _post_mcp(client, INITIALIZE)
    assert response.status_code == 200
    body = _sse_json(response)
    assert body["jsonrpc"] == "2.0"
    result = body["result"]
    assert result["protocolVersion"] == "2025-11-25"
    assert result["serverInfo"] == {"name": "hydra", "version": app.version}
    assert "tools" in result["capabilities"]
    assert response.headers["mcp-session-id"]


def test_tools_list_returns_the_registered_tools(mcp_app: FastAPI) -> None:
    """The handshake completes over the mounted app; the tool modules registered on it."""
    with TestClient(mcp_app) as client:
        initialized = _post_mcp(client, INITIALIZE)
        session_id = initialized.headers["mcp-session-id"]
        assert (
            _post_mcp(
                client,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                session_id=session_id,
            ).status_code
            == 202
        )
        tools = _post_mcp(
            client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session_id=session_id
        )
    assert tools.status_code == 200
    names = [tool["name"] for tool in _sse_json(tools)["result"]["tools"]]
    assert names  # the tool modules (B1/B2/P3-C) register on the shared server instance
    assert {"live_session_start", "live_order"} <= set(names)


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        pytest.param({}, "invalid_token", id="no-authorization"),
        pytest.param(
            {"Authorization": f"Bearer {TEST_API_TOKEN}"},
            "invalid_token",
            id="rest-api-token-is-not-the-mcp-token",
        ),
        pytest.param(
            {"Authorization": f"Bearer {'wrong' * 12}"}, "invalid_token", id="wrong-token"
        ),
        pytest.param({"Authorization": "Basic dXNlcjpwYXNz"}, "invalid_token", id="non-bearer"),
    ],
)
def test_bearer_failures_are_401_envelopes(
    mcp_app: FastAPI, headers: dict[str, str], code: str
) -> None:
    with TestClient(mcp_app) as client:
        response = client.post(
            "/mcp/",
            json=INITIALIZE,
            headers={**headers, "Content-Type": "application/json", "Accept": ACCEPT_BOTH},
        )
    assert response.status_code == 401
    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["state"] == "unauthenticated"
    assert response.headers["www-authenticate"] == "Bearer"


def test_duplicate_authorization_header_is_400(mcp_app: FastAPI) -> None:
    """Two Authorization headers must not reach the SDK's single-value parsing (A6)."""
    with TestClient(mcp_app) as client:
        response = client.post(
            "/mcp/",
            json=INITIALIZE,
            headers=[
                ("Content-Type", "application/json"),
                ("Accept", ACCEPT_BOTH),
                ("Authorization", f"Bearer {TEST_MCP_TOKEN}"),
                ("Authorization", f"Bearer {TEST_MCP_TOKEN}"),
            ],
        )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert (detail["code"], detail["state"]) == ("duplicate_header", "invalid")


def test_disallowed_host_is_421(bind_api_config: Bind) -> None:
    """The SDK's DNS-rebinding protection: a Host outside allowed_hosts is misdirected."""
    cfg = bind_api_config(
        HYDRA_API_TOKEN=TEST_API_TOKEN,
        HYDRA_MCP_TOKEN=TEST_MCP_TOKEN,
        HYDRA_MCP__ALLOWED_HOSTS="other.example",
    )
    with TestClient(_mounted_app(cfg)) as client:  # TestClient sends Host: testserver
        response = _post_mcp(client, INITIALIZE)
    assert response.status_code == 421


def test_mount_without_config_refuses_everything(bind_api_config: Bind) -> None:
    """No config on the app: the gate stays closed (auth_not_configured)."""
    cfg = bind_api_config(HYDRA_MCP_TOKEN=TEST_MCP_TOKEN, HYDRA_MCP__ALLOWED_HOSTS="testserver")
    unconfigured = _mounted_app(cfg)
    unconfigured.state.config = None
    with TestClient(unconfigured) as client:
        response = _post_mcp(client, INITIALIZE)
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "auth_not_configured"


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(None, id="no-token"),
        pytest.param("wrong" * 12, id="wrong-token"),
        pytest.param(TEST_MCP_TOKEN, id="valid-token"),
    ],
)
def test_websocket_upgrade_is_refused_by_the_gate(mcp_app: FastAPI, token: str | None) -> None:
    """The mount is HTTP-only: the gate closes every websocket upgrade itself, never forwards."""
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    with (
        TestClient(mcp_app) as client,
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect("/mcp/", headers=headers),
    ):
        pytest.fail("the mount must not accept a websocket upgrade")
    assert exc_info.value.code == 4403
    assert exc_info.value.reason == "websocket_not_supported"
