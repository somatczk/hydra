"""Trading session lifecycle management.

``SessionManager`` is the registry of all active trading sessions.  It lives
on ``app.state.session_manager`` and is the single point of control for
starting / stopping strategy execution in paper or live mode.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from hydra.core.config import load_config
from hydra.core.event_bus import InMemoryEventBus
from hydra.core.events import BarEvent, EntrySignal, Event, ExitSignal, OrderFillEvent
from hydra.core.types import (
    Direction,
    ExchangeId,
    MarketType,
    OrderRequest,
    OrderType,
    Position,
    Side,
    Symbol,
    Timeframe,
)
from hydra.data.ingestion import ExchangeFeedManager
from hydra.execution.order_manager import OrderManager
from hydra.execution.paper_trading import PaperTradingExecutor
from hydra.risk.pretrade import PortfolioState, PreTradeRiskManager, RiskConfig
from hydra.strategy.config import StrategyConfig, load_strategy_config
from hydra.strategy.context import StrategyContext
from hydra.strategy.engine import StrategyEngine

logger = logging.getLogger(__name__)


def _collect_timeframes(cfg: StrategyConfig) -> list[Timeframe]:
    """Return all timeframes a strategy uses (primary + confirmation + entry)."""
    tfs = [cfg.timeframes.primary]
    if cfg.timeframes.confirmation is not None:
        tfs.append(cfg.timeframes.confirmation)
    if cfg.timeframes.entry is not None:
        tfs.append(cfg.timeframes.entry)
    return tfs


# ---------------------------------------------------------------------------
# Default config / strategy directories
# ---------------------------------------------------------------------------

_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
_STRATEGY_DIR = _CONFIG_DIR / "strategies"


# ---------------------------------------------------------------------------
# TradingSession
# ---------------------------------------------------------------------------


@dataclass
class TradingSession:
    """Lifecycle of one strategy's live/paper trading run."""

    session_id: str
    strategy_id: str
    trading_mode: str  # 'paper' | 'live'
    status: str = "stopped"  # 'running' | 'stopped' | 'error'
    exchange_id: str = "binance"
    symbols: list[str] = field(default_factory=lambda: ["BTCUSDT"])
    timeframe: str = "1h"
    paper_capital: Decimal | None = None
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    error_message: str | None = None

    # Runtime components (not persisted)
    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    _engine: StrategyEngine | None = field(default=None, repr=False)
    _event_bus: InMemoryEventBus | None = field(default=None, repr=False)
    _order_manager: OrderManager | None = field(default=None, repr=False)
    _executor: Any = field(default=None, repr=False)
    _feed: ExchangeFeedManager | None = field(default=None, repr=False)
    _all_timeframes: list[Timeframe] = field(default_factory=list, repr=False)
    _reconcile_task: asyncio.Task[None] | None = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


