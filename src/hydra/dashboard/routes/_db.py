"""Shared error envelope (``{"detail": {"code", "message", "state", ...}}``) and DB helpers."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from hydra.services.errors import DB_ERRORS as DB_ERRORS
from hydra.services.errors import ErrorState as ErrorState
from hydra.services.errors import ServiceError
from hydra.services.errors import error_detail as error_detail
from hydra.services.trader import LocalTrader

logger = logging.getLogger(__name__)


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


class ServiceErrorRoute(APIRoute):
    """A route calling hydra.services: a ServiceError answers exactly as the same HTTPException."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def route_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except ServiceError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

        return route_handler


def request_pool(request: Request) -> Any:
    """The app's asyncpg pool, or None; services raise the 503 where they need it."""
    return getattr(request.app.state, "db_pool", None)


def request_trader(request: Request) -> Any:
    """The app's TraderBackend; an unbound state reads like a manager-less local engine.

    The lifespan binds ``app.state.trader`` and the tests inject theirs there. Without one
    (a lifespan-less app) the routes keep answering exactly what a ``None`` session
    manager answered before the rewiring.
    """
    backend = getattr(request.app.state, "trader", None)
    if backend is not None:
        return backend
    return LocalTrader(getattr(request.app.state, "db_pool", None), None)
