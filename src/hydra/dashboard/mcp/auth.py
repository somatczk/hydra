"""Bearer-token gate for the ``/mcp`` mount: pure ASGI, fail-closed, deliberately no CSRF.

The MCP surface is non-browser — Claude Code sends one static ``Authorization: Bearer``
header — so the token itself is the defense: there is no CSRF check and no Origin check
(the SDK's ``TransportSecuritySettings`` still validates the ``Host`` header against
``mcp.allowed_hosts``). The token is read from ``app.state.config`` at request time;
without a config, or without ``HYDRA_MCP_TOKEN`` in it, every request is refused — the
mount is only ever attached when the token is set, so that 401 is the belt behind the
lifespan's brace. ``Authorization`` sent more than once is refused 400 before any
comparison (the SDK does not check; ported from ``security._single_header``). A valid
token binds ``Principal('service', 'mcp')`` on the request state and hands the request
to the SDK's streamable-HTTP app.
"""

from __future__ import annotations

import hmac
import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from hydra.core.config import HydraConfig
from hydra.dashboard.routes._db import ErrorState, error_detail
from hydra.dashboard.security import Principal

logger = logging.getLogger(__name__)


def _config(scope: Scope) -> HydraConfig | None:
    app = scope.get("app")
    cfg = getattr(getattr(app, "state", None), "config", None)
    return cfg if isinstance(cfg, HydraConfig) else None


def _header_values(scope: Scope, name: str) -> list[str]:
    key = name.encode("latin-1")
    return [v.decode("latin-1") for k, v in scope.get("headers", ()) if k.lower() == key]


def _bearer(raw: str) -> str | None:
    scheme, _, token = raw.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else None


class MCPAuthMiddleware:
    """Fail-closed ``HYDRA_MCP_TOKEN`` gate around the mounted MCP app."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cfg = _config(scope)
        expected = cfg.mcp_token.get_secret_value() if cfg is not None else None
        if expected is None:
            await self._refuse(
                scope, receive, send, 401, "auth_not_configured",
                "MCP authentication is not configured (HYDRA_MCP_TOKEN)", "unauthenticated",
            )
            return
        values = _header_values(scope, "authorization")
        if len(values) > 1:
            await self._refuse(
                scope, receive, send, 400, "duplicate_header",
                "Authorization sent more than once", "invalid",
            )
            return
        provided = _bearer(values[0]) if values else None
        if provided is None or not hmac.compare_digest(provided.encode(), expected.encode()):
            await self._refuse(
                scope, receive, send, 401, "invalid_token",
                "Bearer token is not valid", "unauthenticated",
            )
            return
        scope["state"] = {**scope.get("state", {}), "principal": Principal("service", "mcp")}
        await self.app(scope, receive, send)

    @staticmethod
    async def _refuse(
        scope: Scope,
        receive: Receive,
        send: Send,
        status: int,
        code: str,
        message: str,
        state: ErrorState,
    ) -> None:
        peer = (scope.get("client") or ("?",))[0]
        logger.info("Refused %s %s from %s: %s", scope["type"], scope["path"], peer, code)
        response = JSONResponse(
            {"detail": error_detail(code, message, state)},
            status_code=status,
            headers={"WWW-Authenticate": "Bearer"} if status == 401 else None,
        )
        await response(scope, receive, send)
