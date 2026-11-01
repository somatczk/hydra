"""The MCP mount: the SDK's streamable-HTTP app behind the OAuth gate, attached at ``/mcp``.

``build_mcp_app`` builds the streamable-HTTP app of the shared ``MCPServer`` ('hydra', the
module-level instance in ``__init__``) with ``TransportSecuritySettings`` from
``mcp.allowed_hosts`` and wraps it in the Authentik access-token middleware. ``mount_mcp``
attaches it to the engine app at ``MCP_PATH_PREFIX``, adds the RFC 9728 protected-resource
metadata at both public paths (the SDK's route under ``/.well-known/oauth-protected-resource/mcp``
plus the bare path, which some clients try first) and returns the mount's session manager —
``run()`` is single-use, so the caller enters and closes it (the lifespan does, via
``AsyncExitStack``); until it is entered the transport refuses requests with the SDK's "Task
group is not initialized". ``transport`` reaches the token verifier's HTTP client (tests serve
the issuer's discovery and keys in-process; production leaves it ``None``).
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass

import httpx
from fastapi import FastAPI
from mcp.server.auth.routes import build_resource_metadata_url, create_protected_resource_routes
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.routing import Route
from starlette.types import ASGIApp

from hydra.core.config import HydraConfig
from hydra.dashboard.mcp.auth import MCPAuthMiddleware
from hydra.dashboard.mcp.tokens import TokenVerifier
from hydra.dashboard.security import MCP_PATH_PREFIX, PROTECTED_RESOURCE_PATH

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MCPMount:
    """What ``mount_mcp`` attached: the app at ``path`` and the session manager to ``run()``."""

    path: str
    app: ASGIApp
    session_manager: StreamableHTTPSessionManager


def build_mcp_app(cfg: HydraConfig, transport: httpx.AsyncBaseTransport | None = None) -> ASGIApp:
    """The shared server's streamable-HTTP app, wrapped in the OAuth access-token middleware.

    Also imports ``hydra.dashboard.mcp.tools`` so the tool modules register on the shared
    server instance; a new tool goes in a tool module, never in this file.
    """
    from hydra.dashboard.mcp import server

    importlib.import_module("hydra.dashboard.mcp.tools")
    asgi = server.streamable_http_app(
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(allowed_hosts=cfg.mcp.allowed_hosts),
    )
    metadata_url = build_resource_metadata_url(AnyHttpUrl(str(cfg.mcp.resource_url)))
    return MCPAuthMiddleware(asgi, TokenVerifier(cfg.mcp, transport), str(metadata_url))


def mount_mcp(
    app: FastAPI, cfg: HydraConfig, transport: httpx.AsyncBaseTransport | None = None
) -> MCPMount:
    """Attach the OAuth-gated MCP app and its metadata; call only when MCP OAuth is configured.

    Requests reach the mount only after startup (uvicorn serves nothing before), so attaching
    in the lifespan is race-free: Starlette builds the middleware stack lazily around the
    router, which sees the new routes on the next request.
    """
    from hydra.dashboard.mcp import server

    mcp_app = build_mcp_app(cfg, transport)
    [metadata] = create_protected_resource_routes(
        resource_url=AnyHttpUrl(str(cfg.mcp.resource_url)),
        authorization_servers=[AnyHttpUrl(str(cfg.mcp.oauth_issuer))],
        scopes_supported=cfg.mcp.required_scopes or None,
        resource_name="Hydra",
    )
    app.router.routes.append(metadata)
    app.router.routes.append(
        Route(PROTECTED_RESOURCE_PATH, metadata.endpoint, methods=["GET", "OPTIONS"])
    )
    app.mount(MCP_PATH_PREFIX, mcp_app, name="mcp")
    log_mcp_posture(cfg)
    return MCPMount(path=MCP_PATH_PREFIX, app=mcp_app, session_manager=server.session_manager)


def log_mcp_posture(cfg: HydraConfig) -> None:
    """One startup line for the MCP surface (the REST posture line lives in security.py)."""
    logger.info(
        "MCP server mounted at %s: Authentik OAuth (issuer %s, audience %s), allowed hosts: %s",
        MCP_PATH_PREFIX,
        cfg.mcp.oauth_issuer,
        cfg.mcp.oauth_audience,
        ", ".join(cfg.mcp.allowed_hosts),
    )
