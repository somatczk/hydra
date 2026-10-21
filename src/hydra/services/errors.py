"""Service errors carry the shared envelope (``{"code", "message", "state", ...}``) and a status."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

import asyncpg

logger = logging.getLogger(__name__)

ErrorState = Literal[
    "unavailable",
    "no_data",
    "not_found",
    "invalid",
    "not_implemented",
    "unauthenticated",
    "forbidden",
]

DB_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    TimeoutError,
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
)


def error_detail(code: str, message: str, state: ErrorState, **extra: Any) -> dict[str, Any]:
    """The envelope's ``detail`` object; raw ASGI responses (security.py) use it directly."""
    return {"code": code, "message": message, "state": state, **extra}


class ServiceError(Exception):
    """A failure reported as ``status_code`` with ``detail`` (HTTP body or MCP tool error)."""

    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def service_error(
    status_code: int, code: str, message: str, state: ErrorState, **extra: Any
) -> ServiceError:
    """Build a ServiceError whose detail follows the shared envelope."""
    return ServiceError(status_code, error_detail(code, message, state, **extra))


def database_unavailable(message: str = "Database is not available") -> ServiceError:
    return service_error(503, "database_unavailable", message, "unavailable")


def require_pool(pool: Any) -> Any:
    """Return the pool, or raise 503 when the process has none."""
    if pool is None:
        raise database_unavailable()
    return pool


@asynccontextmanager
async def db_errors(what: str) -> AsyncIterator[None]:
    """Turn connection and query failures into a 503; let real bugs surface as 500."""
    try:
        yield
    except DB_ERRORS as exc:
        logger.exception("Database error while %s", what)
        raise database_unavailable(f"Database error while {what}") from exc
