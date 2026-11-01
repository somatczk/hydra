"""Authentik access-token verification for the ``/mcp`` gate (OAuth 2.1 resource server).

The issuer's OIDC discovery document (whose ``issuer`` must equal ``mcp.oauth_issuer``) names
its ``jwks_uri``. The key set is fetched on first use, cached, and refetched at most once a
minute when a token names an unknown ``kid`` (key rotation); a failed fetch is
:class:`IssuerUnavailableError`, never a pass. A token passes only with an RS256 signature by a
signing key from that set, ``iss`` equal to ``mcp.oauth_issuer`` (exact string), ``aud`` holding
``mcp.oauth_audience``, ``exp``/``nbf`` live within a 30 s leeway, a ``sub``, and every
``mcp.required_scopes`` entry in its space-separated ``scope`` claim.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

import httpx
import jwt

from hydra.core.config import McpConfig

ALGORITHMS = ["RS256"]  # Authentik's default signing key is RSA; never none or HS*
LEEWAY_S = 30
REFRESH_MIN_S = 60.0
FETCH_TIMEOUT_S = 5.0


class InvalidTokenError(Exception):
    """The bearer token is not one this resource server accepts (a 401)."""


class IssuerUnavailableError(Exception):
    """The issuer's discovery document or key set could not be read (a 503)."""


class TokenVerifier:
    """Validates Authentik-signed JWT access tokens against one issuer and audience."""

    def __init__(self, mcp: McpConfig, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if mcp.oauth_issuer is None or mcp.oauth_audience is None:
            raise ValueError("TokenVerifier needs mcp.oauth_issuer and mcp.oauth_audience")
        self.issuer = mcp.oauth_issuer
        self._audience = mcp.oauth_audience
        self._scopes = frozenset(mcp.required_scopes)
        self._transport = transport
        self._keys: list[jwt.PyJWK] = []
        self._fetched_at = -math.inf
        self._lock = asyncio.Lock()

    async def verify(self, token: str) -> dict[str, Any]:
        """The token's claims, or :class:`InvalidTokenError` / :class:`IssuerUnavailableError`."""
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.InvalidTokenError as exc:
            raise InvalidTokenError("malformed token") from exc
        key = await self._signing_key(kid)
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key.key,
                algorithms=ALGORITHMS,
                audience=self._audience,
                issuer=self.issuer,
                leeway=LEEWAY_S,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.InvalidTokenError as exc:
            raise InvalidTokenError(str(exc)) from exc
        if not self._scopes <= set(str(claims.get("scope", "")).split()):
            raise InvalidTokenError("token lacks a required scope")
        return claims

    def _find(self, kid: object) -> jwt.PyJWK | None:
        return next((k for k in self._keys if kid is not None and k.key_id == kid), None)

    async def _signing_key(self, kid: object) -> jwt.PyJWK:
        key = self._find(kid)
        if key is None:
            async with self._lock:
                key = self._find(kid)
                if key is None and time.monotonic() - self._fetched_at >= REFRESH_MIN_S:
                    await self._refresh()
                    key = self._find(kid)
        if key is None:
            raise InvalidTokenError("token is signed by an unknown key")
        return key

    async def _refresh(self) -> None:
        discovery_url = f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=FETCH_TIMEOUT_S
            ) as client:
                discovery = (await client.get(discovery_url)).raise_for_status().json()
                if discovery.get("issuer") != self.issuer:
                    raise IssuerUnavailableError("discovery names a different issuer")
                jwks = (await client.get(discovery["jwks_uri"])).raise_for_status().json()
            keys = jwt.PyJWKSet.from_dict(jwks).keys
        except (
            httpx.HTTPError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            jwt.PyJWKError,
        ) as exc:
            raise IssuerUnavailableError(f"could not read the issuer's keys: {exc}") from exc
        self._keys = [k for k in keys if k.public_key_use in (None, "sig")]
        self._fetched_at = time.monotonic()
