"""Session, kill-switch and quick-trade operations, shared by REST routes and MCP tools.

``SessionManager`` stays the single owner of session runtimes; these functions wrap its
calls with the shared error envelope — the exact codes, states and extras
``dashboard/routes/trading.py`` answered before the extraction — and build the response
dicts. The acting principal arrives as an actor string (``user:<name>`` from the routes,
``service:mcp`` from the tools), so both transports attribute identically: the kill-switch
``risk_events`` rows, ``trading_sessions.created_by`` and the audit lines.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hydra.core.types import OrderType, Side
from hydra.data.ingestion import InsufficientHistoryError
from hydra.execution import session_store
from hydra.execution.errors import (
    DuplicateSessionError,
    ExchangeRateLimitedError,
    KillSwitchActiveError,
    LiveCapitalUnavailableError,
    LiveCredentialsError,
    LiveNotArmedError,
    PaperCapitalUnavailableError,
    SessionNotRunningError,
    SessionPersistError,
    SpotShortError,
    SubmitUnknownError,
    UnsupportedStrategyError,
    UnsupportedVenueError,
)
from hydra.execution.exchange_stops import (
    ExchangeStopState,
    normalize_open_orders,
    verify_exchange_stop,
)
from hydra.execution.live_orders import LiveOrderRecord, PgLiveOrderStore
from hydra.execution.order_manager import RiskRejectedError
from hydra.execution.paper_trading import (
    PaperSessionHaltedError,
    PaperTradingExecutor,
    PersistenceUnavailableError,
)
from hydra.execution.perp_live import (
    LivePerpTradingExecutor,
    normalize_perp_leg,
    verify_perp_ladder,
)
from hydra.execution.protective_orders import ProtectionSpec
from hydra.ml.consumers import validate_session_ml
from hydra.ml.serving import MlServingError
from hydra.risk.market_rules import MarketRulesUnavailableError
from hydra.risk.state import RiskStateUnavailableError
from hydra.services.errors import (
    DB_ERRORS,
    ServiceError,
    db_errors,
    error_detail,
    require_pool,
    service_error,
)
from hydra.services.perps_gate import is_perp_strategy, require_perps_gate
from hydra.services.strategies import (
    build_strategy_name_map,
    find_strategy_file,
    parse_strategy_file,
)
from hydra.strategy.builtin.ml_ensemble import STRATEGY_CLASS as ML_NATIVE_CLASS
from hydra.strategy.config import StrategyConfig

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


def session_to_dict(session: Any, *, reconcile_state: str | None = None) -> dict[str, Any]:
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
        "reconcile_state": reconcile_state,
    }


def _reconcile_state(mgr: Any, session: Any) -> str | None:
    """The list field: a live session's reconcile state ('never' before the first pass)."""
    reader = getattr(mgr, "reconcile_status", None)
    if reader is None:
        return None
    status = reader(session.session_id)
    return None if status is None else status.state


def _start_error(exc: Exception) -> ServiceError:
    """Map a session-start failure to the shared envelope (the routes' former table)."""
    if isinstance(exc, KillSwitchActiveError):
        return service_error(409, exc.code, str(exc), "invalid")
    if isinstance(exc, DuplicateSessionError):
        return service_error(409, exc.code, str(exc), "invalid")
    if isinstance(exc, LiveNotArmedError):
        return service_error(409, exc.code, str(exc), "invalid")
    if isinstance(exc, LiveCredentialsError):
        return service_error(403, exc.code, str(exc), "invalid")
    if isinstance(exc, LiveCapitalUnavailableError):
        return service_error(403, exc.code, str(exc), "invalid")
    if isinstance(exc, RiskStateUnavailableError):
        return service_error(503, "live_risk_unavailable", str(exc), "unavailable")
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
    if isinstance(exc, MlServingError):
        if exc.kind in ("artifact_missing", "unavailable"):
            return service_error(503, f"ml_model_{exc.kind}", str(exc), "unavailable")
        if exc.kind == "not_promoted":
            return service_error(422, "ml_model_not_promoted", str(exc), "invalid")
        return service_error(422, "ml_model_invalid", str(exc), "invalid")
    if isinstance(exc, KeyError | FileNotFoundError):
        return service_error(404, "strategy_not_found", str(exc).strip("'\""), "not_found")
    return service_error(400, "invalid_session", str(exc), "invalid")


