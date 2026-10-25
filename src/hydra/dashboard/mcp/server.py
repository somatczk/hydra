"""The MCP mount: the SDK's streamable-HTTP app behind the token gate, attached at ``/mcp``.

``build_mcp_app`` builds the streamable-HTTP app of the shared ``MCPServer`` ('hydra', the
module-level instance in ``__init__``) with ``TransportSecuritySettings`` from
``mcp.allowed_hosts`` and wraps it in the bearer-token middleware. ``mount_mcp`` attaches it
to the engine app at ``MCP_PATH_PREFIX`` and returns the mount's session manager — ``run()`` is
single-use, so the caller enters and closes it (the lifespan does, via ``AsyncExitStack``);
until it is entered the transport refuses requests with the SDK's "Task group is not
initialized".
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass

from fastapi import FastAPI
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp

from hydra.core.config import HydraConfig
from hydra.dashboard.mcp.auth import MCPAuthMiddleware
from hydra.dashboard.security import MCP_PATH_PREFIX

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MCPMount:
    """What ``mount_mcp`` attached: the app at ``path`` and the session manager to ``run()``."""

    path: str
    app: ASGIApp
    session_manager: StreamableHTTPSessionManager


def build_mcp_app(cfg: HydraConfig) -> ASGIApp:
    """The shared server's streamable-HTTP app, wrapped in the bearer-token middleware.

    Also imports ``hydra.dashboard.mcp.tools`` so the tool modules register on the shared
    server instance (empty placeholders now; B1/B2 fill them without touching this file).
    """
    from hydra.dashboard.mcp import server as mcp_server

    importlib.import_module("hydra.dashboard.mcp.tools")
    asgi = mcp_server.streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=cfg.mcp.allowed_hosts),
    )
    return MCPAuthMiddleware(asgi)


def mount_mcp(app: FastAPI, cfg: HydraConfig) -> MCPMount:
    """Attach the token-gated MCP app at ``MCP_PATH_PREFIX``; call only when the token is set.

    Requests reach the mount only after startup (uvicorn serves nothing before), so attaching
    in the lifespan is race-free: Starlette builds the middleware stack lazily around the
    router, which sees the new ``Mount`` on the next request.
    """
    from hydra.dashboard.mcp import server as mcp_server

    mcp_app = build_mcp_app(cfg)
    app.mount(MCP_PATH_PREFIX, mcp_app, name="mcp")
    log_mcp_posture(cfg)
    return MCPMount(
        path=MCP_PATH_PREFIX, app=mcp_app, session_manager=mcp_server.session_manager
    )


def log_mcp_posture(cfg: HydraConfig) -> None:
    """One startup line for the MCP surface (the REST posture line lives in security.py)."""
    logger.info(
        "MCP server mounted at %s: bearer token set, allowed hosts: %s",
        MCP_PATH_PREFIX,
        ", ".join(cfg.mcp.allowed_hosts),
    )
