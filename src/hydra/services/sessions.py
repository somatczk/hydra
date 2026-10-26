"""Session, kill-switch and quick-trade operations, shared by REST routes and MCP tools.

``SessionManager`` stays the single owner of session runtimes; these functions wrap its
calls with the shared error envelope — the exact codes, states and extras
``dashboard/routes/trading.py`` answered before the extraction — and build the response
dicts. The acting principal arrives as an actor string (``user:<name>`` from the routes,
``service:mcp`` from the tools), so both transports attribute identically: the kill-switch
``risk_events`` rows, ``trading_sessions.created_by`` and the audit lines.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hydra.core.types import OrderType, Side
from hydra.data.ingestion import InsufficientHistoryError
from hydra.execution import session_store
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
from hydra.services.errors import (
    DB_ERRORS,
    ServiceError,
    error_detail,
    require_pool,
    service_error,
)
from hydra.services.strategies import build_strategy_name_map

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request models (the routes and the MCP tools construct and revalidate these)
# ---------------------------------------------------------------------------


class StartSessionRequest(BaseModel):
    strategy_id: str
    trading_mode: str = "paper"  # 'paper' | 'live'
    paper_capital: Decimal | None = Field(None, gt=0, max_digits=24, decimal_places=8)


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


class SessionStopNotPersistedError(ServiceError):
    """The session stopped, but its terminal row did not reach the DB (the route still audits)."""


class KillSwitchNotPersistedError(ServiceError):
    """``stop_all`` latched in memory only; the 503 detail carries the full report."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def require_manager(mgr: Any) -> Any:
    """The engine's SessionManager, or the 503 envelope both transports answer."""
    if mgr is None:
        raise service_error(
            503, "engine_not_ready", "Session manager not initialized", "unavailable"
        )
    return mgr


def session_to_dict(session: Any) -> dict[str, Any]:
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
        "created_by": session.created_by,
    }