LIVE_ARMED_KEY = "live_armed"


async def _validate_session_ml(mgr: Any, strategy_id: str) -> None:
    """P5-C: the fail-closed session-start gate for a strategy that consumes a
    Production model (an enabled ``ml_overlay`` or the ML-native strategy) —
    validated at START, never at save.

    A strategy that consumes no model changes nothing here: the validation
    resolves nothing beyond its file and returns before touching the engine's
    config or pool, so non-ML starts keep their exact envelopes and error
    priority. A strategy that does consume a model must find it attachable
    NOW: registry → artifact → onnxruntime → the frozen-spec metadata check,
    and the native strategy's predictor is attached so the session starts
    with its model loaded. Resolution failures defer to the manager's own
    start refusals (the same 404/422 the probe would raise).
    """
    path = find_strategy_file(strategy_id)
    if path is None:
        return
    data = parse_strategy_file(path)
    if data is None:
        return
    overlay = data.get("ml_overlay") or {}
    consumes_model = bool(overlay.get("enabled")) or (data.get("strategy_class") == ML_NATIVE_CLASS)
    if not consumes_model:
        return
    try:
        cfg = StrategyConfig(**data)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid strategy config for {strategy_id}: {exc}") from exc
    hydra_config = getattr(mgr, "_config", None)
    pool = getattr(mgr, "_db_pool", None)
    if hydra_config is None or pool is None:
        raise MlServingError(
            "unavailable",
            f"strategy {strategy_id} consumes a Production model but the model "
            "registry is unreachable (no database pool); the session refuses "
            "to start without the model",
        )
    await validate_session_ml(pool, Path(hydra_config.ml.models_dir), cfg)


async def _require_live_armed(mgr: Any) -> None:
    """R1: the armed row is the single master gate; anything unreadable is disarmed.

    A DB read that works in the trader process: absent row, no pool, a failed read or a
    malformed value all count as disarmed — the live start is refused, never guessed.
    """
    pool = getattr(mgr, "_db_pool", None)
    row: Any = None
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT value FROM system_config WHERE key = $1", LIVE_ARMED_KEY
                )
        except Exception:
            logger.exception("Live-arming state unreadable; refusing the live start")
            row = None
    armed = False
    if row is not None:
        value = row["value"]
        try:
            value = json.loads(value) if isinstance(value, str) else value
            armed = isinstance(value, dict) and bool(value.get("armed"))
        except (TypeError, ValueError):
            armed = False
    if not armed:
        raise LiveNotArmedError(
            "live trading is not armed; arm it through the code-gated ceremony first"
        )


# ---------------------------------------------------------------------------
# Session operations
# ---------------------------------------------------------------------------


async def start_session(mgr: Any, actor: str, body: StartSessionRequest) -> dict[str, Any]:
    """Start a paper or live session attributed to *actor*.

    A live start begins with the venue's gate: the spot ``live_armed`` system_config row
    is the single master gate for spot strategies and anything unreadable counts as
    disarmed; a perps strategy instead answers to the separate
    ``perps_armed``/``perps_capital`` gate (P4-E) — a DB read that works in the trader
    process. Only then does the manager's live branch check credentials, ``live_capital``
    and the kill switch.
    """
    mgr = require_manager(mgr)
    if body.trading_mode not in ("paper", "live"):
        raise service_error(
            400, "invalid_trading_mode", "trading_mode must be 'paper' or 'live'", "invalid"
        )
    if body.trading_mode == "live":
        if is_perp_strategy(body.strategy_id):
            # P4-E: a perps strategy answers to the separate perps gate, not the spot row
            await require_perps_gate(getattr(mgr, "_db_pool", None))
        else:
            try:
                await _require_live_armed(mgr)
            except LiveNotArmedError as exc:
                raise _start_error(exc) from exc
    try:
        await _validate_session_ml(mgr, body.strategy_id)
    except (MlServingError, TypeError, ValueError) as exc:
        raise _start_error(exc) from exc
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
        LiveNotArmedError,
        LiveCredentialsError,
        LiveCapitalUnavailableError,
        RiskStateUnavailableError,
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
    d = session_to_dict(session, reconcile_state=_reconcile_state(mgr, session))
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
    d = session_to_dict(session, reconcile_state=_reconcile_state(mgr, session))
    d["strategy_name"] = build_strategy_name_map().get(session.strategy_id)
    return d


