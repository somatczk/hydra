"""Shared error envelope and database helpers for dashboard routes.

Every non-2xx response raised by a dashboard route carries
``{"detail": {"code": str, "message": str, "state": ErrorState, ...extra}}``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

import asyncpg
from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

ErrorState = Literal["unavailable", "no_data", "not_found", "invalid", "not_implemented"]

DB_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    TimeoutError,
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
)


def api_error(
    status_code: int,
    code: str,
    message: str,
    state: ErrorState,
    **extra: Any,
) -> HTTPException:
    """Build an HTTPException whose detail follows the shared envelope."""
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "state": state, **extra},
    )


def database_unavailable(message: str = "Database is not available") -> HTTPException:
    return api_error(503, "database_unavailable", message, "unavailable")


def require_pool(request: Request) -> Any:
    """Return the asyncpg pool from app state or raise 503."""
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise database_unavailable()
    return pool


@asynccontextmanager
async def db_errors_to_503(what: str) -> AsyncIterator[None]:
    """Turn connection and query failures into a 503; let real bugs surface as 500."""
    try:
        yield
    except DB_ERRORS as exc:
        logger.exception("Database error while %s", what)
        raise database_unavailable(f"Database error while {what}") from exc
