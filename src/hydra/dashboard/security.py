"""API authentication, CSRF and CORS for the engine, as pure ASGI middleware.

Every HTTP and WebSocket request needs a principal, except the paths in ``PUBLIC_PATHS``:

- ``service``: ``Authorization: Bearer <HYDRA_API_TOKEN>`` (constant-time compare);
- ``user``: a ``Remote-User`` header from a TCP peer inside ``api.trusted_proxy_cidrs``
  (Traefik after Authelia forwardAuth; only in ``proxy`` mode);
- ``dev``: anyone, in ``api.auth_mode: dev`` (local compose only; logged at CRITICAL).

Browser principals (``user``, ``dev``) must also pass the CSRF checks on non-safe methods
(``X-Hydra-Request: 1``, an allowed ``Origin`` if sent, ``Sec-Fetch-Site`` same-origin or none)
and must send an allowed ``Origin`` on WebSocket upgrades. Settings come from
``app.state.config`` at request time; without a loaded config every protected request is refused.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any, Literal

from fastapi import FastAPI, Request
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from hydra.core.config import ApiConfig, HydraConfig
from hydra.core.logging import get_logger
from hydra.dashboard.routes._db import ErrorState, error_detail, unauthenticated

logger = logging.getLogger(__name__)
audit_logger = get_logger("hydra.audit")

PUBLIC_PATHS: frozenset[str] = frozenset({"/health", "/metrics"})
SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "x-hydra-request"
ALLOWED_FETCH_SITES: frozenset[str] = frozenset({"same-origin", "none"})
CORS_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
CORS_HEADERS = ("Content-Type", "X-Hydra-Request", "Authorization")

WS_CLOSE_UNAUTHENTICATED = 4401
WS_CLOSE_FORBIDDEN = 4403

PrincipalKind = Literal["user", "service", "dev"]


@dataclass(frozen=True)
class Principal:
    kind: PrincipalKind
    name: str


class _DeniedError(Exception):
    def __init__(self, status: int, code: str, message: str, state: ErrorState) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.state = state


def _config(scope: Scope) -> HydraConfig | None:
    app = scope.get("app")
    cfg = getattr(getattr(app, "state", None), "config", None)
    return cfg if isinstance(cfg, HydraConfig) else None


def _header_values(scope: Scope, name: str) -> list[str]:
    key = name.encode("latin-1")
    return [v.decode("latin-1") for k, v in scope.get("headers", ()) if k.lower() == key]


def _single_header(scope: Scope, name: str) -> str | None:
    values = _header_values(scope, name)
    if len(values) > 1:
        raise _DeniedError(400, "duplicate_header", f"{name} sent more than once", "invalid")
    return values[0] if values else None


def _peer_is_trusted(scope: Scope, cfg: HydraConfig) -> bool:
    client = scope.get("client")
    if not client:
        return False
    try:
        peer = ip_address(client[0])
    except ValueError:
        return False
    return any(peer in network for network in cfg.api.trusted_proxy_cidrs)


def _bearer(scope: Scope) -> str | None:
    raw = _single_header(scope, "authorization")
    if raw is None:
        return None
    scheme, _, token = raw.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip()


def _not_configured(cfg: HydraConfig | None) -> bool:
    if cfg is None:
        return True
    api = cfg.api
    return api.auth_mode == "proxy" and not api.trusted_proxy_cidrs and cfg.api_token is None


def resolve_principal(scope: Scope, cfg: HydraConfig | None) -> Principal:
    """Who is calling, or :class:`_DeniedError` (401) when nobody we trust."""
    if _not_configured(cfg) or cfg is None:
        raise _DeniedError(
            401,
            "auth_not_configured",
            "API authentication is not configured (api.auth_mode / HYDRA_API_TOKEN)",
            "unauthenticated",
        )
    token = _bearer(scope)
    if token is not None:
        expected = cfg.api_token.get_secret_value() if cfg.api_token is not None else None
        if expected is not None and hmac.compare_digest(token.encode(), expected.encode()):
            return Principal("service", "api-token")
        raise _DeniedError(401, "invalid_token", "Bearer token is not valid", "unauthenticated")
    mode = cfg.api.auth_mode
    if mode == "dev":
        return Principal("dev", "dev")
    if mode == "proxy" and _peer_is_trusted(scope, cfg):
        user = (_single_header(scope, "remote-user") or "").strip()
        if user:
            return Principal("user", user)
    raise _DeniedError(401, "unauthenticated", "Authentication required", "unauthenticated")


def _origin_allowed(origin: str, cfg: HydraConfig) -> bool:
    return origin in cfg.api.cors_origins


def check_csrf(scope: Scope, cfg: HydraConfig) -> None:
    """Non-safe browser requests must prove they come from the dashboard's own origin."""
    if _single_header(scope, CSRF_HEADER) != "1":
        raise _DeniedError(403, "csrf_rejected", "Missing X-Hydra-Request header", "forbidden")
    origin = _single_header(scope, "origin")
    if origin is not None and not _origin_allowed(origin, cfg):
        raise _DeniedError(403, "csrf_rejected", "Origin is not allowed", "forbidden")
    site = _single_header(scope, "sec-fetch-site")
    if site is not None and site.lower() not in ALLOWED_FETCH_SITES:
        raise _DeniedError(403, "csrf_rejected", "Cross-site request refused", "forbidden")


