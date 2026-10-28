"""MCP tools, registered on the shared server instance (``hydra.dashboard.mcp.server``).

``research`` and ``strategies`` carry the 22 research/CRUD tools (B1, plus
``universe_list`` from P6-A1); ``sessions`` and ``risk`` the 10 session/risk tools (B2);
``live`` the two code-gated live tools (P3-C) — 34 total, with
``kill_switch_release``, ``live_session_start`` and ``live_order``
the gated actions.
"""

from __future__ import annotations

from hydra.dashboard.mcp.tools import live, research, risk, sessions, strategies

__all__ = ["live", "research", "risk", "sessions", "strategies"]