def _start_error(exc: Exception) -> ServiceError:
    """Map a session-start failure to the shared envelope (the routes' former table)."""
    if isinstance(exc, KillSwitchActiveError):
        return service_error(409, exc.code, str(exc), "invalid")
    if isinstance(exc, DuplicateSessionError):
        return service_error(409, exc.code, str(exc), "invalid")
    if isinstance(exc, LiveTradingDisabledError):
        return service_error(403, exc.code, str(exc), "not_implemented")
    if isinstance(exc, UnsupportedVenueError):
        return service_error(400, exc.code, str(exc), "invalid")
    if isinstance(exc, UnsupportedStrategyError):
        return service_error(400, exc.code, str(exc), "not_implemented")
    if isinstance(exc, PaperCapitalUnavailableError):
        return service_error(503, exc.code, str(exc), "unavailable")
    if isinstance(exc, InsufficientHistoryError):
        return service_error(
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
        return service_error(503, "market_rules_unavailable", str(exc), "unavailable")
    if isinstance(exc, KeyError | FileNotFoundError):
        return service_error(404, "strategy_not_found", str(exc).strip("'\""), "not_found")
    return service_error(400, "invalid_session", str(exc), "invalid")


# ---------------------------------------------------------------------------
# Session operations
# ---------------------------------------------------------------------------


async def start_session(mgr: Any, actor: str, body: StartSessionRequest) -> dict[str, Any]:
    """Start a paper session (live is refused until P3) attributed to *actor*."""
    mgr = require_manager(mgr)
    if body.trading_mode not in ("paper", "live"):
        raise service_error(
            400, "invalid_trading_mode", "trading_mode must be 'paper' or 'live'", "invalid"
        )
    try:
        session_id = await mgr.start_session(
            strategy_id=body.strategy_id,
            trading_mode=body.trading_mode,
            paper_capital=body.paper_capital,
            actor=actor,
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
    session = mgr.get_session(session_id)
    d = session_to_dict(session)
    d["strategy_name"] = build_strategy_name_map().get(session.strategy_id)
    return d


async def stop_session(mgr: Any, session_id: str) -> dict[str, Any]:
    """Flatten (reduce-only) and stop a session; positions_at_stop reports what is left."""
    mgr = require_manager(mgr)
    try:
        await mgr.stop_session(session_id)
    except KeyError as exc:
        raise service_error(
            404, "session_not_found", f"Session {session_id} not found", "not_found"
        ) from exc
    except SessionPersistError as exc:
        raise SessionStopNotPersistedError(
            503,
            error_detail(
                exc.code,
                f"Session {session_id} stopped, but the database still says running: {exc}",
                "unavailable",
            ),
        ) from exc
    session = mgr.get_session(session_id)
    d = session_to_dict(session)
    d["strategy_name"] = build_strategy_name_map().get(session.strategy_id)
    return d


def list_sessions(mgr: Any) -> list[dict[str, Any]]:
    """Every session (running + recent stopped) with each one's health fields."""
    mgr = require_manager(mgr)
    name_map = build_strategy_name_map()
    results = []
    for s in mgr.list_sessions():
        d = session_to_dict(s)
        d["strategy_name"] = name_map.get(s.strategy_id)
        # health_snapshot()'s reads without its DB query, so a DB stall never slows this poll
        rt, executor = s._runtime, s._executor
        d["last_bar_at"] = s.last_bar_at.isoformat() if s.last_bar_at else None
        d["entries_paused"] = rt.entries_paused if rt is not None else None
        d["feed_live"] = (
            rt.feed.is_live(s.exchange_id) if rt is not None and s.task_alive else False
        )
        d["persist_dirty"] = (
            executor.get_account_snapshot().persist_dirty
            if isinstance(executor, PaperTradingExecutor)
            else None
        )
        results.append(d)
    return results


async def get_session_detail(mgr: Any, session_id: str) -> dict[str, Any]:
    """Full session detail with account numbers, positions and fills."""
    mgr = require_manager(mgr)
    try:
        detail = await mgr.get_session_detail(session_id)
    except KeyError as exc:
        raise service_error(
            404, "session_not_found", f"Session {session_id} not found", "not_found"
        ) from exc
    detail["strategy_name"] = build_strategy_name_map().get(detail.get("strategy_id", ""))
    session = mgr.get_session(session_id)
    detail["created_by"] = session.created_by if session is not None else None
    executor = getattr(session, "_executor", None)
    if isinstance(executor, PaperTradingExecutor):
        rt = session._runtime
        detail["open_orders"] = [
            {
                **o,
                # a LIMIT's stop_price is only its risk-check stop: nothing rests there
                "stop_price": None if o["type"] == OrderType.LIMIT else o["stop_price"],
                "protective": rt.protect.is_protective(o["oco_id"] or o["id"]) if rt else None,
            }
            for o in await executor.fetch_open_orders()
        ]
    return dict(detail)


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


async def activate_kill_switch(mgr: Any, actor: str, reason: str) -> dict[str, Any]:
    """Latch the kill switch, then flatten and stop every running or errored session."""
    mgr = require_manager(mgr)
    report = await mgr.stop_all(actor=actor, reason=reason)
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
        raise KillSwitchNotPersistedError(
            503,
            error_detail(
                "kill_switch_not_persisted",
                message + "; the latch is in memory only and is lost on restart",
                "unavailable",
                **{k: v for k, v in body.items() if k != "message"},
            ),
        )
    return body


async def release_kill_switch(pool: Any, mgr: Any, actor: str, reason: str) -> dict[str, Any]:
    """Release the kill switch; a release that cannot be persisted stays latched (503)."""
    mgr = require_manager(mgr)
    require_pool(pool)
    try:
        await mgr.release_kill_switch(actor=actor, reason=reason)
    except Exception as exc:
        logger.exception("Kill switch release not persisted")
        raise service_error(
            503,
            "kill_switch_release_failed",
            "The release could not be persisted; the kill switch stays active",
            "unavailable",
        ) from exc
    return {
        "active": False,
        "message": "Kill switch released - trading can resume",
        "sessions_stopped": 0,
    }


# ---------------------------------------------------------------------------
# Quick trade and webhook signal
# ---------------------------------------------------------------------------


async def _refuse_live_webhook_target(pool: Any, *, symbol: str, strategy_id: str) -> None:
    """Refuse with 409 ``live_gate_required`` when the target's running row is live-mode.

    The target's mode is read from the database — the truth the D5 process split shares.
    An unreadable check degrades to today's behaviour: nothing live can run before P3-A,
    whose live branch owns the fail-closed posture.
    """
    if pool is None:
        return
    try:
        rows = await session_store.load_running(pool)
    except DB_ERRORS:
        logger.exception("Webhook live-gate read failed for strategy %s", strategy_id)
        return
    for row in rows:
        if (
            row["strategy_id"] == strategy_id
            and row["trading_mode"] == "live"
            and symbol in row["symbols"]
        ):
            raise service_error(
                409,
                "live_gate_required",
                f"Strategy '{strategy_id}' has a running live session; webhook signals "
                "cannot place live orders",
                "invalid",
            )


async def submit_webhook_order(
    mgr: Any,
    *,
    symbol: str,
    side: str,
    action: str,
    quantity: Decimal | None,
    strategy_id: str,
    pool: Any = None,
    webhook_origin: bool = False,
) -> dict[str, Any]:
    """A webhook-originated signal on the first running session of *strategy_id*.

    A call marked ``webhook_origin`` (the R3 seam) refuses a live-mode target with 409
    ``live_gate_required`` before any order is placed: an external signal never trades a
    live session. Unmarked calls keep today's behaviour; nothing live can run before P3-A.

    An entry is a buy, an exit a reduce-only sell; an entry without ``quantity`` is sized
    like a strategy entry. Returns ``{"session_id", "order_id", "status"}``; unknown
    failures propagate for the caller's 502.
    """
    if mgr is None:
        raise service_error(
            503, "engine_not_ready", "Trading engine not initialized", "unavailable"
        )
    if webhook_origin:
        await _refuse_live_webhook_target(pool, symbol=symbol, strategy_id=strategy_id)
    is_exit = action == "exit"
    if not is_exit and side == "sell":
        raise service_error(
            422, "spot_short", "Short entries are not supported on spot sessions", "invalid"
        )
    try:
        session, result = await mgr.submit_manual_order(
            symbol=symbol,
            side=Side.SELL if is_exit else Side.BUY,
            order_type=OrderType.MARKET,
            quantity=quantity,
            strategy_id=strategy_id,
        )
    except SessionNotRunningError as exc:
        raise service_error(
            404,
            "session_not_found",
            f"No running session for strategy_id '{strategy_id}'",
            "not_found",
        ) from exc
    except SpotShortError as exc:
        if is_exit:
            raise service_error(
                409, "no_position", f"No open {symbol} position to exit", "no_data"
            ) from exc
        raise service_error(422, exc.code, str(exc), "invalid") from exc
    except RiskRejectedError as exc:
        raise service_error(
            422, "risk_rejected", exc.reason, "invalid", check=exc.check, intent=exc.intent
        ) from exc
    except ValueError as exc:
        raise service_error(422, "order_rejected", str(exc), "invalid") from exc
    except PersistenceUnavailableError as exc:
        raise service_error(503, "paper_state_unpersisted", str(exc), "unavailable") from exc
    except PaperSessionHaltedError as exc:
        raise service_error(503, "paper_session_halted", str(exc), "unavailable") from exc
    return {
        "session_id": session.session_id,
        "order_id": result.order_id,
        "status": result.status,
    }


async def submit_manual_order(mgr: Any, order: QuickTradeRequest) -> dict[str, Any]:
    """A risk-checked manual order on the running session ``order.session_id``.

    SL/TP (or a trailing stop) are placed reduce-only when the entry fills; without them
    the strategy's protective stop (default 5 %) is placed. A SELL only reduces the held
    position.
    """
    mgr = require_manager(mgr)
    side = Side.BUY if order.side == "buy" else Side.SELL
    protection: ProtectionSpec | None = None
    if order.stop_loss is not None or order.take_profit is not None or order.trailing_stop_pct:
        protection = ProtectionSpec(
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            trailing_pct=order.trailing_stop_pct / 100 if order.trailing_stop_pct else None,
        )
    try:
        session, result = await mgr.submit_manual_order(
            symbol=order.symbol,
            side=side,
            order_type=OrderType(order.order_type),
            quantity=order.quantity,
            price=order.price,
            protection=protection,
            session_id=order.session_id,
        )
    except SessionNotRunningError as exc:
        raise service_error(404, exc.code, str(exc), "not_found") from exc
    except SpotShortError as exc:
        raise service_error(422, exc.code, str(exc), "invalid") from exc
    except RiskRejectedError as exc:
        raise service_error(
            422, "risk_rejected", exc.reason, "invalid", check=exc.check, intent=exc.intent
        ) from exc
    except ValueError as exc:
        raise service_error(422, "order_rejected", str(exc), "invalid") from exc
    except PersistenceUnavailableError as exc:
        raise service_error(503, "paper_state_unpersisted", str(exc), "unavailable") from exc
    except PaperSessionHaltedError as exc:
        raise service_error(503, "paper_session_halted", str(exc), "unavailable") from exc
    return {
        "order_id": result.order_id,
        "session_id": session.session_id,
        "symbol": order.symbol,
        "side": side.value,
        "order_type": order.order_type,
        "quantity": float(result.quantity),
        "price": float(order.price) if order.price is not None else None,
        "status": result.status,
        "protection": result.protection,
        "protective_order_id": result.protective_order_id,
        "exit_orders_deferred": side == Side.BUY and result.protection == "pending",
        "fill_price": float(result.fill_price) if result.fill_price is not None else None,
        "mark_source": result.mark_source,
    }
