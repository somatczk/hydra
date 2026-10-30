"""TradingSession and restart recovery, moved verbatim from ``session_manager`` (S6 pure move)."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hydra.core import metrics
from hydra.core.config import HydraConfig
from hydra.core.types import ExchangeId, MarketType, Timeframe
from hydra.execution import client_factory, session_store
from hydra.execution.client_factory import ClientFactory
from hydra.execution.errors import PaperCapitalUnavailableError
from hydra.execution.flatten import FlattenResult
from hydra.execution.paper_trading import PaperStateUnavailableError, PaperTradingExecutor
from hydra.execution.perp_trading import PaperExecutor, PerpPaperTradingExecutor
from hydra.execution.session_wiring import SessionRuntime, probe_strategy
from hydra.ml.consumers import validate_session_ml
from hydra.risk.market_rules import MarketRulesCache
from hydra.risk.service import RiskService
from hydra.strategy.config import StrategyConfig, config_hash

if TYPE_CHECKING:
    from hydra.core.event_bus import InMemoryEventBus
    from hydra.data.ingestion import ExchangeFeedManager
    from hydra.execution.order_manager import OrderManager
    from hydra.strategy.engine import StrategyEngine

logger = logging.getLogger(__name__)

PRE_P1_STATE = "paper state unavailable (pre-P1 session)"


@dataclass
class TradingSession:
    """Lifecycle of one strategy's paper trading run."""

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
    last_bar_at: datetime | None = None
    last_fill_at: datetime | None = None
    flatten_incomplete: bool = False
    persist_error: str | None = None
    strategy_config: dict[str, Any] | None = None
    config_hash: str | None = None
    created_by: str | None = None  # the principal string that started the session

    # Runtime components (not persisted)
    _runtime: SessionRuntime | None = field(default=None, repr=False)
    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    _engine: StrategyEngine | None = field(default=None, repr=False)
    _event_bus: InMemoryEventBus | None = field(default=None, repr=False)
    _order_manager: OrderManager | None = field(default=None, repr=False)
    _executor: Any = field(default=None, repr=False)
    _feed: ExchangeFeedManager | None = field(default=None, repr=False)
    _all_timeframes: list[Timeframe] = field(default_factory=list, repr=False)

    @property
    def task_alive(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def holdings(self) -> int:
        """Symbols with a position held by this session's paper account (stops included)."""
        ex = self._executor
        if not isinstance(ex, (PaperTradingExecutor, PerpPaperTradingExecutor)):
            return 0
        return sum(1 for q in ex.get_account_snapshot().base_totals.values() if q > 0)


def _snapshot(raw: Any) -> dict[str, Any] | None:
    """The stored config snapshot (asyncpg returns jsonb as text); NULL for pre-snapshot rows."""
    return json.loads(raw) if isinstance(raw, str) else raw


class SessionRecoveryMixin:
    """Restart recovery, moved verbatim from ``session_manager`` (S6 pure move)."""

    _db_pool: Any
    _sessions: dict[str, TradingSession]
    _config: HydraConfig
    _market_rules: MarketRulesCache
    _clients: ClientFactory

    # Provided by the host; mypy's override checks keep these signatures in sync.
    def _require_risk(self) -> RiskService:
        raise NotImplementedError

    def _risk_for(self, session: TradingSession) -> RiskService:
        raise NotImplementedError

    async def _recover_live_one(self, session: TradingSession) -> None:
        raise NotImplementedError

    def _find_strategy_config(self, strategy_id: str) -> StrategyConfig:
        raise NotImplementedError

    def _runtime(
        self,
        session: TradingSession,
        cfg: StrategyConfig,
        executor: PaperExecutor,
        required_history: int,
    ) -> SessionRuntime:
        raise NotImplementedError

    async def _launch(self, runtime: SessionRuntime, capital: Decimal, *, recovering: bool) -> None:
        raise NotImplementedError

    async def _close_executor(self, session: TradingSession, reason: str) -> FlattenResult:
        raise NotImplementedError

    # -- Internal: recovery ---------------------------------------------------

    async def _recover(self) -> None:
        if self._db_pool is None:
            return
        rows = await session_store.load_running(self._db_pool)
        for row in rows:
            sid = str(row["id"])
            existing = self._sessions.get(sid)
            if existing is not None and existing.task_alive:
                continue
            session = TradingSession(
                session_id=sid,
                strategy_id=row["strategy_id"],
                trading_mode=row["trading_mode"],
                status="running",
                exchange_id=row["exchange_id"],
                symbols=list(row["symbols"]),
                timeframe=row["timeframe"],
                paper_capital=row["paper_capital"],
                started_at=row["started_at"],
                strategy_config=_snapshot(row.get("strategy_config")),
                created_by=row.get("created_by"),
            )
            self._sessions[sid] = session
            try:
                await self._recover_one(session)
            except Exception as exc:
                logger.exception("Failed to recover session %s", sid)
                self._mark_error(session, f"recovery failed: {exc}")
            await session_store.persist_session(self._db_pool, session)

    async def _recover_one(self, session: TradingSession) -> None:
        """Restore the paper account first; once restored it is either resumed or closed.

        The venue is validated against the row's own market type (the A1 fix: a
        ``binanceusdm`` row recovers through the perp pair, never the default-SPOT
        validation that errored it), and a FUTURES row restores through the perp
        executor — the same one its session started on.
        """
        if session.trading_mode != "paper":
            await self._recover_live_one(session)
            return
        risk = self._require_risk()
        # the row's own mode: a bybit LIVE row recovers live; a bybit paper row is errored
        exchange_id: ExchangeId = client_factory.validate_venue(
            session.exchange_id,
            client_factory.venue_market_type(session.exchange_id),
            mode="paper",
        )
        rules_error: Exception | None = None
        rules: Any = None
        try:
            rules = await self._market_rules.get(exchange_id, session.symbols)
        except Exception as exc:
            rules_error = exc
        executor: PaperExecutor | None
        try:
            if client_factory.venue_market_type(session.exchange_id) is MarketType.FUTURES:
                executor = await PerpPaperTradingExecutor.restore(
                    self._db_pool,
                    session.session_id,
                    market_rules=rules,
                    funding_reader=self._clients.funding_reader_factory(self._db_pool, exchange_id),
                )
            else:
                executor = await PaperTradingExecutor.restore(
                    self._db_pool, session.session_id, market_rules=rules
                )
        except PaperStateUnavailableError as exc:
            self._mark_error(session, f"paper state could not be restored: {exc}")
            return
        if executor is None:
            self._mark_error(session, PRE_P1_STATE)
            return
        session._executor = executor
        halted = executor.get_account_snapshot().halted
        if halted is not None:
            # a restored halted account (a persisted liquidation) never resumes
            self._mark_error(session, f"paper ledger halted: {halted}")
            return
        if risk.kill_switch_active():
            await self._close_restored(session, "kill_switch", None)
            return
        try:
            if rules_error is not None:
                raise rules_error
            cfg = self._recovery_config(session)
            client_factory.validate_venue(
                session.exchange_id, cfg.exchange.market_type, mode="paper"
            )
            required_history = await probe_strategy(cfg, self._config)
            if session.paper_capital is None or session.paper_capital <= 0:
                raise PaperCapitalUnavailableError("the session row has no allocated paper_capital")
            # P5-C: a model-consuming session re-validates its model before the resume
            await validate_session_ml(
                self._db_pool,
                Path(self._config.ml.models_dir),
                cfg,
            )
            runtime = self._runtime(session, cfg, executor, required_history)
            await self._launch(runtime, session.paper_capital, recovering=True)
        except Exception as exc:
            logger.exception("Restored session %s cannot resume", session.session_id)
            await self._close_restored(session, "recovery_failed", f"recovery failed: {exc}")
            return
        logger.info("Recovered session %s for strategy %s", session.session_id, session.strategy_id)

    def _recovery_config(self, session: TradingSession) -> StrategyConfig:
        """The config the session started with; rows from before the snapshot use the file."""
        if session.strategy_config is not None:
            cfg = StrategyConfig.model_validate(session.strategy_config)
            cfg.enabled = True
        else:
            cfg = self._find_strategy_config(session.strategy_id)
            session.strategy_config = cfg.model_dump(mode="json")
        session.config_hash = config_hash(cfg)
        return cfg

    async def _close_restored(
        self, session: TradingSession, reason: str, failure: str | None
    ) -> None:
        """A restored account that will not resume: sell its holdings at the restored marks."""
        try:
            if session._executor is not None:
                risk = self._risk_for(session)
                await risk.register_session(
                    session.session_id,
                    session._executor,
                    allocated=session.paper_capital or Decimal("0"),
                    symbols=session.symbols,
                    exchange_id=session.exchange_id,
                    strategy_id=session.strategy_id,
                    leverage=getattr(session._executor, "leverage", 1),
                )
        except Exception:
            logger.exception("Session %s not registered for its PnL carry", session.session_id)
        result = await self._close_executor(session, reason)
        outcome = (
            f"{result.positions_remaining} position(s) could not be flattened"
            + (f" ({'; '.join(result.failures)})" if result.failures else "")
            if result.positions_remaining
            else "restored holdings flattened"
        )
        if failure is None and not result.positions_remaining:
            session.status = "stopped"
            session.stopped_at = datetime.now(UTC)
            session.error_message = f"Not resumed after restart: kill switch active ({outcome})"
            return
        self._mark_error(session, f"{failure or 'kill switch active'}; {outcome}")

    def _mark_error(self, session: TradingSession, message: str) -> None:
        session.status = "error"
        session.error_message = message
        session.stopped_at = datetime.now(UTC)
        metrics.record_session_errored(session.strategy_id)
        logger.error("Session %s not resumed: %s", session.session_id, message)

    async def _load_recent(self) -> None:
        if self._db_pool is None:
            return
        for row in await session_store.load_recent(self._db_pool):
            sid = str(row["id"])
            if sid in self._sessions:
                continue
            self._sessions[sid] = TradingSession(
                session_id=sid,
                strategy_id=row["strategy_id"],
                trading_mode=row["trading_mode"],
                status=row["status"],
                exchange_id=row["exchange_id"],
                symbols=list(row["symbols"]),
                timeframe=row["timeframe"],
                paper_capital=row["paper_capital"],
                started_at=row["started_at"],
                stopped_at=row["stopped_at"],
                error_message=row["error_message"],
                positions_at_stop=row.get("positions_at_stop", 0),
                created_by=row.get("created_by"),
            )