def list_sessions(mgr: Any) -> list[dict[str, Any]]:
    """Every session (running + recent stopped) with each one's health fields."""
    mgr = require_manager(mgr)
    name_map = build_strategy_name_map()
    results = []
    for s in mgr.list_sessions():
        d = session_to_dict(s, reconcile_state=_reconcile_state(mgr, s))
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
    if session is not None and (
        session.trading_mode == "live" or getattr(executor, "exchange_client", None) is not None
    ):
        # not isinstance: LiveTradingExecutor subclasses the paper executor (R12)
        await _live_exchange_state(detail, session, executor)
    elif isinstance(executor, PaperTradingExecutor):
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
    if session is not None and session.trading_mode == "live":
        await _live_reconcile_reads(mgr, detail, session)
    return dict(detail)


async def _live_exchange_state(detail: dict[str, Any], session: Any, executor: Any) -> None:
    """A8-live (R12): the exchange is the truth for open orders and each position's stop.

    Runs only for an executor that exposes its keyed ``exchange_client`` (the live executor,
    P3-A); paper positions never carry ``exchange_stop``. A live PERP session reads BOTH
    fapi namespaces and verifies through ``verify_perp_ladder`` — futures have no OCO, so
    the spot reads would answer ``missing`` for a correctly resting ladder. Failures per
    symbol surface as state ``unknown`` with the error string, never ``missing``.
    """
    client = getattr(executor, "exchange_client", None)
    if client is None:
        return
    perp = isinstance(executor, LivePerpTradingExecutor)
    if not perp and getattr(client, "fetch_open_order_lists", None) is None:
        return
    protect = session._runtime.protect if session._runtime is not None else None
    orders: list[dict[str, Any]] = []
    for symbol in session.symbols:
        try:
            if perp:
                legs = [normalize_perp_leg(o) for o in await client.fetch_open_orders(symbol)]
                legs.extend(
                    normalize_perp_leg(o)
                    for o in await client.fetch_open_orders(symbol, conditional=True)
                )
            else:
                legs = normalize_open_orders(await client.fetch_open_orders(symbol))
        except Exception as exc:
            logger.warning("Live open orders of %s unreadable: %s", symbol, exc)
            continue
        for leg in legs:
            leg["protective"] = _leg_is_protective(protect, leg, perp=perp)
            orders.append(leg)
    detail["open_orders"] = orders
    for position in detail.get("positions") or []:
        expected = protect.armed(position.get("symbol")) if protect is not None else None
        try:
            if perp:
                state = await verify_perp_ladder(client, position.get("symbol"), expected)
            else:
                state = await verify_exchange_stop(client, position.get("symbol"), expected)
        except Exception as exc:
            state = ExchangeStopState(state="unknown", checked_at=datetime.now(UTC), error=str(exc))
        position["exchange_stop"] = {
            "state": state.state,
            "kind": state.kind,
            "stop_price": state.stop_price,
            "take_profit": state.take_profit,
            "order_list_id": state.order_list_id,
            "checked_at": state.checked_at.isoformat(),
            "error": state.error,
        }


def _leg_is_protective(protect: Any, leg: dict[str, Any], *, perp: bool) -> bool | None:
    """Is this exchange leg one of the session's protective groups?

    A perp ladder leg matches by its numeric algo id (a running group's leg ids)
    or its leg client id (an adopted group's); a spot leg by its OCO list id.
    """
    if protect is None:
        return None
    if perp:
        keys = (str(leg.get("id")), str(leg.get("client_order_id") or ""))
        return any(protect.is_protective(key) for key in keys if key)
    return bool(protect.is_protective(str(leg.get("oco_id") or leg["id"])))


