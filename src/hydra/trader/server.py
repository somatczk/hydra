"""The trader's internal API: one endpoint per :class:`TraderBackend` op, bearer-gated (D5).

A small FastAPI served by ``python -m hydra.trader`` on :8000 for the dashboard API's
:class:`~hydra.dashboard.trader_client.RemoteTrader`: no MCP mount, no CORS, no docs —
internal docker networks only. Every path except the public probes needs
``Authorization: Bearer <HYDRA_API_TOKEN>`` (constant-time, fail-closed), and a
:class:`~hydra.services.errors.ServiceError` answers with the shared envelope so the
RemoteTrader rehydrates it verbatim. Request models are reused from
``hydra.services.sessions`` — the seam's own shapes, never duplicated here.
"""

from __future__ import annotations

import hmac
from dataclasses import asdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

import hydra
from hydra.dashboard.routes import health as health_routes
from hydra.dashboard.routes import metrics as metrics_routes
from hydra.dashboard.routes._db import ServiceErrorRoute, install_error_handlers
from hydra.dashboard.security import PUBLIC_PATHS
from hydra.services.errors import ErrorState, error_detail
from hydra.services.sessions import QuickTradeRequest, StartSessionRequest
from hydra.services.trader import LocalTrader

if TYPE_CHECKING:
    from hydra.core.config import HydraConfig

# ---------------------------------------------------------------------------
# The bearer gate
# ---------------------------------------------------------------------------


def _bearer(scope: Scope) -> str | None:
    """The single Authorization header's bearer token, or None (a duplicate is a 400)."""
    values = [
        v.decode("latin-1") for k, v in scope.get("headers", ()) if k.lower() == b"authorization"
    ]
    if len(values) > 1:
        raise _DeniedError(400, "duplicate_header", "authorization sent more than once", "invalid")
    raw = values[0] if values else None
    if raw is None:
        return None
    scheme, _, token = raw.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else None


class _DeniedError(Exception):
    def __init__(self, status: int, code: str, message: str, state: ErrorState) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.state = state


class BearerGate:
    """Fail-closed constant-time bearer gate over every non-public ``http`` path."""

    def __init__(self, app: ASGIApp, token: str | None) -> None:
        self.app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        try:
            token = _bearer(scope)
        except _DeniedError as denied:
            await self._refuse(scope, receive, send, denied)
            return
        failure = self._failure(token)
        if failure is not None:
            await self._refuse(scope, receive, send, failure)
            return
        await self.app(scope, receive, send)

    def _failure(self, token: str | None) -> _DeniedError | None:
        """The refusal the presented token earns, or None (constant-time on both paths)."""
        expected = self._token
        if expected is None:
            return _DeniedError(
                401,
                "auth_not_configured",
                "The trader's API token is not configured (HYDRA_API_TOKEN)",
                "unauthenticated",
            )
        if token is None:
            return _DeniedError(
                401, "unauthenticated", "Authentication required", "unauthenticated"
            )
        if not hmac.compare_digest(token.encode(), expected.encode()):
            return _DeniedError(
                401, "invalid_token", "Bearer token is not valid", "unauthenticated"
            )
        return None

    @staticmethod
    async def _refuse(scope: Scope, receive: Receive, send: Send, denied: _DeniedError) -> None:
        response = JSONResponse(
            {"detail": error_detail(denied.code, denied.message, denied.state)},
            status_code=denied.status,
            headers={"WWW-Authenticate": "Bearer"} if denied.status == 401 else None,
        )
        await response(scope, receive, send)


# ---------------------------------------------------------------------------
# Wire models (the two order bodies are the seam's own, reused verbatim)
# ---------------------------------------------------------------------------


class StartSessionCall(BaseModel):
    actor: str
    body: StartSessionRequest


class QuickTradeCall(BaseModel):
    order: QuickTradeRequest


class WebhookSignalCall(BaseModel):
    symbol: str
    side: str
    action: str
    quantity: Decimal | None = None
    strategy_id: str


class KillSwitchCall(BaseModel):
    actor: str
    reason: str


class ResetBreakerCall(KillSwitchCall):
    tier: int
    confirm_tier: int


class ActorCall(BaseModel):
    actor: str


ops_router = APIRouter(prefix="/ops", tags=["trader-ops"], route_class=ServiceErrorRoute)


def _trader(request: Request) -> LocalTrader:
    return cast("LocalTrader", request.app.state.trader)


