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
from typing import Any

from hydra.core.config import HydraConfig, load_config
from hydra.core.event_bus import InMemoryEventBus
from hydra.core.types import Position, Timeframe
from hydra.data.ingestion import ExchangeFeedManager
from hydra.execution import client_factory, session_store, session_wiring
from hydra.execution.flatten import flatten_session
from hydra.execution.order_manager import OrderManager
from hydra.execution.portfolio_state import build_portfolio_state
from hydra.risk.pretrade import PortfolioState, PreTradeRiskManager, RiskConfig
from hydra.strategy.config import StrategyConfig, load_strategy_config
from hydra.strategy.engine import StrategyEngine

logger = logging.getLogger(__name__)


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
    positions_at_stop: int = 0

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

    def __init__(self, db_pool: Any = None, config: HydraConfig | None = None) -> None:
        self._sessions: dict[str, TradingSession] = {}
        self._db_pool = db_pool
        self._config = config
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

    def _hydra_config(self) -> HydraConfig:
        """Return the injected config, or load it from disk when none was given."""
        return self._config if self._config is not None else load_config()

    # -- Portfolio state builder --------------------------------------------

    async def _build_portfolio_state(self, session: TradingSession) -> PortfolioState:
        """Aggregate current portfolio state from executor + DB risk_state."""
        return await build_portfolio_state(session._executor, session.symbols, self._db_pool)

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
            active = await session_store.read_kill_switch(self._db_pool)
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
        event_bus, engine = await session_wiring.build_engine(cfg, self._hydra_config())

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
            executor = client_factory.build_paper_executor(
                exchange_id=cfg.exchange.exchange_id,
                initial_balances=initial_bal,
                db_pool=self._db_pool,
                strategy_id=strategy_id,
                session_id=session_id,
            )
        else:
            executor = client_factory.build_live_executor(
                exchange_id=cfg.exchange.exchange_id,
                config=credentials or {},
            )

        # Build portfolio state for risk checks
        session.symbols = cfg.symbols
        session.session_id = session_id
        await session_wiring.attach_order_path(
            session,
            cfg,
            engine,
            event_bus,
            executor,
            self._risk_manager,
            self._build_portfolio_state,
        )

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
        await session_store.persist_session(self._db_pool, session)

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
            await session_store.set_kill_switch(self._db_pool, True)

        logger.warning("Kill switch activated — stopped %d sessions", len(running))

    async def release_kill_switch(self) -> None:
        """Release the kill switch so new sessions can start."""
        if self._db_pool is not None:
            await session_store.set_kill_switch(self._db_pool, False)
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
            "positions_at_stop": session.positions_at_stop,
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
            trades = await session_store.load_session_trades(self._db_pool, session)

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

    async def _run_session(self, session: TradingSession) -> None:
        """Run the strategy engine in its own task."""
        try:
            if session._engine is not None:
                await session_wiring.start_session_runtime(
                    session,
                    get_risk_manager=lambda: self._risk_manager,
                    get_risk_config=lambda: self._risk_config,
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
            await session_store.persist_session(self._db_pool, session)

    async def _stop_session_internal(self, session: TradingSession) -> None:
        """Stop a session's engine/task, flatten positions, cancel orders, update state."""
        await flatten_session(session)

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
        await session_store.persist_session(self._db_pool, session)

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
                    from hydra.core.metrics import update_position, update_reconciliation

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
            rows = await session_store.load_running(self._db_pool)
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

                    event_bus, engine = await session_wiring.build_engine(cfg, self._hydra_config())

                    executor: Any
                    if trading_mode == "paper":
                        initial_bal = {
                            "USDT": row["paper_capital"] or Decimal("10000"),
                        }
                        executor = client_factory.build_paper_executor(
                            exchange_id=cfg.exchange.exchange_id,
                            initial_balances=initial_bal,
                            db_pool=self._db_pool,
                            strategy_id=strategy_id,
                            session_id=sid,
                        )
                    else:
                        # Live recovery: load credentials from DB
                        creds = await client_factory.load_live_credentials(
                            self._db_pool, cfg.exchange.exchange_id
                        )
                        executor = client_factory.build_live_executor(
                            exchange_id=cfg.exchange.exchange_id,
                            config=creds,
                        )

                    # Build portfolio state for risk checks
                    await session_wiring.attach_order_path(
                        session,
                        cfg,
                        engine,
                        event_bus,
                        executor,
                        self._risk_manager,
                        self._build_portfolio_state,
                    )
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
                    await session_store.mark_recovery_failed(self._db_pool, sid)
        except Exception:
            logger.exception("Failed to query running sessions for recovery")

    async def load_recent_sessions(self) -> None:
        """Load recent sessions from DB into memory (for API display)."""
        if self._db_pool is None:
            return
        try:
            rows = await session_store.load_recent(self._db_pool)
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
                        positions_at_stop=row.get("positions_at_stop", 0),
                    )
        except Exception:
            logger.exception("Failed to load recent sessions from DB")
