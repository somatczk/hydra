"""The trusted-proxy scheme fix: the 307 Location must say https behind Traefik's TLS.

uvicorn serves the engine with ``proxy_headers=False`` (flipping it would rewrite
``scope['client']`` and break the Remote-User proxy auth), so behind Traefik the ASGI
scheme stays ``http`` and starlette builds the trailing-slash 307 redirect Location as
``URL(scope=...)`` — leaking ``http://trading.romulus.hu/mcp/`` on an https site. The
``ProxySchemeMiddleware`` in ``security.py`` trusts one ``X-Forwarded-Proto`` header from
``api.trusted_proxy_cidrs`` peers only, never touches ``scope['client']``, and leaves
everything as it was on any other input.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from hydra.core.config import HydraConfig
from hydra.dashboard.api import app
from hydra.dashboard.mcp import mount_mcp

from .conftest import TEST_API_TOKEN

TRAEFIK = ("10.10.13.2", 41000)  # the pinned static Traefik address the /32 trusts
OUTSIDER = ("10.10.16.2", 41000)  # the test edge subnet, but outside the /32
MCP_TEST_TOKEN = "m" + secrets.token_urlsafe(48)
NO_SLASH_MCP = "/mcp"  # the recorded bug: the no-slash MCP URL redirect
PLAIN_API = "/api/strategies/indicators"  # an authenticated route that needs no DB
REDIRECTS = (NO_SLASH_MCP, PLAIN_API + "/")  # 307 probes: mount redirect + a plain /api one

Bind = Callable[..., HydraConfig]


@pytest.fixture()
def default_api_auth() -> None:
    """No default bearer: each request sends exactly the headers the test chooses."""


@pytest.fixture()
def trusted_proxy(bind_api_config: Bind) -> Iterator[HydraConfig]:
    """Proxy mode trusting exactly Traefik's /32, with the real MCP mount on the real app."""
    cfg = bind_api_config(
        HYDRA_API__AUTH_MODE="proxy",
        HYDRA_API__TRUSTED_PROXY_CIDRS="10.10.13.2/32",
        HYDRA_API_TOKEN=TEST_API_TOKEN,
        HYDRA_MCP_TOKEN=MCP_TEST_TOKEN,
        HYDRA_MCP__ALLOWED_HOSTS="testserver",
    )
    routes_before = list(app.router.routes)
    mount_mcp(app, cfg)
    yield cfg
    app.router.routes[:] = routes_before


def _redirect(
    client: TestClient, path: str, headers: dict[str, str] | list[tuple[str, str]] | None = None
) -> Response:
    return client.get(path, follow_redirects=False, headers=headers)


def _proxy_user(extra: dict[str, str] | None = None) -> dict[str, str]:
    return {"Remote-User": "soma", "X-Forwarded-Proto": "https", **(extra or {})}


def test_trusted_peer_redirects_say_https(trusted_proxy: HydraConfig) -> None:
    """X-Forwarded-Proto: https from the trusted peer fixes both 307 Locations."""
    client = TestClient(app, client=TRAEFIK)
    for path in REDIRECTS:
        resp = _redirect(client, path, _proxy_user())
        assert resp.status_code == 307, (path, resp.status_code)
        assert resp.headers["location"].startswith("https://"), (path, resp.headers["location"])


def test_untrusted_peer_gains_no_scheme(trusted_proxy: HydraConfig) -> None:
    """The same forwarded header from any other peer changes nothing."""
    client = TestClient(app, client=OUTSIDER)
    bearer = {"Authorization": f"Bearer {TEST_API_TOKEN}", "X-Forwarded-Proto": "https"}
    mcp = _redirect(client, NO_SLASH_MCP, {"X-Forwarded-Proto": "https"})  # no auth needed
    api = _redirect(client, PLAIN_API + "/", bearer)
    for resp in (mcp, api):
        assert resp.status_code == 307
        assert resp.headers["location"].startswith("http://")


@pytest.mark.parametrize("proto", ["ftp", "HTTPS", "https, http"])
def test_garbage_proto_leaves_the_scheme_alone(trusted_proxy: HydraConfig, proto: str) -> None:
    client = TestClient(app, client=TRAEFIK)
    for path in REDIRECTS:
        resp = _redirect(client, path, _proxy_user({"X-Forwarded-Proto": proto}))
        assert resp.status_code == 307, (path, resp.status_code)
        assert resp.headers["location"].startswith("http://"), (path, resp.headers["location"])


def test_trusted_peer_without_proto_header_stays_http(trusted_proxy: HydraConfig) -> None:
    """No ``X-Forwarded-Proto`` from the trusted peer: direct ``:8000`` traffic stays ``http``."""
    client = TestClient(app, client=TRAEFIK)
    resp = _redirect(client, NO_SLASH_MCP, {"Remote-User": "soma"})
    assert resp.status_code == 307
    assert resp.headers["location"].startswith("http://")


def test_duplicate_proto_from_the_trusted_peer_is_refused(trusted_proxy: HydraConfig) -> None:
    client = TestClient(app, client=TRAEFIK)
    resp: Response = client.get(
        PLAIN_API,
        headers=[
            ("Remote-User", "soma"),
            ("X-Forwarded-Proto", "https"),
            ("X-Forwarded-Proto", "http"),
        ],
    )
    assert resp.status_code == 400
    detail: dict[str, Any] = resp.json()["detail"]
    assert (detail["code"], detail["state"]) == ("duplicate_header", "invalid")


def test_duplicate_proto_from_an_untrusted_peer_is_just_untrusted(
    trusted_proxy: HydraConfig,
) -> None:
    """Outside the trust boundary the header never mattered: no new refusal surface."""
    client = TestClient(app, client=OUTSIDER)
    resp = client.get(
        PLAIN_API,
        headers=[("X-Forwarded-Proto", "https"), ("X-Forwarded-Proto", "http")],
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


def test_remote_user_still_resolves_through_the_scheme_fix(trusted_proxy: HydraConfig) -> None:
    """The middleware never touches scope['client']: the proxy principal still authenticates."""
    client = TestClient(app, client=TRAEFIK)
    resp = client.get(PLAIN_API, headers=_proxy_user())
    assert resp.status_code == 200
    assert resp.json()  # the route answered, so Remote-User was resolved from the raw peer
