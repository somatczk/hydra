"""Trading session, kill-switch and quick-trade API routes.

Every order goes through ``SessionManager.submit_manual_order`` (OrderManager -> RiskService ->
paper executor). Errors use the ``_db.py`` envelope.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Any, Literal, Self

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hydra.core.types import OrderType, Side
from hydra.dashboard.health import SNAPSHOT_TIMEOUT_S
from hydra.dashboard.routes._db import api_error, require_pool
from hydra.dashboard.routes.strategies import build_strategy_name_map
from hydra.dashboard.security import audit
from hydra.data.ingestion import InsufficientHistoryError
from hydra.execution.errors import (
    DuplicateSessionError,
    KillSwitchActiveError,
    LiveTradingDisabledError,
    PaperCapitalUnavailableError,
    SessionNotRunningError,
    SessionPersistError,
    SpotShortError,
    UnsupportedStrategyError,
    UnsupportedVenueError,
)
from hydra.execution.order_manager import RiskRejectedError
from hydra.execution.paper_trading import (
    PaperSessionHaltedError,
    PaperTradingExecutor,
    PersistenceUnavailableError,
)
from hydra.execution.protective_orders import ProtectionSpec
from hydra.risk.market_rules import MarketRulesUnavailableError

router = APIRouter(prefix="/api/trading", tags=["trading"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class StartSessionRequest(BaseModel):
    strategy_id: str
    trading_mode: str = "paper"  # 'paper' | 'live'
    paper_capital: Decimal | None = Field(None, gt=0, max_digits=24, decimal_places=8)


class SessionResponse(BaseModel):
    session_id: str
    strategy_id: str
    strategy_name: str | None = None
    trading_mode: str
    status: str
    exchange_id: str
    symbols: list[str]
    timeframe: str
    paper_capital: float | None = None
    started_at: str | None = None
    stopped_at: str | None = None
    error_message: str | None = None
    positions_at_stop: int = 0
    flatten_incomplete: bool = False
    persist_error: str | None = None
    last_bar_at: str | None = None
    entries_paused: str | None = None
    persist_dirty: bool | None = None
    feed_live: bool | None = None


class PositionItem(BaseModel):
    symbol: str
    direction: str
    quantity: float
    avg_entry_price: float
    unrealized_pnl: float


class TradeItem(BaseModel, extra="allow"):
    id: str | None = None
    symbol: str | None = None
    side: str | None = None
    quantity: float = 0
    price: float = 0
    fee: float = 0
    pnl: float = 0
    net_pnl: float | None = None
    timestamp: str | None = None

    @field_validator("fee", mode="before")
    @classmethod
    def _normalize_fee(cls, v: Any) -> float:
        if isinstance(v, dict):
            return float(v.get("cost", 0))
        return float(v) if v is not None else 0.0


class OpenOrderItem(BaseModel, extra="allow"):
    id: str
    symbol: str
    side: str
    type: str
    amount: float
    price: float | None
    stop_price: float | None
    reason: str
    protective: bool | None


class SessionMetrics(BaseModel):
    balance: dict[str, float] = Field(default_factory=dict)
    total_pnl: float = 0
    win_rate: float = 0
    total_trades: int = 0
    open_positions: int = 0
    equity: float | None = None
    realized_pnl: float | None = None
    unrealized_pnl: float | None = None
    fees: float | None = None
    drawdown: float | None = None


class SessionDetailResponse(BaseModel):
    session_id: str
    strategy_id: str
    strategy_name: str | None = None
    trading_mode: str
    status: str
    exchange_id: str
    symbols: list[str]
    timeframe: str
    paper_capital: float | None = None
    started_at: str | None = None
    stopped_at: str | None = None
    error_message: str | None = None
    positions_at_stop: int = 0
    flatten_incomplete: bool = False
    last_bar_at: str | None = None
    entries_paused: str | None = None
    protection_failures: list[str] = Field(default_factory=list)
    persist_error: str | None = None
    metrics: SessionMetrics = Field(default_factory=SessionMetrics)
    positions: list[PositionItem] = Field(default_factory=list)
    trades: list[TradeItem] = Field(default_factory=list)
    open_orders: list[OpenOrderItem] = Field(default_factory=list)


class KillSwitchResponse(BaseModel):
    active: bool
    message: str
    sessions_stopped: int = 0
    positions_remaining: int = 0
    persisted: bool = True
    flatten_failures: list[str] = Field(default_factory=list)


class KillSwitchRelease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: Literal["RELEASE"]
    reason: str = Field(..., min_length=3, max_length=500)


class QuickTradeRequest(BaseModel):
    """A manual order; SL/TP/trailing are placed reduce-only when the entry fills."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = "BTCUSDT"
    side: Literal["buy", "sell"]
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    quantity: Decimal = Field(..., gt=0, max_digits=24, decimal_places=8)
    price: Decimal | None = Field(None, gt=0, max_digits=24, decimal_places=8)
    take_profit: Decimal | None = Field(None, gt=0, max_digits=24, decimal_places=8)
    stop_loss: Decimal | None = Field(None, gt=0, max_digits=24, decimal_places=8)
    trailing_stop_pct: Decimal | None = Field(None, gt=0, lt=100)
    session_id: str = Field(..., min_length=1)

    @field_validator("side", mode="before")
    @classmethod
    def _lower(cls, v: Any) -> Any:
        return v.lower() if isinstance(v, str) else v

    @field_validator("order_type", mode="before")
    @classmethod
    def _upper(cls, v: Any) -> Any:
        return v.upper() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.order_type == "LIMIT" and self.price is None:
            raise ValueError("price is required for LIMIT orders")
        if self.trailing_stop_pct is not None and (
            self.stop_loss is not None or self.take_profit is not None
        ):
            raise ValueError("trailing_stop_pct cannot be combined with stop_loss or take_profit")
        if (
            self.stop_loss is not None
            and self.take_profit is not None
            and self.stop_loss >= self.take_profit
        ):
            raise ValueError("stop_loss must be below take_profit")
        return self


