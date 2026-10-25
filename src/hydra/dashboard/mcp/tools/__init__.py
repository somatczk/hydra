"""MCP tools, registered on the shared server instance (``hydra.dashboard.mcp.server``).

Empty placeholders in this PR — ``tools/list`` returns ``[]``; B1 fills ``research`` and
``strategies`` (21 research/CRUD tools), B2 fills ``sessions`` and ``risk`` (10 more),
each without touching the transport files.
"""

from __future__ import annotations

from hydra.dashboard.mcp.tools import research, risk, sessions, strategies

__all__ = ["research", "risk", "sessions", "strategies"]
