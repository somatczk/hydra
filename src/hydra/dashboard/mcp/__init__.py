"""The Hydra MCP server (D5): streamable HTTP at ``/mcp``, gated by Authentik OAuth.

One module-level ``MCPServer`` ('hydra') per engine process; the lifespan binds its
``MCPMount`` to ``app.state.mcp`` and attaches the server at ``/mcp`` only while MCP OAuth is
configured (``mcp.oauth_issuer``, ``mcp.oauth_audience``, ``mcp.resource_url``; unset, no
surface — fail-safe). A valid Authentik access token grants ``Principal('service', 'mcp')``,
never a REST principal and never a ``user``. Tool modules register on the shared ``server``
instance via ``hydra.dashboard.mcp.tools`` (42 tools in six modules); a new tool goes in one of
them, never in the transport files.
"""

from __future__ import annotations

from mcp.server import MCPServer

import hydra
from hydra.dashboard.mcp.server import MCPMount, build_mcp_app, mount_mcp

server: MCPServer = MCPServer("hydra", version=hydra.__version__)

__all__ = ["MCPMount", "MCPServer", "build_mcp_app", "mount_mcp", "server"]
