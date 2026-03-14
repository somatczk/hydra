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

from hydra.core.config import load_config
from hydra.core.event_bus import InMemoryEventBus
from hydra.execution.order_manager import OrderManager
from hydra.execution.paper_trading import PaperTradingExecutor
from hydra.strategy.config import StrategyConfig, load_strategy_config
from hydra.strategy.context import StrategyContext
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

    # Runtime components (not persisted)
    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    _engine: StrategyEngine | None = field(default=None, repr=False)
    _event_bus: InMemoryEventBus | None = field(default=None, repr=False)
    _order_manager: OrderManager | None = field(default=None, repr=False)
    _executor: Any = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


class SessionManager:
    """Registry of all active trading sessions."""

    def __init__(self, db_pool: Any = None) -> None:
        self._sessions: dict[str, TradingSession] = {}
        self._db_pool = db_pool

    # -- Public API ----------------------------------------------------------

    async def start_session(
        self,
        strategy_id: str,
        trading_mode: str,
        paper_capital: Decimal | None = None,
    ) -> str:
        """Start a new trading session for *strategy_id*.

        Returns the session_id.
        """
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
            initial_bal = {"USDT": paper_capital or Decimal("10000")}
            executor = PaperTradingExecutor(
                exchange_id=cfg.exchange.exchange_id,
                initial_balances=initial_bal,
                db_pool=self._db_pool,
                strategy_id=strategy_id,
            )
        else:
            # Live mode: use ExchangeClient (lazy import to avoid CCXT at startup)
            from hydra.execution.exchange_client import ExchangeClient

            executor = ExchangeClient(
                exchange_id=cfg.exchange.exchange_id,
                config={},
            )

        order_manager = OrderManager(executor=executor, event_bus=event_bus)

        session._engine = engine
        session._event_bus = event_bus
        session._order_manager = order_manager
        session._executor = executor

        # Start engine
        session.status = "running"
        session.started_at = datetime.now(UTC)
        task = asyncio.create_task(self._run_session(session))
        session._task = task

        self._sessions[session_id] = session

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

        raise ValueError(f"Strategy config not found: {strategy_id}")

    async def _run_session(self, session: TradingSession) -> None:
        """Run the strategy engine in its own task."""
        try:
            if session._engine is not None:
                await session._engine.start()
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

    async def _stop_session_internal(self, session: TradingSession) -> None:
        """Stop a session's engine/task and update state."""
        if session._engine is not None:
            await session._engine.stop()

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
