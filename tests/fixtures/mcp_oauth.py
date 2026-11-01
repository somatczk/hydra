"""An in-process Authentik stand-in for the MCP OAuth gate: RSA keys, their JWKS, signed tokens.

``FakeIssuer`` answers the issuer's OIDC discovery document and its JWKS over an
``httpx.MockTransport`` (no network); ``mint`` signs an access token the way Authentik does
(RS256, ``kid`` header, ``iss``/``aud``/``sub``/``exp``/``scope``). ``OAUTH_ENV`` is the
config that mounts ``/mcp`` for these tokens on ``testserver``; ``TOKEN`` is a valid token
for the whole test run.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ISSUER = "https://auth.test/application/o/hydra-mcp/"
AUDIENCE = "hydra-mcp-test-client"
RESOURCE_URL = "https://testserver/mcp"
KID = "hydra-test-key-1"
OAUTH_ENV = {
    "HYDRA_MCP__OAUTH_ISSUER": ISSUER,
    "HYDRA_MCP__OAUTH_AUDIENCE": AUDIENCE,
    "HYDRA_MCP__RESOURCE_URL": RESOURCE_URL,
    "HYDRA_MCP__ALLOWED_HOSTS": "testserver",
}


def new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


KEY = new_key()


def mint(*, key: Any = KEY, kid: str | None = KID, algorithm: str = "RS256", **claims: Any) -> str:
    """A signed access token; a claim passed as ``None`` is left out."""
    now = int(time.time())
    body: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "8f1c2d",
        "preferred_username": "soma",
        "iat": now,
        "exp": now + 3600,
        "scope": "openid profile email",
    }
    body.update(claims)
    body = {name: value for name, value in body.items() if value is not None}
    headers = {"kid": kid} if kid is not None else None
    return jwt.encode(body, key, algorithm=algorithm, headers=headers)


TOKEN = mint(exp=int(time.time()) + 24 * 3600)


class FakeIssuer:
    """Serves discovery and a JWKS of ``keys`` (kid -> private key); counts JWKS reads."""

    def __init__(self, keys: dict[str, rsa.RSAPrivateKey] | None = None) -> None:
        self.keys = dict(keys or {KID: KEY})
        self.jwks_reads = 0
        self.down = False
        self.discovery_issuer = ISSUER
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("issuer is down", request=request)
        if request.url == httpx.URL(f"{ISSUER}.well-known/openid-configuration"):
            return httpx.Response(
                200, json={"issuer": self.discovery_issuer, "jwks_uri": f"{ISSUER}jwks/"}
            )
        if request.url == httpx.URL(f"{ISSUER}jwks/"):
            self.jwks_reads += 1
            return httpx.Response(200, json={"keys": [_jwk(k, v) for k, v in self.keys.items()]})
        return httpx.Response(404)


def _jwk(kid: str, key: rsa.RSAPrivateKey) -> dict[str, Any]:
    jwk: dict[str, Any] = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    return {**jwk, "kid": kid, "use": "sig", "alg": "RS256"}


def serve_lifespan_issuer(monkeypatch: pytest.MonkeyPatch, issuer: FakeIssuer) -> None:
    """Route the lifespan's ``mount_mcp`` through ``issuer`` (it imports the name at startup)."""
    import hydra.dashboard.mcp as mcp_pkg

    real = mcp_pkg.mount_mcp
    monkeypatch.setattr(
        mcp_pkg, "mount_mcp", lambda app, cfg: real(app, cfg, transport=issuer.transport)
    )
