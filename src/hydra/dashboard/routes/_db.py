"""Shared error envelope (``{"detail": {"code", "message", "state", ...}}``) and DB helpers."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

import asyncpg
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

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
        detail=error_detail(code, message, state, **extra),
    )


def database_unavailable(message: str = "Database is not available") -> HTTPException:
    return api_error(503, "database_unavailable", message, "unavailable")


def unauthenticated(code: str, message: str) -> HTTPException:
    return api_error(401, code, message, "unauthenticated")


def forbidden(code: str, message: str) -> HTTPException:
    return api_error(403, code, message, "forbidden")


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


def _validation_message(errors: list[dict[str, Any]]) -> str:
    parts = []
    for err in errors:
        field = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
        msg = str(err.get("msg", "invalid value"))
        parts.append(f"{field}: {msg}" if field else msg)
    return "; ".join(parts)


async def _validation_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    raw = exc.errors() if isinstance(exc, RequestValidationError) else []
    errors = [
        {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
        for e in raw
    ]
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder(
            {
                "detail": {
                    "code": "validation_error",
                    "message": _validation_message(errors),
                    "state": "invalid",
                    "errors": errors,
                }
            }
        ),
    )


def install_error_handlers(app: FastAPI) -> None:
    """Make request-validation 422s use the shared envelope (submitted values are not echoed)."""
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