class QuickTradeResponse(BaseModel):
    order_id: str | None
    session_id: str
    symbol: str
    side: str
    order_type: str
    quantity: float
    price: float | None = None
    status: str
    protection: str
    protective_order_id: str | None = None
    exit_orders_deferred: bool = False
    fill_price: float | None = None
    mark_source: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_session_manager(request: Request) -> Any:
    mgr = getattr(request.app.state, "session_manager", None)
    if mgr is None:
        raise api_error(503, "engine_not_ready", "Session manager not initialized", "unavailable")
    return mgr


def _actor(request: Request) -> str:
    principal = getattr(request.state, "principal", None)
    name = getattr(principal, "name", None)
    kind = getattr(principal, "kind", None)
    if name:
        return f"{kind}:{name}" if kind else str(name)
    return "anonymous"


def _session_to_dict(session: Any) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "strategy_id": session.strategy_id,
        "trading_mode": session.trading_mode,
        "status": session.status,
        "exchange_id": session.exchange_id,
        "symbols": session.symbols,
        "timeframe": session.timeframe,
        "paper_capital": float(session.paper_capital) if session.paper_capital else None,
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "stopped_at": session.stopped_at.isoformat() if session.stopped_at else None,
        "error_message": session.error_message,
        "positions_at_stop": session.positions_at_stop,
        "flatten_incomplete": session.flatten_incomplete,
        "persist_error": session.persist_error,
    }


def _start_error(exc: Exception) -> HTTPException:
    """Map a session-start failure to the shared envelope."""
    if isinstance(exc, KillSwitchActiveError):
        return api_error(409, exc.code, str(exc), "invalid")
    if isinstance(exc, DuplicateSessionError):
        return api_error(409, exc.code, str(exc), "invalid")
    if isinstance(exc, LiveTradingDisabledError):
        return api_error(403, exc.code, str(exc), "not_implemented")
    if isinstance(exc, UnsupportedVenueError):
        return api_error(400, exc.code, str(exc), "invalid")
    if isinstance(exc, UnsupportedStrategyError):
        return api_error(400, exc.code, str(exc), "not_implemented")
    if isinstance(exc, PaperCapitalUnavailableError):
        return api_error(503, exc.code, str(exc), "unavailable")
    if isinstance(exc, InsufficientHistoryError):
        return api_error(
            422,
            "insufficient_history",
            str(exc),
            "no_data",
            symbol=exc.symbol,
            timeframe=str(exc.timeframe),
            have=exc.have,
            need=exc.need,
        )
    if isinstance(exc, MarketRulesUnavailableError):
        return api_error(503, "market_rules_unavailable", str(exc), "unavailable")
    if isinstance(exc, KeyError | FileNotFoundError):
        return api_error(404, "strategy_not_found", str(exc).strip("'\""), "not_found")
    return api_error(400, "invalid_session", str(exc), "invalid")


# ---------------------------------------------------------------------------
# Session endpoints
# ---------------------------------------------------------------------------