class SessionManager:
    """Registry of all active trading sessions."""

    def __init__(self, db_pool: Any = None) -> None:
        self._sessions: dict[str, TradingSession] = {}
        self._db_pool = db_pool
        self._risk_manager: PreTradeRiskManager | None = None
        self._risk_config: dict = {}
        self._stale_cleanup_task: asyncio.Task[None] | None = None

        self._init_task: asyncio.Task[None] | None = None
        # Kick off async init if we have a DB pool (safe in running loop only)
        if self._db_pool is not None:
            try:
                loop = asyncio.get_running_loop()
                self._init_task = loop.create_task(self._init_risk_manager())
            except RuntimeError:
                pass  # no running loop — will init on first session start

    # -- Risk manager initialisation ----------------------------------------

    async def _init_risk_manager(self) -> None:
        """Load global risk config from DB and create the PreTradeRiskManager."""
        try:
            async with self._db_pool.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM risk_config WHERE scope = 'global'")
            if row:
                self._risk_config = dict(row)
                config = RiskConfig(
                    max_position_pct=Decimal(str(row.get("max_position_pct", "0.10"))),
                    max_risk_per_trade=Decimal(str(row.get("max_risk_per_trade", "0.02"))),
                    max_portfolio_heat=Decimal(str(row.get("max_portfolio_heat", "0.06"))),
                    max_daily_loss_pct=Decimal(str(row.get("max_daily_loss_pct", "0.03"))),
                    max_consecutive_losses=int(row.get("max_consecutive_losses", 5)),
                )
                self._risk_manager = PreTradeRiskManager(config=config)
            else:
                self._risk_manager = PreTradeRiskManager()
            logger.info("PreTradeRiskManager initialised (from DB: %s)", row is not None)

            # Load global paper capital from system_config table
            try:
                async with self._db_pool.acquire() as conn:
                    pc_row = await conn.fetchrow(
                        "SELECT value FROM system_config WHERE key = 'paper_capital'"
                    )
                self._global_paper_capital = (
                    Decimal(str(pc_row["value"])) if pc_row else Decimal("10000")
                )
            except Exception:
                self._global_paper_capital = Decimal("10000")
        except Exception:
            logger.exception("Failed to load risk config from DB; using defaults")
            self._risk_manager = PreTradeRiskManager()
            self._global_paper_capital = Decimal("10000")

    async def reload_risk_config(self) -> None:
        """Re-read risk config from DB and update the live risk manager."""
        if self._db_pool is None:
            return
        await self._init_risk_manager()

    def _total_allocated_paper_capital(self) -> Decimal:
        """Sum paper_capital of all running paper sessions."""
        total = Decimal("0")
        for s in self._sessions.values():
            if s.status == "running" and s.trading_mode == "paper" and s.paper_capital:
                total += s.paper_capital
        return total

    # -- Portfolio state builder --------------------------------------------

    async def _build_portfolio_state(self, session: TradingSession) -> PortfolioState:
        """Aggregate current portfolio state from executor + DB risk_state."""
        positions: list[Position] = []
        balances: dict[str, Decimal] = {}
        daily_pnl = Decimal("0")
        consecutive_losses = 0
        current_drawdown = Decimal("0")
        portfolio_value = Decimal("10000")

        if session._executor is not None:
            balances = await session._executor.get_balance()
            for sym in session.symbols:
                pos_list = await session._executor.get_positions(sym)
                positions.extend(pos_list)

        # Sum portfolio value from balances + position notional values
        balance_total = sum(balances.values(), Decimal("0"))
        position_value = Decimal("0")
        for pos in positions:
            try:
                price = await session._executor.get_last_price(str(pos.symbol))
                position_value += pos.quantity * price
            except Exception:  # nosec B110
                pass  # price unavailable — skip position valuation
        portfolio_value = balance_total + position_value

        # Load risk state from DB if available
        if self._db_pool is not None:
            try:
                async with self._db_pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT daily_pnl, consecutive_losses, current_drawdown "
                        "FROM risk_state WHERE scope = 'global'"
                    )
                if row:
                    daily_pnl = Decimal(str(row["daily_pnl"])) if row["daily_pnl"] else Decimal("0")
                    consecutive_losses = (
                        int(row["consecutive_losses"]) if row["consecutive_losses"] else 0
                    )
                    current_drawdown = (
                        Decimal(str(row["current_drawdown"]))
                        if row["current_drawdown"]
                        else Decimal("0")
                    )
            except Exception:
                logger.debug("Could not load risk_state for session %s", session.session_id)

        try:
            from hydra.dashboard.metrics import update_portfolio

            update_portfolio(float(portfolio_value), float(current_drawdown), float(daily_pnl))
        except Exception:
            pass

        return PortfolioState(
            positions=positions,
            balances=balances,
            daily_pnl=daily_pnl,
            consecutive_losses=consecutive_losses,
            current_drawdown=current_drawdown,
            portfolio_value=portfolio_value,
        )

    # -- Public API ----------------------------------------------------------

    async def start_session(
        self,
        strategy_id: str,
        trading_mode: str,
        paper_capital: Decimal | None = None,
        credentials: dict | None = None,
    ) -> str:
        """Start a new trading session for *strategy_id*.

        Returns the session_id.
        """
        # Ensure risk manager is initialised before proceeding
        if self._init_task is not None and not self._init_task.done():
            await self._init_task

        # Check kill switch
        if self._db_pool is not None:
            async with self._db_pool.acquire() as conn:
                active = await conn.fetchval(
                    "SELECT kill_switch_active FROM risk_config WHERE scope = 'global'"
                )
                if active:
                    raise RuntimeError("Kill switch is active — cannot start new sessions")

        # Prevent duplicate sessions for the same strategy
        for s in self._sessions.values():
            if s.strategy_id == strategy_id and s.status == "running":
                raise ValueError(f"Strategy {strategy_id} already has a running session")

        # Load strategy config from YAML
        cfg = self._find_strategy_config(strategy_id)

        session_id = str(uuid.uuid4())
        session = TradingSession(
            session_id=session_id,
            strategy_id=strategy_id,
            trading_mode=trading_mode,
            exchange_id=cfg.exchange.exchange_id,
            symbols=cfg.symbols,
            timeframe=str(cfg.timeframes.primary),
            paper_capital=paper_capital,
        )

        # Wire components
        event_bus = InMemoryEventBus()
        context = StrategyContext()

        hydra_config = load_config()
        engine = StrategyEngine(config=hydra_config, event_bus=event_bus, context=context)
        await engine.load_strategy_from_config(cfg)

        executor: Any
        if trading_mode == "paper":
            requested = paper_capital or Decimal("10000")
            global_cap = getattr(self, "_global_paper_capital", Decimal("10000"))
            allocated = self._total_allocated_paper_capital()
            effective = min(requested, global_cap - allocated)
            if effective <= 0:
                raise ValueError(
                    "Insufficient paper capital: "
                    f"global={global_cap}, allocated={allocated}, requested={requested}"
                )
            paper_capital = effective
            initial_bal = {"USDT": effective}  # per-strategy allocation
            executor = PaperTradingExecutor(
                exchange_id=cfg.exchange.exchange_id,
                initial_balances=initial_bal,
                db_pool=self._db_pool,
                strategy_id=strategy_id,
                session_id=session_id,
            )
        else:
            # Live mode: use ExchangeClient (lazy import to avoid CCXT at startup)
            from hydra.execution.exchange_client import ExchangeClient

            executor = ExchangeClient(
                exchange_id=cfg.exchange.exchange_id,
                config=credentials or {},
            )

        # Build portfolio state for risk checks
        session._executor = executor
        session._event_bus = event_bus
        session.symbols = cfg.symbols
        session.session_id = session_id

        portfolio_state = await self._build_portfolio_state(session)

        async def _state_builder() -> PortfolioState:
            return await self._build_portfolio_state(session)

        order_manager = OrderManager(
            executor=executor,
            event_bus=event_bus,
            risk_checker=self._risk_manager,
            portfolio_state=portfolio_state,
            portfolio_state_builder=_state_builder,
        )
        feed = ExchangeFeedManager(event_bus=event_bus)

        session._engine = engine
        session._order_manager = order_manager
        session._feed = feed
        session._all_timeframes = _collect_timeframes(cfg)

        # Start engine
        session.status = "running"
        session.started_at = datetime.now(UTC)
        task = asyncio.create_task(self._run_session(session))
        session._task = task

        self._sessions[session_id] = session

        # Start stale order cleanup if not already running
        if self._stale_cleanup_task is None or self._stale_cleanup_task.done():
            self._stale_cleanup_task = asyncio.create_task(self._stale_order_cleanup_loop())

        # Persist to DB
        await self._persist_session(session)

        logger.info(
            "Started %s session %s for strategy %s",
            trading_mode,
            session_id,
            strategy_id,
        )
        return session_id

    async def stop_session(self, session_id: str) -> None:
        """Stop a running session."""
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Session {session_id} not found")

        await self._stop_session_internal(session)
        logger.info("Stopped session %s", session_id)

    async def stop_all(self) -> None:
        """Kill switch: stop ALL running sessions."""
        running = [s for s in self._sessions.values() if s.status == "running"]
        for session in running:
            await self._stop_session_internal(session)

        # Set kill switch flag in DB
        if self._db_pool is not None:
            async with self._db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE risk_config SET kill_switch_active = TRUE, "
                    "updated_at = now() WHERE scope = 'global'"
                )

        logger.warning("Kill switch activated — stopped %d sessions", len(running))

    async def release_kill_switch(self) -> None:
        """Release the kill switch so new sessions can start."""
        if self._db_pool is not None:
            async with self._db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE risk_config SET kill_switch_active = FALSE, "
                    "updated_at = now() WHERE scope = 'global'"
                )
        logger.info("Kill switch released")

    def list_sessions(self) -> list[TradingSession]:
        """Return all sessions (running + recently stopped)."""
        return list(self._sessions.values())

    def get_session(self, session_id: str) -> TradingSession | None:
        """Return a session by ID."""
        return self._sessions.get(session_id)

    async def get_session_detail(self, session_id: str) -> dict[str, Any]:
        """Return session metadata + live executor state (or DB trades if stopped)."""
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Session {session_id} not found")

        detail: dict[str, Any] = {
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
        }

        # Gather live data from executor or DB trades
        balance: dict[str, float] = {}
        positions: list[dict[str, Any]] = []
        trades: list[dict[str, Any]] = []

        if session.status == "running" and session._executor is not None:
            # Live executor state
            raw_balance = await session._executor.fetch_balance()
            balance = {k: float(v) for k, v in raw_balance.items()}
            raw_positions = await session._executor.fetch_positions()
            positions = [
                {
                    "symbol": p["symbol"],
                    "direction": p["side"],
                    "quantity": p["contracts"],
                    "avg_entry_price": p["entryPrice"],
                    "unrealized_pnl": p["unrealizedPnl"],
                }
                for p in raw_positions
            ]
            trades = await session._executor.get_filled_orders()
        else:
            # Stopped/error: load trades from DB
            trades = await self._load_session_trades(session)

        # Compute metrics from trades
        total_pnl = sum(t.get("pnl", 0) or 0 for t in trades)
        winning = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
        total_trades = len(trades)
        win_rate = (winning / total_trades * 100) if total_trades > 0 else 0.0

        detail["metrics"] = {
            "balance": balance,
            "total_pnl": total_pnl,
            "win_rate": round(win_rate, 1),
            "total_trades": total_trades,
            "open_positions": len(positions),
        }
        detail["positions"] = positions
        detail["trades"] = trades

        return detail

    async def _load_session_trades(self, session: TradingSession) -> list[dict[str, Any]]:
        """Load trades from DB for a stopped session."""
        if self._db_pool is None:
            return []
        try:
            source = session.trading_mode  # 'paper' or 'live'
            async with self._db_pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, symbol, side, quantity, price, fee, pnl, timestamp
                    FROM trades
                    WHERE session_id = $1 AND source = $2
                    ORDER BY timestamp ASC
                    """,
                    session.session_id,
                    source,
                )
                return [
                    {
                        "id": str(row["id"]),
                        "symbol": row["symbol"],
                        "side": row["side"],
                        "quantity": float(row["quantity"]) if row["quantity"] else 0,
                        "price": float(row["price"]) if row["price"] else 0,
                        "fee": float(row["fee"]) if row["fee"] else 0,
                        "pnl": float(row["pnl"]) if row["pnl"] else 0,
                        "timestamp": row["timestamp"].isoformat() if row["timestamp"] else None,
                    }
                    for row in rows
                ]
        except Exception:
            logger.exception("Failed to load trades for session %s", session.session_id)
            return []

    # -- Internal helpers ----------------------------------------------------

    def _find_strategy_config(self, strategy_id: str) -> StrategyConfig:
        """Locate and load a strategy config by ID from the strategies dir."""
        if not _STRATEGY_DIR.exists():
            raise FileNotFoundError(f"Strategy directory not found: {_STRATEGY_DIR}")

        for path in sorted(_STRATEGY_DIR.iterdir()):
            if path.suffix not in (".yaml", ".yml"):
                continue
            cfg = load_strategy_config(path)
            if cfg.id == strategy_id:
                # Force-enable it for session use
                cfg.enabled = True
                return cfg

        raise KeyError(f"Strategy config not found: {strategy_id}")

    async def _execute_order_actions(
        self, actions: list, executor: Any, session: TradingSession
    ) -> None:
        """Dispatch OrderAction items (PlaceOrder/CancelOrder/ModifyOrder) to the executor."""
        from hydra.strategy.base import CancelOrder, ModifyOrder, PlaceOrder

        for action in actions:
            try:
                if isinstance(action, PlaceOrder):
                    await executor.create_order(
                        symbol=action.symbol,
                        side=action.side,
                        order_type=action.order_type,
                        quantity=action.quantity,
                        price=action.price,
                        stop_price=action.stop_price,
                        params=action.params or {},
                    )
                elif isinstance(action, CancelOrder):
                    await executor.cancel_order(action.order_id, action.symbol)
                elif isinstance(action, ModifyOrder):
                    await executor.cancel_order(action.order_id, action.symbol)
                    if action.new_price is not None or action.new_quantity is not None:
                        await executor.create_order(
                            symbol=action.symbol,
                            side="BUY",
                            order_type="LIMIT",
                            quantity=action.new_quantity or Decimal("0"),
                            price=action.new_price,
                        )
            except Exception:
                logger.exception(
                    "Failed to execute order action %s in session %s",
                    type(action).__name__,
                    session.session_id,
                )

    async def _run_session(self, session: TradingSession) -> None:
        """Run the strategy engine in its own task."""
        try:
            if session._engine is not None:
                await session._engine.start()

                # Wire signal->order bridge
                if session._event_bus and session._order_manager and session._executor:
                    om = session._order_manager  # capture for closure

                    async def on_bar_price(event: Event) -> None:
                        if (
                            isinstance(event, BarEvent)
                            and event.ohlcv
                            and hasattr(session._executor, "set_market_price")
                        ):
                            session._executor.set_market_price(
                                str(event.symbol),
                                event.ohlcv.close,
                            )

                    async def on_signal(event: Event) -> None:
                        order = await self._signal_to_order(event, session)
                        if order is not None:
                            try:
                                await om.submit_order(order)
                            except ValueError as exc:
                                logger.warning("Skipped order from signal: %s", exc)
                            except Exception:
                                logger.exception("Failed to submit order from signal")

                    await session._event_bus.subscribe("bar", on_bar_price)
                    await session._event_bus.subscribe("entry_signal", on_signal)
                    await session._event_bus.subscribe("exit_signal", on_signal)

                    # Forward fill events to risk manager
                    async def on_fill(event: Event) -> None:
                        if self._risk_manager and session.status == "running":
                            pnl = Decimal(str(getattr(event, "pnl", 0) or 0))
                            balance = await session._executor.get_balance()
                            portfolio_val = sum(balance.values(), Decimal("0"))
                            await self._risk_manager.record_fill(pnl, portfolio_val)

                    await session._event_bus.subscribe("order_fill", on_fill)

                    # Publish fills to WebSocket ConnectionManager
                    async def on_fill_ws(event: Event) -> None:
                        try:
                            from hydra.core.websocket import manager

                            fill_data = {
                                "event": "order_fill",
                                "session_id": session.session_id,
                                "order_id": getattr(event, "order_id", ""),
                                "symbol": str(getattr(event, "symbol", "")),
                                "side": str(getattr(event, "side", "")),
                                "quantity": str(getattr(event, "quantity", "0")),
                                "price": str(getattr(event, "price", "0")),
                                "fee": str(getattr(event, "fee", "0")),
                                "timestamp": datetime.now(UTC).isoformat(),
                            }
                            await manager.broadcast(f"trades:{session.session_id}", fill_data)
                        except Exception:
                            logger.debug("Could not broadcast fill to WebSocket")

                    await session._event_bus.subscribe("order_fill", on_fill_ws)

                # Wire OrderManagementStrategy if applicable
                if (
                    session._engine is not None
                    and session._engine.is_order_management
                    and session._executor is not None
                    and session._event_bus is not None
                ):
                    from hydra.strategy.base import OrderManagementStrategy

                    for strategy in session._engine.get_all_strategies().values():
                        if isinstance(strategy, OrderManagementStrategy):
                            oms = strategy
                            ex = session._executor

                            # Execute on_start actions
                            start_actions = await oms.on_start()
                            if start_actions:
                                await self._execute_order_actions(start_actions, ex, session)

                            # On bar events: call strategy's on_bar -> execute actions
                            async def on_bar_oms(
                                event: Event,
                                _oms: Any = oms,
                                _ex: Any = ex,
                            ) -> None:
                                if isinstance(event, BarEvent):
                                    actions = await _oms.on_bar(event)
                                    if actions:
                                        await self._execute_order_actions(actions, _ex, session)

                            await session._event_bus.subscribe("bar", on_bar_oms)

                            # On fill events: call strategy's on_fill -> execute follow-ups
                            async def on_fill_oms(
                                event: Event,
                                _oms: Any = oms,
                                _ex: Any = ex,
                            ) -> None:
                                if isinstance(event, OrderFillEvent):
                                    actions = await _oms.on_fill(event)
                                    if actions:
                                        await self._execute_order_actions(actions, _ex, session)

                            await session._event_bus.subscribe("order_fill", on_fill_oms)

                # Start market data feed
                if session._feed is not None:
                    eid = cast(ExchangeId, session.exchange_id)

                    def _ccxt_factory() -> Any:
                        import ccxt.pro

                        cls = getattr(ccxt.pro, session.exchange_id)
                        return cls({"enableRateLimit": True})

                    session._feed._exchange_factories[eid] = _ccxt_factory
                    await session._feed.connect(
                        exchange_id=eid,
                        symbols=session.symbols,
                        timeframes=session._all_timeframes or [Timeframe(session.timeframe)],
                    )

                # Start reconciliation loop for live sessions
                if session.trading_mode == "live":
                    session._reconcile_task = asyncio.create_task(
                        self._reconciliation_loop(session)
                    )

                # Keep the task alive until cancelled
                while session.status == "running":
                    await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.exception("Session %s errored", session.session_id)
            session.status = "error"
            session.error_message = str(exc)
            session.stopped_at = datetime.now(UTC)
            await self._persist_session(session)

    async def _signal_to_order(self, event: Event, session: TradingSession) -> OrderRequest | None:
        """Convert a signal event into an OrderRequest for the live context."""
        if isinstance(event, EntrySignal):
            balance = await session._executor.get_balance()
            capital = balance.get("USDT", Decimal("0"))
            if capital <= 0:
                return None

            # Load max_risk_per_trade from risk config
            max_risk_per_trade = Decimal(str(self._risk_config.get("max_risk_per_trade", "0.02")))

            price = await session._executor.get_last_price(str(event.symbol))
            if price <= 0:
                return None

            # Compute portfolio value from balance for sizing
            portfolio_value = capital
            quantity = (portfolio_value * max_risk_per_trade) / price
            quantity = quantity.quantize(Decimal("0.00000001"))
            if quantity <= 0:
                return None

            side = Side.BUY if event.direction == Direction.LONG else Side.SELL
            return OrderRequest(
                symbol=Symbol(str(event.symbol)),
                side=side,
                order_type=OrderType.MARKET,
                quantity=quantity,
                strategy_id=event.strategy_id,
                exchange_id=event.exchange_id,
                market_type=event.market_type,
            )

        if isinstance(event, ExitSignal):
            positions = await session._executor.get_positions(str(event.symbol))
            if not positions:
                return None
            pos = positions[0]
            side = Side.SELL if pos.direction == Direction.LONG else Side.BUY
            close_pct = getattr(event, "close_pct", 1.0)
            quantity = pos.quantity * Decimal(str(min(max(close_pct, 0.0), 1.0)))
            return OrderRequest(
                symbol=Symbol(str(event.symbol)),
                side=side,
                order_type=OrderType.MARKET,
                quantity=quantity,
                strategy_id=event.strategy_id,
                exchange_id=event.exchange_id,
                market_type=MarketType.SPOT,
            )

        return None

    async def _stop_session_internal(self, session: TradingSession) -> None:
        """Stop a session's engine/task, flatten positions, cancel orders, update state."""
        # 1. Cancel all open orders
        if session._order_manager is not None:
            try:
                open_ids = [
                    oid
                    for oid, t in session._order_manager._orders.items()
                    if t.status.name in ("PENDING", "SUBMITTED", "PARTIALLY_FILLED")
                ]
                for oid in open_ids:
                    await session._order_manager.cancel_order(oid)
                if open_ids:
                    logger.info(
                        "Cancelled %d open orders in session %s",
                        len(open_ids),
                        session.session_id,
                    )
            except Exception:
                logger.exception("Failed to cancel open orders in session %s", session.session_id)

        # 2. Flatten all open positions (market close)
        if session._executor is not None:
            try:
                positions = await session._executor.fetch_positions()
                for pos in positions:
                    symbol = pos["symbol"]
                    side = pos.get("side", "")
                    qty = abs(float(pos.get("contracts", 0)))
                    if qty <= 0:
                        continue
                    # Close: sell longs, buy shorts
                    close_side = "sell" if side == "long" else "buy"
                    await session._executor.create_order(
                        symbol=symbol,
                        order_type="market",
                        side=close_side,
                        amount=qty,
                    )
                    logger.info(
                        "Closed %s position %.8f %s in session %s",
                        side,
                        qty,
                        symbol,
                        session.session_id,
                    )
            except Exception:
                logger.exception("Failed to flatten positions in session %s", session.session_id)

        if session._feed is not None:
            await session._feed.disconnect_all()

        if session._engine is not None:
            await session._engine.stop()

        if session._reconcile_task is not None and not session._reconcile_task.done():
            session._reconcile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session._reconcile_task

        if session._task is not None and not session._task.done():
            session._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session._task

        session.status = "stopped"
        session.stopped_at = datetime.now(UTC)
        await self._persist_session(session)

    async def _persist_session(self, session: TradingSession) -> None:
        """Upsert session row to DB."""
        if self._db_pool is None:
            return
        try:
            async with self._db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO trading_sessions
                        (id, strategy_id, trading_mode, status, exchange_id,
                         symbols, timeframe, paper_capital, started_at,
                         stopped_at, error_message)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    ON CONFLICT (id) DO UPDATE SET
                        status = EXCLUDED.status,
                        started_at = EXCLUDED.started_at,
                        stopped_at = EXCLUDED.stopped_at,
                        error_message = EXCLUDED.error_message
                    """,
                    session.session_id,
                    session.strategy_id,
                    session.trading_mode,
                    session.status,
                    session.exchange_id,
                    session.symbols,
                    session.timeframe,
                    session.paper_capital,
                    session.started_at,
                    session.stopped_at,
                    session.error_message,
                )
        except Exception:
            logger.exception("Failed to persist session %s", session.session_id)

    # -- Background tasks ----------------------------------------------------

    async def _stale_order_cleanup_loop(self) -> None:
        """Periodically clean up stale orders across all active sessions."""
        while True:
            try:
                await asyncio.sleep(60)
                for session in self._sessions.values():
                    if session.status == "running" and session._order_manager is not None:
                        try:
                            cancelled = await session._order_manager.cleanup_stale_orders()
                            if cancelled:
                                logger.info(
                                    "Cleaned up %d stale orders in session %s",
                                    len(cancelled),
                                    session.session_id,
                                )
                        except Exception:
                            logger.debug(
                                "Stale order cleanup failed for session %s",
                                session.session_id,
                            )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in stale order cleanup loop")

    async def _reconciliation_loop(self, session: TradingSession) -> None:
        """Periodically log position state for live sessions (every 5 minutes)."""
        while session.status == "running":
            try:
                await asyncio.sleep(300)
                if session.status != "running" or session._executor is None:
                    break

                balances = await session._executor.get_balance()
                all_positions: list[Position] = []
                for sym in session.symbols:
                    pos_list = await session._executor.get_positions(sym)
                    all_positions.extend(pos_list)

                try:
                    from hydra.dashboard.metrics import update_position, update_reconciliation

                    for pos in all_positions:
                        update_position(str(pos.symbol), session.exchange_id, float(pos.quantity))
                    for sym in session.symbols:
                        update_reconciliation(session.exchange_id, sym, mismatch=False)
                except Exception:
                    pass

                logger.info(
                    "Reconciliation [session=%s]: balances=%s, positions=%d, symbols=%s",
                    session.session_id,
                    {k: str(v) for k, v in balances.items()},
                    len(all_positions),
                    [str(p.symbol) for p in all_positions],
                )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Reconciliation error for session %s", session.session_id)

    # -- Lifecycle -----------------------------------------------------------

    async def graceful_shutdown(self) -> None:
        """Stop in-memory engines/tasks but leave DB status as 'running' for recovery."""
        # Cancel stale order cleanup
        if self._stale_cleanup_task is not None and not self._stale_cleanup_task.done():
            self._stale_cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stale_cleanup_task

        for session in self._sessions.values():
            if session.status != "running":
                continue
            if session._feed is not None:
                await session._feed.disconnect_all()
            if session._engine is not None:
                await session._engine.stop()
            if session._task is not None and not session._task.done():
                session._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await session._task
        logger.info("Graceful shutdown complete — sessions left as 'running' in DB for recovery")

    async def recover_running_sessions(self) -> None:
        """Recover sessions that were left as 'running' in DB after a graceful shutdown."""
        if self._db_pool is None:
            return
        try:
            async with self._db_pool.acquire() as conn:
                rows = await conn.fetch("SELECT * FROM trading_sessions WHERE status = 'running'")
            for row in rows:
                sid = row["id"]

                # Duplicate guard: skip if already tracked in-memory
                if sid in self._sessions:
                    continue

                strategy_id = row["strategy_id"]
                trading_mode = row["trading_mode"]
                task: asyncio.Task[None] | None = None
                try:
                    cfg = self._find_strategy_config(strategy_id)

                    session = TradingSession(
                        session_id=sid,
                        strategy_id=strategy_id,
                        trading_mode=trading_mode,
                        status="running",
                        exchange_id=row["exchange_id"],
                        symbols=row["symbols"],
                        timeframe=row["timeframe"],
                        paper_capital=row["paper_capital"],
                        started_at=row["started_at"],
                    )

                    event_bus = InMemoryEventBus()
                    context = StrategyContext()
                    hydra_config = load_config()
                    engine = StrategyEngine(
                        config=hydra_config,
                        event_bus=event_bus,
                        context=context,
                    )
                    await engine.load_strategy_from_config(cfg)

                    executor: Any
                    if trading_mode == "paper":
                        initial_bal = {
                            "USDT": row["paper_capital"] or Decimal("10000"),
                        }
                        executor = PaperTradingExecutor(
                            exchange_id=cfg.exchange.exchange_id,
                            initial_balances=initial_bal,
                            db_pool=self._db_pool,
                            strategy_id=strategy_id,
                            session_id=sid,
                        )
                    else:
                        # Live recovery: load credentials from DB
                        from hydra.execution.exchange_client import ExchangeClient

                        creds: dict[str, Any] = {}
                        try:
                            from hydra.core.encryption import decrypt

                            async with self._db_pool.acquire() as conn:
                                cred_row = await conn.fetchrow(
                                    "SELECT encrypted_key, encrypted_secret, "
                                    "encrypted_passphrase "
                                    "FROM exchange_credentials "
                                    "WHERE exchange_id = $1",
                                    cfg.exchange.exchange_id,
                                )
                            if cred_row:
                                creds = {
                                    "apiKey": decrypt(cred_row["encrypted_key"]),
                                    "secret": decrypt(cred_row["encrypted_secret"]),
                                }
                                if cred_row.get("encrypted_passphrase"):
                                    creds["password"] = decrypt(cred_row["encrypted_passphrase"])
                            else:
                                raise ValueError(
                                    f"No credentials found for exchange {cfg.exchange.exchange_id}"
                                )
                        except ValueError:
                            raise
                        except Exception as exc:
                            raise ValueError(
                                f"Failed to load credentials for exchange "
                                f"{cfg.exchange.exchange_id}"
                            ) from exc

                        executor = ExchangeClient(
                            exchange_id=cfg.exchange.exchange_id,
                            config=creds,
                        )

                    # Build portfolio state for risk checks
                    session._executor = executor
                    session._event_bus = event_bus
                    portfolio_state = await self._build_portfolio_state(session)

                    async def _recovery_state_builder(
                        _s: TradingSession = session,
                    ) -> PortfolioState:
                        return await self._build_portfolio_state(_s)

                    order_manager = OrderManager(
                        executor=executor,
                        event_bus=event_bus,
                        risk_checker=self._risk_manager,
                        portfolio_state=portfolio_state,
                        portfolio_state_builder=_recovery_state_builder,
                    )
                    feed = ExchangeFeedManager(event_bus=event_bus)

                    session._engine = engine
                    session._order_manager = order_manager
                    session._feed = feed
                    session._all_timeframes = _collect_timeframes(cfg)
                    task = asyncio.create_task(self._run_session(session))
                    session._task = task

                    self._sessions[sid] = session
                    logger.info("Recovered session %s for strategy %s", sid, strategy_id)
                except Exception:
                    logger.exception("Failed to recover session %s", sid)
                    # Cancel the task if it was created
                    if task is not None and not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                    # Mark as error so we don't retry endlessly
                    if sid in self._sessions:
                        self._sessions[sid].status = "error"
                        self._sessions[sid].error_message = "Recovery failed"
                    async with self._db_pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE trading_sessions SET status = 'error', "
                            "error_message = 'Recovery failed' WHERE id = $1",
                            sid,
                        )
        except Exception:
            logger.exception("Failed to query running sessions for recovery")

    async def load_recent_sessions(self) -> None:
        """Load recent sessions from DB into memory (for API display)."""
        if self._db_pool is None:
            return
        try:
            async with self._db_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM trading_sessions ORDER BY created_at DESC LIMIT 50"
                )
                for row in rows:
                    sid = row["id"]
                    if sid not in self._sessions:
                        self._sessions[sid] = TradingSession(
                            session_id=sid,
                            strategy_id=row["strategy_id"],
                            trading_mode=row["trading_mode"],
                            status=row["status"],
                            exchange_id=row["exchange_id"],
                            symbols=row["symbols"],
                            timeframe=row["timeframe"],
                            paper_capital=row["paper_capital"],
                            started_at=row["started_at"],
                            stopped_at=row["stopped_at"],
                            error_message=row["error_message"],
                        )
        except Exception:
            logger.exception("Failed to load recent sessions from DB")