# ---------------------------------------------------------------------------
# One endpoint per TraderBackend op (pinned seam #5: exactly the 19 ops)
# ---------------------------------------------------------------------------


@ops_router.post("/start-session")
async def start_session(call: StartSessionCall, request: Request) -> dict[str, Any]:
    return await _trader(request).start_session(call.actor, call.body)


@ops_router.delete("/sessions/{session_id}")
async def stop_session(session_id: str, request: Request) -> dict[str, Any]:
    return await _trader(request).stop_session(session_id)


@ops_router.get("/sessions")
async def list_sessions(request: Request) -> list[dict[str, Any]]:
    return await _trader(request).list_sessions()


@ops_router.get("/sessions/{session_id}/detail")
async def session_detail(session_id: str, request: Request) -> dict[str, Any]:
    return await _trader(request).session_detail(session_id)


@ops_router.post("/quick-trade")
async def quick_trade(call: QuickTradeCall, request: Request) -> dict[str, Any]:
    return await _trader(request).quick_trade(call.order)


@ops_router.post("/webhook-signal")
async def webhook_signal(call: WebhookSignalCall, request: Request) -> dict[str, Any]:
    return await _trader(request).webhook_signal(
        symbol=call.symbol,
        side=call.side,
        action=call.action,
        quantity=call.quantity,
        strategy_id=call.strategy_id,
    )


@ops_router.post("/strategies/{strategy_id}/reload")
async def reload_strategy(strategy_id: str, request: Request) -> dict[str, Any]:
    return await _trader(request).reload_strategy(strategy_id)


@ops_router.post("/kill-switch")
async def activate_kill_switch(call: KillSwitchCall, request: Request) -> dict[str, Any]:
    return await _trader(request).activate_kill_switch(call.actor, call.reason)


@ops_router.delete("/kill-switch")
async def release_kill_switch(call: KillSwitchCall, request: Request) -> dict[str, Any]:
    return await _trader(request).release_kill_switch(call.actor, call.reason)


@ops_router.get("/kill-switch/active")
async def kill_switch_active(request: Request) -> dict[str, Any]:
    return {"active": await _trader(request).kill_switch_active()}


@ops_router.get("/risk/status")
async def risk_status(request: Request) -> dict[str, Any]:
    return await _trader(request).risk_status()


@ops_router.get("/risk/var")
async def var_estimate(request: Request) -> dict[str, Any]:
    return await _trader(request).var_estimate()


@ops_router.get("/risk/breakers")
async def circuit_breakers(request: Request) -> list[dict[str, Any]]:
    return await _trader(request).circuit_breakers()


@ops_router.post("/risk/breakers/reset")
async def reset_breaker(call: ResetBreakerCall, request: Request) -> list[dict[str, Any]]:
    return await _trader(request).reset_breaker(
        call.tier, call.actor, call.reason, call.confirm_tier
    )


@ops_router.post("/risk/paper-account/reset")
async def reset_paper_account(call: KillSwitchCall, request: Request) -> None:
    return await _trader(request).reset_paper_account(call.actor, call.reason)


@ops_router.get("/risk/live-status")
async def live_status(request: Request, source: str = "paper") -> dict[str, Any]:
    return await _trader(request).live_status(source=source)


@ops_router.get("/positions")
async def positions(request: Request) -> list[dict[str, Any]]:
    return await _trader(request).positions()


@ops_router.post("/risk/paper-capital/reload")
async def reload_paper_capital(call: ActorCall, request: Request) -> None:
    return await _trader(request).reload_paper_capital(call.actor)


@ops_router.get("/health-snapshot")
async def health_snapshot(request: Request) -> dict[str, Any]:
    snap = await _trader(request).health_snapshot()
    body: dict[str, Any] = jsonable_encoder(asdict(snap))
    return body


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------


def build_app(cfg: HydraConfig, db_pool: Any, mgr: Any) -> FastAPI:
    """The trader's app: the gate, the 19 op endpoints, the probes and /metrics."""
    app = FastAPI(
        title="Hydra Trader",
        version=hydra.__version__,
        lifespan=None,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config = cfg
    app.state.db_pool = db_pool
    app.state.session_manager = mgr
    app.state.trader = LocalTrader(db_pool, mgr)
    app.state.health_monitor = None
    install_error_handlers(app)
    app.include_router(health_routes.router)
    app.include_router(metrics_routes.router)
    app.include_router(ops_router)
    app.add_middleware(
        BearerGate, token=cfg.api_token.get_secret_value() if cfg.api_token else None
    )
    return app