async def _live_reconcile_reads(mgr: Any, detail: dict[str, Any], session: Any) -> None:
    """A7 for a live session: the tracked order rows (newest first, cap 100), the
    reconcile state, and R20's ``reconciled`` flag on every fill."""
    reader = getattr(mgr, "reconcile_status", None)
    status = reader(session.session_id) if reader is not None else None
    if status is not None:
        detail["reconcile"] = {
            "state": status.state,
            "last_reconciled_at": (
                status.last_reconciled_at.isoformat() if status.last_reconciled_at else None
            ),
            "detail": status.detail,
        }
        covered_at = status.covered_at
        for trade in detail.get("trades") or []:
            trade["reconciled"] = _covered(trade.get("timestamp"), covered_at)
    detail["orders"] = [live_order_row(row) for row in await _live_order_rows(mgr, session)]


def _covered(timestamp: Any, covered_at: datetime | None) -> bool:
    """R20: a reconcile pass with state ok|mismatch covered this fill or it did not."""
    if covered_at is None or not isinstance(timestamp, str):
        return False
    try:
        at = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return at <= covered_at


async def _live_order_rows(mgr: Any, session: Any) -> list[LiveOrderRecord]:
    """The tracked rows from the running executor's store, else the live_orders table."""
    store = getattr(getattr(session, "_executor", None), "live_orders", None)
    if store is None:
        pool = getattr(mgr, "_db_pool", None)
        if pool is None:
            return []
        store = PgLiveOrderStore(pool)
    return await store.orders(session.session_id, 100)


def live_order_row(row: LiveOrderRecord) -> dict[str, Any]:
    """One tracked live order in the pinned seam #2 shape (16 fields)."""
    return {
        "client_order_id": row.client_order_id,
        "exchange_order_id": row.exchange_order_id,
        "symbol": row.symbol,
        "side": row.side,
        "order_type": row.order_type,
        "quantity": float(row.quantity),
        "price": None if row.price is None else float(row.price),
        "stop_price": None if row.stop_price is None else float(row.stop_price),
        "filled_quantity": float(row.filled_quantity),
        "average_fill_price": (
            None if row.average_fill_price is None else float(row.average_fill_price)
        ),
        "status": row.status.value,
        "tag": row.tag,
        "actor": row.actor,
        "submitted_at": None if row.submitted_at is None else row.submitted_at.isoformat(),
        "terminal_at": None if row.terminal_at is None else row.terminal_at.isoformat(),
        "error": row.error,
    }


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------

LATCH_APPLY_WINDOW_S = 60
_LATCH_WINDOW_NOTE = (
    "The trading engine could not be reached; the kill switch was latched in the database "
    f"and the trader applies it within {LATCH_APPLY_WINDOW_S} seconds once it returns "
    "(nothing applies while it is down)"
)


async def latch_kill_switch_db(pool: Any, actor: str, reason: str) -> None:
    """D5's fail-safe: latch the switch in the database when the trader cannot be reached.

    The write is the durable half — the same ``risk_config`` row the engine's RiskService
    latches, plus its audit event — and the trader's 60 s cleanup loop applies it
    (``stop_all``) within :data:`LATCH_APPLY_WINDOW_S` seconds. Scope ``global`` is the
    switch's own row, so the event names the switch, not one ledger.
    """
    require_pool(pool)
    async with db_errors("latching the kill switch"), pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE risk_config SET kill_switch_active = TRUE, updated_at = now() "
            "WHERE scope = 'global'"
        )
        if str(result).strip() == "UPDATE 0":
            raise service_error(
                503,
                "risk_config_missing",
                "No risk_config row for scope 'global'",
                "unavailable",
            )
        await conn.execute(
            "INSERT INTO risk_events (scope, kind, tier, actor, reason, details) "
            "VALUES ('global', 'kill_switch_on', NULL, $1, $2, $3::jsonb)",
            actor,
            reason,
            json.dumps({"persisted": True, "source": "db_latch"}),
        )


