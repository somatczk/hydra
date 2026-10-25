"""The Hydra MCP server (D5): streamable HTTP at ``/mcp``, gated by ``HYDRA_MCP_TOKEN``.

One module-level ``MCPServer`` ('hydra') per engine process; the lifespan binds its
``MCPMount`` to ``app.state.mcp`` and attaches the server at ``/mcp`` only while
``HYDRA_MCP_TOKEN`` is set (no token, no surface — fail-safe). The token is deliberately
not ``HYDRA_API_TOKEN``: it grants ``Principal('service', 'mcp')``, never a REST
service principal. Tool modules register on the shared ``server`` instance via
``hydra.dashboard.mcp.tools`` — empty placeholders in this PR (``tools/list == []``);
B1/B2 fill them without touching the transport files.
"""

from __future__ import annotations

import hydra
from mcp.server import MCPServer

from hydra.dashboard.mcp.server import MCPMount, build_mcp_app, mount_mcp

server: MCPServer = MCPServer("hydra", version=hydra.__version__)

__all__ = ["MCPMount", "MCPServer", "build_mcp_app", "mount_mcp", "server"]
