"""OAuth bearer gate for the ``/mcp`` mount: pure ASGI, fail-closed, deliberately no CSRF.

The MCP surface is non-browser: the client (Claude Code, a claude.ai connector) signs in
through Authentik and sends its access token as ``Authorization: Bearer``, so the token itself
is the defense: there is no CSRF check and no Origin check (the SDK's
``TransportSecuritySettings`` still validates the ``Host`` header against
``mcp.allowed_hosts``). Each token is checked by ``tokens.TokenVerifier``. A refused request is
a 401 envelope whose ``WWW-Authenticate`` names the RFC 9728 metadata (``resource_metadata``,
plus ``error="invalid_token"`` when a token was sent) so the client can discover the issuer;
an unreachable issuer is a 503 (``auth_unavailable``), never a pass. ``Authorization`` sent
more than once is refused 400 before any check (the SDK does not check). A valid token binds
``Principal('service', 'mcp')``, never a ``user``: whoever signed in, the agent cannot approve
its own gated actions (service principals cannot approve). WebSocket upgrades are closed 4403
by the gate itself, never forwarded: the transport is HTTP-only.
"""

from __future__ import annotations

import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from hydra.dashboard.mcp.tokens import InvalidTokenError, IssuerUnavailableError, TokenVerifier
from hydra.dashboard.routes._db import ErrorState, error_detail
from hydra.dashboard.security import WS_CLOSE_FORBIDDEN, Principal

logger = logging.getLogger(__name__)


def _header_values(scope: Scope, name: str) -> list[str]:
    key = name.encode("latin-1")
    return [v.decode("latin-1") for k, v in scope.get("headers", ()) if k.lower() == key]


def _bearer(raw: str) -> str | None:
    scheme, _, token = raw.partition(" ")
    return (token.strip() or None) if scheme.lower() == "bearer" else None


def _peer(scope: Scope) -> str:
    return str((scope.get("client") or ("?",))[0])


class MCPAuthMiddleware:
    """Fail-closed Authentik access-token gate around the mounted MCP app."""

    def __init__(self, app: ASGIApp, verifier: TokenVerifier, metadata_url: str) -> None:
        self.app = app
        self.verifier = verifier
        self.metadata_url = metadata_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            logger.info(
                "Refused websocket %s from %s: websocket_not_supported",
                scope["path"],
                _peer(scope),
            )
            await WebSocketClose(code=WS_CLOSE_FORBIDDEN, reason="websocket_not_supported")(
                scope, receive, send
            )
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        values = _header_values(scope, "authorization")
        if len(values) > 1:
            await self._refuse(
                scope, receive, send, 400, "duplicate_header", "Authorization sent more than once"
            )
            return
        token = _bearer(values[0]) if values else None
        if token is None:
            await self._refuse(
                scope, receive, send, 401, "invalid_token", "Sign in through Authentik first"
            )
            return
        try:
            await self.verifier.verify(token)
        except InvalidTokenError as exc:
            logger.info("Refused an MCP token from %s: %s", _peer(scope), exc)
            await self._refuse(
                scope, receive, send, 401, "invalid_token", "Bearer token is not valid", sent=True
            )
            return
        except IssuerUnavailableError as exc:
            logger.warning("MCP token check failed: %s", exc)
            await self._refuse(
                scope,
                receive,
                send,
                503,
                "auth_unavailable",
                "The sign-in server could not be reached to check the token",
            )
            return
        scope["state"] = {**scope.get("state", {}), "principal": Principal("service", "mcp")}
        await self.app(scope, receive, send)

    async def _refuse(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        status: int,
        code: str,
        message: str,
        *,
        sent: bool = False,
    ) -> None:
        logger.info("Refused %s %s from %s: %s", scope["type"], scope["path"], _peer(scope), code)
        state: ErrorState = (
            "invalid" if status == 400 else "unauthenticated" if status == 401 else "unavailable"
        )
        error = 'error="invalid_token", ' if sent else ""
        response = JSONResponse(
            {"detail": error_detail(code, message, state)},
            status_code=status,
            headers=(
                {"WWW-Authenticate": f'Bearer {error}resource_metadata="{self.metadata_url}"'}
                if status == 401
                else None
            ),
        )
        await response(scope, receive, send)