async def activate_kill_switch_fail_safe(
    backend: Any, pool: Any, actor: str, reason: str
) -> dict[str, Any]:
    """Activate through the backend; on ``engine_unreachable`` latch in the DB and 503.

    The 503 answers honestly: the latch is durable and the trader applies it within one
    cleanup tick once it returns — but this call does NOT report sessions stopped — the
    engine never answered, so nobody knows whether it is still running.
    """
    try:
        body: dict[str, Any] = await backend.activate_kill_switch(actor, reason)
        return body
    except ServiceError as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if exc.status_code != 503 or detail.get("code") != "engine_unreachable":
            raise
        await latch_kill_switch_db(pool, actor, reason)
        raise ServiceError(
            503,
            error_detail(
                "engine_unreachable",
                _LATCH_WINDOW_NOTE,
                "unavailable",
                latched=True,
                apply_window_s=LATCH_APPLY_WINDOW_S,
            ),
        ) from exc


async def release_kill_switch_fail_safe(backend: Any, actor: str, reason: str) -> dict[str, Any]:
    """Release through the backend; an unreachable trader keeps the switch LATCHED (503).

    Mirrors the activate leg's honest shape: nothing about the switch changes through a
    dead trader — it stays ON until the trader returns and the release is re-fired.
    """
    try:
        body: dict[str, Any] = await backend.release_kill_switch(actor, reason)
        return body
    except ServiceError as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if exc.status_code != 503 or detail.get("code") != "engine_unreachable":
            raise
        raise ServiceError(
            503,
            error_detail(
                "engine_unreachable",
                "The trading engine could not be reached; the kill switch stays LATCHED "
                "— re-release it once the trader returns",
                "unavailable",
                latched=True,
            ),
        ) from exc


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


def _live_gate_unreadable() -> ServiceError:
    """R25: a webhook-originated order never flows past an unreadable mode read."""
    return service_error(
        503,
        "live_gate_unreadable",
        "The target session's trading mode could not be read; webhook signals are refused",
        "unavailable",
    )


async def refuse_live_webhook_target(pool: Any, *, symbol: str, strategy_id: str) -> None:
    """Refuse with 409 ``live_gate_required`` when the target's running row is live-mode.

    The target's mode is read from the database — the truth the D5 process split shares.
    An unreadable mode (no pool, or a failed read) fails closed with 503
    ``live_gate_unreadable``: a webhook-originated order never flows past an unreadable
    gate (R25). The signals route calls this before delegating (R3) and the backend op
    calls it again with ``webhook_origin=True`` — one truth, two layers.
    """
    if pool is None:
        logger.error("Webhook live-gate unreadable for strategy %s: no database pool", strategy_id)
        raise _live_gate_unreadable()
    try:
        rows = await session_store.load_running(pool)
    except DB_ERRORS as exc:
        logger.exception("Webhook live-gate read failed for strategy %s", strategy_id)
        raise _live_gate_unreadable() from exc
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
    live session — and when the mode cannot be read, it refuses with 503
    ``live_gate_unreadable`` instead (R25: the gate fails closed). Unmarked calls keep
    today's behaviour; nothing live can run before P3-A.

    An entry is a buy, an exit a reduce-only sell; an entry without ``quantity`` is sized
    like a strategy entry. Returns ``{"session_id", "order_id", "status"}``; unknown
    failures propagate for the caller's 502.
    """
    if mgr is None:
        raise service_error(
            503, "engine_not_ready", "Trading engine not initialized", "unavailable"
        )
    if webhook_origin:
        await refuse_live_webhook_target(pool, symbol=symbol, strategy_id=strategy_id)
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
    except SubmitUnknownError as exc:
        raise service_error(
            503, exc.code, str(exc), "unavailable", client_order_id=exc.client_order_id
        ) from exc
    except ExchangeRateLimitedError as exc:
        raise service_error(503, exc.code, str(exc), "unavailable") from exc
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
    except SubmitUnknownError as exc:
        raise service_error(
            503, exc.code, str(exc), "unavailable", client_order_id=exc.client_order_id
        ) from exc
    except ExchangeRateLimitedError as exc:
        raise service_error(503, exc.code, str(exc), "unavailable") from exc
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
