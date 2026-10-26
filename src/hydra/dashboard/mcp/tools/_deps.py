"""Engine bindings shared by the tool modules: pool, session manager, error conversion.

A tool call has no FastAPI request — the SDK's Starlette app replaces ``scope["app"]``
with its own — so tool bodies resolve the engine lazily: the uvicorn entrypoint object
(``hydra.dashboard.api.app``) whose state the lifespan binds (``db_pool``,
``session_manager``). In the engine process that object is the app the MCP mount is
attached to; without a lifespan-bound pool the services answer their 503 envelope.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from hydra.dashboard.security import Principal
from hydra.services.errors import ServiceError

MCP_PRINCIPAL = Principal("service", "mcp")
"""The principal every tool mutation is attributed to (seam 4)."""


def engine_state() -> Any:
    """The engine FastAPI app's state the lifespan bound (config, pool, session manager)."""
    from hydra.dashboard.api import app

    return app.state


def db_pool() -> Any:
    """The process's asyncpg pool, or None; services raise the 503 where they need it."""
    return getattr(engine_state(), "db_pool", None)


def session_manager() -> Any:
    """The engine's SessionManager, or None when startup has not bound one."""
    return getattr(engine_state(), "session_manager", None)


def tool_error(exc: ServiceError) -> ToolError:
    """A ToolError carrying the full ServiceError envelope, verbatim and unsanitized.

    The JSON text is what the tool caller reads, so a 409 ``restart_required`` keeps
    its ``changed_fields`` list and a 422 keeps its ``no_market_data`` extras.
    """
    return ToolError(json.dumps({"status": exc.status_code, "detail": exc.detail}, default=str))


def json_text(value: Any) -> str:
    """One JSON text block for any tool result.

    The SDK splits a list result into one content block per item; serializing here
    keeps every call to exactly one block carrying the whole (list or dict) result.
    """
    return json.dumps(value, indent=2, default=str)