@router.post("/sessions", response_model=SessionResponse, status_code=201)
async def start_session(body: StartSessionRequest, request: Request) -> dict[str, Any]:
    """Start a paper session. Live is 403 until P3; only Binance SPOT is accepted."""
    mgr = _get_session_manager(request)
    if body.trading_mode not in ("paper", "live"):
        raise api_error(
            400, "invalid_trading_mode", "trading_mode must be 'paper' or 'live'", "invalid"
        )
    try:
        session_id = await mgr.start_session(
            strategy_id=body.strategy_id,
            trading_mode=body.trading_mode,
            paper_capital=body.paper_capital,
        )
    except (
        KillSwitchActiveError,
        DuplicateSessionError,
        LiveTradingDisabledError,
        UnsupportedVenueError,
        UnsupportedStrategyError,
        PaperCapitalUnavailableError,
        InsufficientHistoryError,
        MarketRulesUnavailableError,
        KeyError,
        FileNotFoundError,
        ValueError,
    ) as exc:
        raise _start_error(exc) from exc
    audit(
        getattr(request.state, "principal", None),
        "session_start",
        session_id=session_id,
        strategy_id=body.strategy_id,
    )
    session = mgr.get_session(session_id)
    d = _session_to_dict(session)
    d["strategy_name"] = build_strategy_name_map().get(session.strategy_id)
    return d


@router.delete("/sessions/{session_id}", response_model=SessionResponse)
async def stop_session(session_id: str, request: Request) -> dict[str, Any]:
    """Flatten (reduce-only) and stop a session; positions_at_stop reports what is left."""
    mgr = _get_session_manager(request)
    try:
        await mgr.stop_session(session_id)
    except KeyError as exc:
        raise api_error(
            404, "session_not_found", f"Session {session_id} not found", "not_found"
        ) from exc
    except SessionPersistError as exc:
        audit(
            getattr(request.state, "principal", None),
            "session_stop",
            session_id=session_id,
            persisted=False,
        )
        raise api_error(
            503,
            exc.code,
            f"Session {session_id} stopped, but the database still says running: {exc}",
            "unavailable",
        ) from exc
    audit(getattr(request.state, "principal", None), "session_stop", session_id=session_id)
    session = mgr.get_session(session_id)
    d = _session_to_dict(session)
    d["strategy_name"] = build_strategy_name_map().get(session.strategy_id)
    return d


@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(request: Request) -> list[dict[str, Any]]:
    """List all trading sessions (running + recent stopped) with each one's health."""
    mgr = _get_session_manager(request)
    name_map = build_strategy_name_map()
    try:
        snap = await asyncio.wait_for(mgr.health_snapshot(), SNAPSHOT_TIMEOUT_S)
        health = {h.session_id: h for h in snap.sessions}
    except Exception:
        logger.warning("Session list: health snapshot failed", exc_info=True)
        health = {}
    results = []
    for s in mgr.list_sessions():
        d = _session_to_dict(s)
        d["strategy_name"] = name_map.get(s.strategy_id)
        h = health.get(s.session_id)
        if h is not None:
            d["last_bar_at"] = h.last_bar_at.isoformat() if h.last_bar_at else None
            d["entries_paused"] = h.entries_paused
            d["persist_dirty"] = h.persist_dirty
            d["feed_live"] = h.feed_live
        results.append(d)
    return results


@router.get("/sessions/{session_id}/detail", response_model=SessionDetailResponse)
async def get_session_detail(session_id: str, request: Request) -> dict[str, Any]:
    """Full session detail with account numbers, positions and fills."""
    mgr = _get_session_manager(request)
    try:
        detail = await mgr.get_session_detail(session_id)
    except KeyError as exc:
        raise api_error(
            404, "session_not_found", f"Session {session_id} not found", "not_found"
        ) from exc
    detail["strategy_name"] = build_strategy_name_map().get(detail.get("strategy_id", ""))
    session = mgr.get_session(session_id)
    executor = getattr(session, "_executor", None)
    if isinstance(executor, PaperTradingExecutor):
        rt = session._runtime
        detail["open_orders"] = [
            {**o, "protective": rt.protect.is_protective(o["oco_id"] or o["id"]) if rt else None}
            for o in await executor.fetch_open_orders()
        ]
    return dict(detail)


# ---------------------------------------------------------------------------
# Kill switch endpoints
# ---------------------------------------------------------------------------


