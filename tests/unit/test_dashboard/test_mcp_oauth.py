"""The MCP OAuth resource server: Authentik token checks and the service-principal invariant.

Tokens are signed with real RSA keys and verified against a JWKS served in-process by
``tests.fixtures.mcp_oauth.FakeIssuer`` (discovery + keys over ``httpx.MockTransport``).
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.types import Receive, Scope, Send

from hydra.core.config import McpConfig
from hydra.dashboard.mcp import tokens
from hydra.dashboard.mcp.auth import MCPAuthMiddleware
from hydra.dashboard.mcp.tokens import InvalidTokenError, IssuerUnavailableError, TokenVerifier
from hydra.dashboard.routes.activity import _decider
from hydra.dashboard.security import Principal
from tests.fixtures.mcp_oauth import (
    AUDIENCE,
    ISSUER,
    RESOURCE_URL,
    FakeIssuer,
    mint,
    new_key,
)


def _verifier(issuer: FakeIssuer, scopes: list[str] | None = None) -> TokenVerifier:
    cfg = McpConfig(
        oauth_issuer=ISSUER,
        oauth_audience=AUDIENCE,
        resource_url=RESOURCE_URL,
        allowed_hosts=["testserver"],
        required_scopes=scopes or [],
    )
    return TokenVerifier(cfg, issuer.transport)


async def test_a_good_token_passes_and_the_keys_are_read_once() -> None:
    issuer = FakeIssuer()
    verifier = _verifier(issuer)
    for _ in range(3):
        claims = await verifier.verify(mint())
    assert (claims["iss"], claims["aud"], claims["preferred_username"]) == (
        ISSUER,
        AUDIENCE,
        "soma",
    )
    assert issuer.jwks_reads == 1


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(lambda: mint(iss="https://auth.test/application/o/other/"), id="wrong-iss"),
        pytest.param(lambda: mint(iss=ISSUER.rstrip("/")), id="iss-without-trailing-slash"),
        pytest.param(lambda: mint(aud="some-other-client"), id="wrong-aud"),
        pytest.param(lambda: mint(exp=int(time.time()) - 120), id="expired"),
        pytest.param(lambda: mint(nbf=int(time.time()) + 600), id="not-yet-valid"),
        pytest.param(lambda: mint(exp=None), id="no-exp"),
        pytest.param(lambda: mint(sub=None), id="no-sub"),
        pytest.param(lambda: mint(key=new_key()), id="signed-by-another-key-same-kid"),
        pytest.param(lambda: mint(kid=None), id="no-kid"),
        pytest.param(lambda: mint(key="x" * 64, algorithm="HS256"), id="hs256"),
        pytest.param(lambda: mint(key=None, algorithm="none"), id="alg-none"),
        pytest.param(lambda: "not.a.jwt", id="malformed"),
    ],
)
async def test_bad_tokens_are_refused(token: Any) -> None:
    with pytest.raises(InvalidTokenError):
        await _verifier(FakeIssuer()).verify(token())


async def test_a_token_missing_a_required_scope_is_refused() -> None:
    verifier = _verifier(FakeIssuer(), scopes=["hydra:mcp"])
    with pytest.raises(InvalidTokenError, match="scope"):
        await verifier.verify(mint())
    assert (await verifier.verify(mint(scope="openid hydra:mcp")))["sub"]


async def test_an_unknown_kid_refetches_once_then_is_rate_limited() -> None:
    issuer = FakeIssuer()
    verifier = _verifier(issuer)
    await verifier.verify(mint())
    stranger = new_key()
    for _ in range(3):
        with pytest.raises(InvalidTokenError, match="unknown key"):
            await verifier.verify(mint(key=stranger, kid="rotated"))
    assert issuer.jwks_reads == 1  # the last read is under a minute old: no refetch


async def test_a_rotated_key_is_picked_up_after_the_refresh_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issuer = FakeIssuer()
    verifier = _verifier(issuer)
    await verifier.verify(mint())
    rotated = new_key()
    issuer.keys = {"rotated": rotated}
    monkeypatch.setattr(tokens, "REFRESH_MIN_S", 0.0)
    assert (await verifier.verify(mint(key=rotated, kid="rotated")))["sub"]
    assert issuer.jwks_reads == 2
    with pytest.raises(InvalidTokenError):
        await verifier.verify(mint())  # the old key left the set


async def test_an_unreachable_issuer_is_unavailable_never_a_pass() -> None:
    issuer = FakeIssuer()
    issuer.down = True
    with pytest.raises(IssuerUnavailableError):
        await _verifier(issuer).verify(mint())


async def test_a_discovery_document_for_another_issuer_is_refused() -> None:
    issuer = FakeIssuer()
    issuer.discovery_issuer = "https://evil.test/application/o/hydra-mcp/"
    with pytest.raises(IssuerUnavailableError, match="different issuer"):
        await _verifier(issuer).verify(mint())


async def test_an_encryption_key_never_verifies_a_signature() -> None:
    issuer = FakeIssuer()
    real = issuer.transport

    def enc_only(request: httpx.Request) -> httpx.Response:
        response = real.handle_request(request)
        if request.url.path.endswith("/jwks/"):
            body = response.json()
            return httpx.Response(200, json={"keys": [{**k, "use": "enc"} for k in body["keys"]]})
        return response

    issuer.transport = httpx.MockTransport(enc_only)
    with pytest.raises(InvalidTokenError, match="unknown key"):
        await _verifier(issuer).verify(mint())


async def test_an_oauth_caller_is_a_service_principal_that_cannot_approve() -> None:
    """Whoever signs in, the gate binds service:mcp, and the approve route refuses it (403)."""
    seen: list[Scope] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    gate = MCPAuthMiddleware(inner, _verifier(FakeIssuer()), "https://testserver/m")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gate), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/", headers={"Authorization": f"Bearer {mint(preferred_username='soma')}"}
        )
    assert response.status_code == 204
    [scope] = seen
    principal = scope["state"]["principal"]
    assert principal == Principal("service", "mcp")
    with pytest.raises(HTTPException) as refused:
        _decider(Request(scope))
    assert refused.value.status_code == 403
    assert refused.value.detail["code"] == "principal_not_allowed"
