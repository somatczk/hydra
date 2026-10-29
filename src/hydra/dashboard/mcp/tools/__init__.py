"""MCP tools, registered on the shared server instance (``hydra.dashboard.mcp.server``).

``research`` and ``strategies`` carry the 23 research/CRUD tools (B1, plus
``universe_list`` from P6-A1 and ``data_inventory`` from P6-C4); ``sessions`` and
``risk`` the 10 session/risk tools (B2); ``live`` the two code-gated live tools
(P3-C); ``models`` the 7 model-registry tools — 42 total, with
``kill_switch_release``, ``live_session_start``, ``live_order``, ``model_promote``
and ``model_rollback`` the gated actions.
"""

from __future__ import annotations

from hydra.dashboard.mcp.tools import live, models, research, risk, sessions, strategies

__all__ = ["live", "models", "research", "risk", "sessions", "strategies"]