@router.post("/kill-switch", response_model=KillSwitchResponse)
async def activate_kill_switch(request: Request) -> dict[str, Any]:
    """Latch the kill switch, then flatten and stop every running or errored session."""
    mgr = _get_session_manager(request)
    report = await mgr.stop_all(actor=_actor(request), reason="kill switch (api)")
    audit(
        getattr(request.state, "principal", None),
        "kill_switch_on",
        sessions_stopped=report.sessions_stopped,
        positions_remaining=report.positions_remaining,
        persisted=report.persisted,
    )
    if report.positions_remaining:
        message = (
            f"Kill switch latched; {report.positions_remaining} position(s) could NOT be "
            "closed - check the sessions"
        )
    else:
        message = "Kill switch activated - all positions closed and trading halted"
    body = {
        "active": True,
        "message": message,
        "sessions_stopped": report.sessions_stopped,
        "positions_remaining": report.positions_remaining,
        "persisted": report.persisted,
        "flatten_failures": list(report.flatten_failures),
    }
    if not report.persisted:
        raise api_error(
            503,
            "kill_switch_not_persisted",
            message + "; the latch is in memory only and is lost on restart",
            "unavailable",
            **{k: v for k, v in body.items() if k != "message"},
        )
    return body


@router.delete("/kill-switch", response_model=KillSwitchResponse)
async def release_kill_switch(body: KillSwitchRelease, request: Request) -> dict[str, Any]:
    """Release the kill switch (``{"confirm": "RELEASE", "reason": ...}``)."""
    mgr = _get_session_manager(request)
    require_pool(request)
    try:
        await mgr.release_kill_switch(actor=_actor(request), reason=body.reason)
    except Exception as exc:
        logger.exception("Kill switch release not persisted")
        raise api_error(
            503,
            "kill_switch_release_failed",
            "The release could not be persisted; the kill switch stays active",
            "unavailable",
        ) from exc
    audit(getattr(request.state, "principal", None), "kill_switch_off", reason=body.reason)
    return {
        "active": False,
        "message": "Kill switch released - trading can resume",
        "sessions_stopped": 0,
    }


# ---------------------------------------------------------------------------
# Quick-trade endpoint
# ---------------------------------------------------------------------------


@router.post("/quick-trade", response_model=QuickTradeResponse)
async def quick_trade(body: QuickTradeRequest, request: Request) -> dict[str, Any]:
    """A risk-checked manual order on the running session ``session_id``.

    SL/TP (or a trailing stop) are placed reduce-only when the entry fills; without them the
    strategy's protective stop (default 5 %) is placed. A SELL only reduces the held position.
    """
    mgr = _get_session_manager(request)
    side = Side.BUY if body.side == "buy" else Side.SELL
    protection: ProtectionSpec | None = None
    if body.stop_loss is not None or body.take_profit is not None or body.trailing_stop_pct:
        protection = ProtectionSpec(
            stop_loss=body.stop_loss,
            take_profit=body.take_profit,
            trailing_pct=body.trailing_stop_pct / 100 if body.trailing_stop_pct else None,
        )
    try:
        session, result = await mgr.submit_manual_order(
            symbol=body.symbol,
            side=side,
            order_type=OrderType(body.order_type),
            quantity=body.quantity,
            price=body.price,
            protection=protection,
            session_id=body.session_id,
        )
    except SessionNotRunningError as exc:
        raise api_error(404, exc.code, str(exc), "not_found") from exc
    except SpotShortError as exc:
        raise api_error(422, exc.code, str(exc), "invalid") from exc
    except RiskRejectedError as exc:
        raise api_error(
            422, "risk_rejected", exc.reason, "invalid", check=exc.check, intent=exc.intent
        ) from exc
    except ValueError as exc:
        raise api_error(422, "order_rejected", str(exc), "invalid") from exc
    except PersistenceUnavailableError as exc:
        raise api_error(503, "paper_state_unpersisted", str(exc), "unavailable") from exc
    except PaperSessionHaltedError as exc:
        raise api_error(503, "paper_session_halted", str(exc), "unavailable") from exc
    audit(
        getattr(request.state, "principal", None),
        "manual_order",
        session_id=session.session_id,
        symbol=body.symbol,
        side=body.side,
        order_type=body.order_type,
        quantity=str(body.quantity),
        order_id=result.order_id,
    )
    return {
        "order_id": result.order_id,
        "session_id": session.session_id,
        "symbol": body.symbol,
        "side": side.value,
        "order_type": body.order_type,
        "quantity": float(result.quantity),
        "price": float(body.price) if body.price is not None else None,
        "status": result.status,
        "protection": result.protection,
        "protective_order_id": result.protective_order_id,
        "exit_orders_deferred": side == Side.BUY and result.protection == "pending",
        "fill_price": float(result.fill_price) if result.fill_price is not None else None,
        "mark_source": result.mark_source,
    }
