"""MCP tools, registered on the shared server instance (``hydra.dashboard.mcp.server``).

``research`` and ``strategies`` carry the 21 research/CRUD tools (B1); ``sessions`` and
``risk`` the 10 session/risk tools (B2) — 31 total, with ``kill_switch_release`` the one
gated action.
"""

from __future__ import annotations

from hydra.dashboard.mcp.tools import research, risk, sessions, strategies

__all__ = ["research", "risk", "sessions", "strategies"]
