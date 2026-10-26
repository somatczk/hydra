"""Engine bindings shared by the tool modules: pool, trader backend, error conversion.

A tool call has no FastAPI request — the SDK's Starlette app replaces ``scope["app"]``
with its own — so tool bodies resolve the engine lazily. The engine container serves
``python -m hydra.dashboard.api``, which executes ``api.py`` as ``__main__``: the
served app the lifespan binds (``db_pool``, ``trader``) is
``sys.modules["__main__"].app``, and importing ``hydra.dashboard.api`` in that process
builds a second, lifespan-less instance. The engine state is therefore resolved from
the lifespan-bound ``__main__`` app when that is the served engine, with the module
import as the fallback (the unit-test seam); without a lifespan-bound pool the
services answer their 503 envelope.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from hydra.dashboard.security import Principal
from hydra.services.errors import ServiceError, service_error
from hydra.services.trader import LocalTrader

MCP_PRINCIPAL = Principal("service", "mcp")
"""The principal every tool mutation is attributed to (seam 4)."""


def engine_state() -> Any:
    """The served engine app's state the lifespan bound (config, pool, session manager).

    Under ``python -m hydra.dashboard.api`` the served app is ``__main__.app``; the
    import below would build a stateless second instance in that process, so the
    lifespan-bound ``__main__`` app wins and the import stays the fallback.
    """
    main = sys.modules.get("__main__")
    main_file = getattr(main, "__file__", None)
    if main_file and str(main_file).replace("\\", "/").lower().endswith("dashboard/api.py"):
        state = getattr(getattr(main, "app", None), "state", None)
        if state is not None and (
            getattr(state, "db_pool", None) is not None
            or getattr(state, "session_manager", None) is not None
        ):
            return state
    from hydra.dashboard.api import app

    return app.state


def db_pool() -> Any:
    """The process's asyncpg pool, or None; services raise the 503 where they need it."""
    return getattr(engine_state(), "db_pool", None)


def trader_from(state: Any) -> Any:
    """The TraderBackend bound on *state* (``app.state``), manager-less fallback when unbound."""
    backend = getattr(state, "trader", None)
    if backend is not None:
        return backend
    return LocalTrader(getattr(state, "db_pool", None), None)


def trader() -> Any:
    """The engine's TraderBackend; an unbound state behaves like a manager-less engine."""
    return trader_from(engine_state())


def tool_error(exc: ServiceError) -> ToolError:
    """A ToolError carrying the full ServiceError envelope, verbatim and unsanitized.

    The JSON text is what the tool caller reads, so a 409 ``restart_required`` keeps
    its ``changed_fields`` list and a 422 keeps its ``no_market_data`` extras.
    """
    return ToolError(json.dumps({"status": exc.status_code, "detail": exc.detail}, default=str))


def invalid_arguments(exc: ValidationError) -> ToolError:
    """A 422 validation envelope naming every invalid field, like the REST 422s.

    The SDK converts any non-ToolError raised inside a tool body into an opaque
    ``Error executing tool <name>``; without this, a caller cannot correct the input.
    """
    raw = exc.errors()
    errors = [
        {
            "loc": [str(part) for part in error["loc"]],
            "msg": error["msg"],
            "type": error["type"],
        }
        for error in raw
    ]
    parts = [
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
        if error["loc"]
        else error["msg"]
        for error in raw
    ]
    return tool_error(
        service_error(422, "validation_error", "; ".join(parts), "invalid", errors=errors)
    )


def json_text(value: Any) -> str:
    """One JSON text block for any tool result.

    The SDK splits a list result into one content block per item; serializing here
    keeps every call to exactly one block carrying the whole (list or dict) result.
    """
    return json.dumps(value, indent=2, default=str)