def check_ws_origin(scope: Scope, cfg: HydraConfig) -> None:
    origin = _single_header(scope, "origin")
    if origin is None or not _origin_allowed(origin, cfg):
        raise _DeniedError(
            403, "ws_origin_rejected", "WebSocket Origin is not allowed", "forbidden"
        )


class SecurityMiddleware:
    """Fail-closed authentication + CSRF for ``http`` and ``websocket`` scopes."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, self._lifespan_send(scope, send))
            return
        if scope["type"] not in ("http", "websocket") or scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        cfg = _config(scope)
        try:
            principal = resolve_principal(scope, cfg)
            if cfg is not None and principal.kind != "service":
                if scope["type"] == "websocket":
                    check_ws_origin(scope, cfg)
                elif scope["method"] not in SAFE_METHODS:
                    check_csrf(scope, cfg)
        except _DeniedError as denied:
            peer = (scope.get("client") or ("?",))[0]
            logger.info(
                "Refused %s %s from %s: %s", scope["type"], scope["path"], peer, denied.code
            )
            await self._refuse(scope, receive, send, denied)
            return
        scope["state"] = {**scope.get("state", {}), "principal": principal}
        await self.app(scope, receive, send)

    @staticmethod
    async def _refuse(scope: Scope, receive: Receive, send: Send, denied: _DeniedError) -> None:
        if scope["type"] == "websocket":
            code = WS_CLOSE_UNAUTHENTICATED if denied.status == 401 else WS_CLOSE_FORBIDDEN
            await WebSocketClose(code=code, reason=denied.code)(scope, receive, send)
            return
        headers = {"WWW-Authenticate": "Bearer"} if denied.status == 401 else None
        response = JSONResponse(
            {"detail": error_detail(denied.code, denied.message, denied.state)},
            status_code=denied.status,
            headers=headers,
        )
        await response(scope, receive, send)

    @staticmethod
    def _lifespan_send(scope: Scope, send: Send) -> Send:
        async def wrapped(message: Message) -> None:
            if message["type"] == "lifespan.startup.complete":
                log_auth_posture(_config(scope))
            await send(message)

        return wrapped


def log_auth_posture(cfg: HydraConfig | None) -> None:
    """One startup line saying how the API is protected; dev and unconfigured are loud."""
    if cfg is None:
        logger.error("API auth: no config loaded; every protected request is refused")
    elif cfg.api.auth_mode == "dev":
        logger.critical(
            "API auth_mode is 'dev': every caller is trusted. Local compose only, never deploy this"
        )
    elif _not_configured(cfg):
        logger.error(
            "API auth_mode 'proxy' has no api.trusted_proxy_cidrs and no HYDRA_API_TOKEN: "
            "every protected request is refused (401 auth_not_configured)"
        )
    else:
        logger.info(
            "API auth_mode %s: %d trusted proxy network(s), service token %s",
            cfg.api.auth_mode,
            len(cfg.api.trusted_proxy_cidrs),
            "set" if cfg.api_token is not None else "unset",
        )


class ConfigCORSMiddleware:
    """Starlette's CORSMiddleware with ``allow_origins`` read from ``app.state.config``."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._built: dict[tuple[str, ...], CORSMiddleware] = {}

    def _for(self, origins: tuple[str, ...]) -> CORSMiddleware:
        middleware = self._built.get(origins)
        if middleware is None:
            middleware = CORSMiddleware(
                self.app,
                allow_origins=origins,
                allow_methods=CORS_METHODS,
                allow_headers=CORS_HEADERS,
                allow_credentials=True,
            )
            self._built = {origins: middleware}
        return middleware

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cfg = _config(scope)
        origins = cfg.api.cors_origins if cfg is not None else ApiConfig().cors_origins
        await self._for(tuple(origins))(scope, receive, send)


def install_security(app: FastAPI) -> None:
    """Auth + CSRF inside, CORS outermost so preflights are answered before auth."""
    app.add_middleware(SecurityMiddleware)
    app.add_middleware(ConfigCORSMiddleware)


def current_principal(request: Request) -> Principal:
    """FastAPI dependency: the principal the middleware resolved for this request."""
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, Principal):
        raise unauthenticated("unauthenticated", "Authentication required")
    return principal


def audit(principal: Principal | None, action: str, **fields: Any) -> None:
    """One structured line on the ``hydra.audit`` logger for a security-relevant action."""
    audit_logger.info(
        "audit",
        action=action,
        principal=principal.name if principal is not None else None,
        principal_kind=principal.kind if principal is not None else None,
        **fields,
    )
