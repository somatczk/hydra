"""Trading session registry and public API.

``SessionManager`` lives on ``app.state.session_manager``. ``startup()`` loads the RiskService,
recovers running paper sessions (live rows are stopped, never resumed), then loads recent rows
for display. Only paper sessions on Binance SPOT run in P1; live start is refused (P3).
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
from typing import TYPE_CHECKING, Any

from hydra.core import metrics
from hydra.core.config import HydraConfig
from hydra.core.types import ExchangeId, OrderType, Side, Timeframe
from hydra.execution import client_factory, session_store
from hydra.execution.client_factory import ClientFactory
from hydra.execution.errors import (
    DuplicateSessionError,
    KillSwitchActiveError,
    LiveTradingDisabledError,
    PaperCapitalUnavailableError,
    SessionNotRunningError,
    SessionPersistError,
    UnsupportedStrategyError,
)
from hydra.execution.flatten import FlattenResult
from hydra.execution.paper_trading import PaperStateUnavailableError, PaperTradingExecutor
from hydra.execution.session_wiring import ManualOrderResult, SessionRuntime, probe_strategy
from hydra.risk.market_rules import MarketRulesCache
from hydra.risk.service import RiskService
from hydra.risk.state import InMemoryRiskStateStore, PgRiskStateStore, RiskStateStore
from hydra.strategy.config import StrategyConfig, load_strategy_config

if TYPE_CHECKING:
    from hydra.core.event_bus import InMemoryEventBus
    from hydra.data.ingestion import ExchangeFeedManager
    from hydra.execution.order_manager import OrderManager
    from hydra.execution.protective_orders import ProtectionSpec
    from hydra.strategy.engine import StrategyEngine

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
_STRATEGY_DIR = _CONFIG_DIR / "strategies"

LIVE_NOT_RESUMED = "Live session not resumed after restart (live trading disabled until P3)"
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
        """Symbols with base held by this session's paper account (resting stops included)."""
        ex = self._executor
        if not isinstance(ex, PaperTradingExecutor):
            return 0
        return sum(1 for q in ex.get_account_snapshot().base_totals.values() if q > 0)


@dataclass(frozen=True, slots=True)
class KillSwitchReport:
    active: bool
    persisted: bool
    sessions_stopped: int
    positions_remaining: int
    flatten_failures: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionHealth:
    session_id: str
    strategy_id: str
    mode: str
    status: str
    desired: bool
    supervised: bool
    last_bar_at: datetime | None
    last_bar_age_s: float | None
    last_fill_at: datetime | None
    open_positions: int
    flatten_incomplete: bool
    entries_paused: str | None
    feed_live: bool
    feed_stale_reasons: dict[str, list[str]]
    persist_backlog: int
    persist_dirty: bool
    error_message: str | None
    protection_failures: tuple[str, ...] = ()
    status_persisted: bool = True


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    sessions: tuple[SessionHealth, ...]
    desired_from_db: bool
    kill_switch: bool
    risk_available: bool


class SessionManager:
    """Registry of trading sessions; the only owner of session runtimes."""

    def __init__(
        self,
        db_pool: Any = None,
        *,
        config: HydraConfig,
        clients: ClientFactory | None = None,
        risk: RiskService | None = None,
        risk_store: RiskStateStore | None = None,
        paper_capital: Decimal | None = None,
    ) -> None:
        self._sessions: dict[str, TradingSession] = {}
        self._db_pool = db_pool
        self._config = config
        self._clients = clients or ClientFactory()
        self._market_rules = MarketRulesCache(loader=self._clients.markets_loader)
        self._risk_store = risk_store
        self._paper_capital = paper_capital
        self.risk: RiskService | None = risk
        self._stale_cleanup_task: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self._lock = asyncio.Lock()
        self._unpersisted: set[str] = set()

    # -- Startup -------------------------------------------------------------

    async def startup(self) -> None:
        """Load risk state, recover running sessions, then load recent rows for display.

        A missing ``system_config.paper_capital`` or unreadable risk state raises: there is no
        default capital to fall back to.
        """
        risk = self._build_risk()
        if risk is not None:
            await risk.load()
            logger.info(
                "Risk service loaded (paper_capital=%s, kill_switch=%s)",
                risk.paper_capital,
                risk.kill_switch_active(),
            )
        else:
            logger.warning("No database and no paper_capital: sessions cannot start")
        await self._recover()
        if risk is not None:
            await risk.recovery_complete()
        await self._load_recent()
        self._ensure_cleanup_loop()

    def _build_risk(self) -> RiskService | None:
        if self.risk is None:
            store = self._risk_store
            if store is None and self._db_pool is not None:
                store = PgRiskStateStore(self._db_pool)
            if store is None and self._paper_capital is not None:
                store = InMemoryRiskStateStore(paper_capital=self._paper_capital)
            if store is None:
                return None
            self.risk = RiskService(store, market_rules=self._market_rules)
        if self.risk.on_must_flatten is None:
            self.risk.on_must_flatten = self._flatten_for_risk
        if self.risk.on_kill_switch is None:
            self.risk.on_kill_switch = self._kill_switch_for_risk
        return self.risk

    def _require_risk(self) -> RiskService:
        if self.risk is None:
            raise PaperCapitalUnavailableError(
                "paper trading needs system_config.paper_capital (no database is configured)"
            )
        return self.risk

    # -- Public API ----------------------------------------------------------

    def kill_switch_active(self) -> bool:
        return self.risk is not None and self.risk.kill_switch_active()

    async def start_session(
        self,
        strategy_id: str,
        trading_mode: str,
        paper_capital: Decimal | None = None,
    ) -> str:
        """Start a paper session; returns its id. Raises the typed errors in ``errors``."""
        risk = self._require_risk()
        if risk.kill_switch_active():
            raise KillSwitchActiveError("Kill switch is active: new sessions cannot start")
        if trading_mode not in ("paper", "live"):
            raise ValueError("trading_mode must be 'paper' or 'live'")
        async with self._lock:
            for s in self._sessions.values():
                if s.strategy_id == strategy_id and s.status == "running":
                    raise DuplicateSessionError(
                        f"Strategy {strategy_id} already has a running session"
                    )
            cfg = self._find_strategy_config(strategy_id)
            exchange_id = client_factory.validate_venue(
                cfg.exchange.exchange_id, cfg.exchange.market_type
            )
            if trading_mode == "live":
                raise LiveTradingDisabledError("live trading is disabled until P3")
            probe = await probe_strategy(cfg, self._config)
            if probe.order_management:
                raise UnsupportedStrategyError(
                    f"{cfg.strategy_class} manages its own orders; DCA/Grid run in P2"
                )
            capital = self._allocate(risk, paper_capital)

            session_id = str(uuid.uuid4())
            session = TradingSession(
                session_id=session_id,
                strategy_id=strategy_id,
                trading_mode="paper",
                exchange_id=exchange_id,
                symbols=list(cfg.symbols),
                timeframe=str(cfg.timeframes.primary),
                paper_capital=capital,
            )
            rules = await self._market_rules.get(exchange_id, cfg.symbols)
            executor = client_factory.build_paper_executor(
                exchange_id=exchange_id,
                initial_balances={"USDT": capital},
                db_pool=self._db_pool,
                strategy_id=strategy_id,
                session_id=session_id,
                market_rules=rules,
            )
            await executor.checkpoint()
            runtime = self._runtime(session, cfg, executor, probe.required_history)
            await self._launch(runtime, capital, recovering=False)
            session.status = "running"
            session.started_at = datetime.now(UTC)
            self._sessions[session_id] = session
            self._ensure_cleanup_loop()
            await session_store.persist_session(self._db_pool, session)
        logger.info("Started paper session %s for strategy %s", session_id, strategy_id)
        return session_id

    async def stop_session(self, session_id: str) -> None:
        """Flatten (reduce-only) and stop a session."""
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Session {session_id} not found")
        await self._stop_session_internal(session)
        logger.info("Stopped session %s", session_id)
        if session.persist_error is not None:
            raise SessionPersistError(session.persist_error)

    async def stop_all(
        self, actor: str = "operator", reason: str = "kill switch"
    ) -> KillSwitchReport:
        """Kill switch: latch first, then flatten and stop every running or errored session."""
        if self.risk is not None:
            latched = await self.risk.activate_kill_switch(actor, reason)
            persisted = latched.persisted
        else:
            persisted = False
        return await self._stop_targets(persisted, reason)

    async def _stop_targets(self, persisted: bool, reason: str) -> KillSwitchReport:
        targets = [
            s
            for s in list(self._sessions.values())
            if s.status == "running"
            or (s.status == "error" and (s._runtime is not None or s.holdings > 0))
        ]
        failures: list[str] = []
        remaining = 0
        for session in targets:
            result = await self._stop_session_internal(session, reason=reason)
            remaining += result.positions_remaining
            if result.positions_remaining:
                failures.append(
                    f"{session.session_id}: {result.positions_remaining} position(s) remain"
                    + (f" ({'; '.join(result.failures)})" if result.failures else "")
                )
            if session.persist_error is not None:
                failures.append(f"{session.session_id}: {session.persist_error}")
        logger.warning(
            "Kill switch: stopped %d session(s), %d position(s) remain",
            len(targets),
            remaining,
        )
        return KillSwitchReport(
            active=True,
            persisted=persisted,
            sessions_stopped=len(targets),
            positions_remaining=remaining,
            flatten_failures=tuple(failures),
        )

    async def release_kill_switch(self, actor: str = "operator", reason: str = "") -> None:
        """Release the kill switch (raises when it cannot be persisted; stays latched)."""
        if self.risk is not None:
            await self.risk.release_kill_switch(actor, reason)
        logger.info("Kill switch released by %s", actor)

    def list_sessions(self) -> list[TradingSession]:
        return list(self._sessions.values())

    def get_session(self, session_id: str) -> TradingSession | None:
        return self._sessions.get(session_id)

    async def submit_manual_order(
        self,
        *,
        symbol: str,
        side: Side,
        order_type: OrderType = OrderType.MARKET,
        quantity: Decimal | None = None,
        price: Decimal | None = None,
        protection: ProtectionSpec | None = None,
        session_id: str | None = None,
        strategy_id: str | None = None,
    ) -> tuple[TradingSession, ManualOrderResult]:
        """Quick-trade and webhook orders: OrderManager -> RiskService -> paper executor.

        The target is *session_id*, or the first running session of *strategy_id* / trading
        *symbol*. Raises SessionNotRunningError, SpotShortError, RiskRejectedError, ValueError.
        """
        session = self._target(symbol, session_id, strategy_id)
        runtime = session._runtime
        if runtime is None:
            raise SessionNotRunningError(f"session {session.session_id} is not running")
        result = await runtime.submit_manual(symbol, side, order_type, quantity, price, protection)
        return session, result

    def _target(
        self, symbol: str, session_id: str | None, strategy_id: str | None
    ) -> TradingSession:
        if session_id is not None:
            session = self._sessions.get(session_id)
            if session is None or session.status != "running":
                raise SessionNotRunningError(f"no running session {session_id}")
            return session
        for s in self._sessions.values():
            if (
                s.status == "running"
                and symbol in s.symbols
                and (strategy_id is None or s.strategy_id == strategy_id)
            ):
                return s
        scope = f"strategy '{strategy_id}'" if strategy_id else f"symbol '{symbol}'"
        raise SessionNotRunningError(f"No running session for {scope}")

    async def get_session_detail(self, session_id: str) -> dict[str, Any]:
        """Session metadata, account numbers, positions and fills."""
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Session {session_id} not found")
        runtime = session._runtime
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
            "flatten_incomplete": session.flatten_incomplete,
            "last_bar_at": session.last_bar_at.isoformat() if session.last_bar_at else None,
            "entries_paused": runtime.entries_paused if runtime is not None else None,
            "protection_failures": list(runtime.protect.failures[-10:]) if runtime else [],
            "persist_error": session.persist_error,
        }
        balance: dict[str, float] = {}
        positions: list[dict[str, Any]] = []
        account: dict[str, float] = {}
        executor = session._executor
        if isinstance(executor, PaperTradingExecutor):
            snap = executor.get_account_snapshot()
            balance = {k: float(v) for k, v in (await executor.fetch_balance()).items()}
            positions = [
                {
                    "symbol": str(p.symbol),
                    "direction": str(p.direction).lower(),
                    "quantity": float(p.quantity),
                    "avg_entry_price": float(p.avg_entry_price),
                    "unrealized_pnl": float(p.unrealized_pnl),
                }
                for p in await executor.get_positions()
            ]
            trades = await executor.get_filled_orders()
            account = {
                "equity": float(snap.equity),
                "realized_pnl": float(snap.realized_net),
                "unrealized_pnl": float(snap.unrealized),
                "fees": float(snap.fees_paid),
                "drawdown": float(snap.drawdown),
            }
        else:
            trades = await session_store.load_session_trades(self._db_pool, session)
        closing = [t for t in trades if t.get("net_pnl") is not None]
        total_pnl = sum(float(t["net_pnl"]) for t in closing)
        winning = sum(1 for t in closing if float(t["net_pnl"]) > 0)
        detail["metrics"] = {
            "balance": balance,
            "total_pnl": total_pnl,
            "win_rate": round(winning / len(closing) * 100, 1) if closing else 0.0,
            "total_trades": len(trades),
            "open_positions": len(positions),
            **account,
        }
        detail["positions"] = positions
        detail["trades"] = trades
        return detail

    async def health_snapshot(self) -> HealthSnapshot:
        """Per session: desired (DB) vs supervised (task alive), feed and bar freshness."""
        desired_ids: set[str] | None = None
        if self._db_pool is not None:
            try:
                desired_ids = await session_store.running_ids(self._db_pool)
            except Exception:
                logger.warning("health snapshot: running rows unreadable", exc_info=True)
        now = datetime.now(UTC)
        out: list[SessionHealth] = []
        for s in self._sessions.values():
            rt = s._runtime
            executor = s._executor
            snap = (
                executor.get_account_snapshot()
                if isinstance(executor, PaperTradingExecutor)
                else None
            )
            eid = s.exchange_id
            out.append(
                SessionHealth(
                    session_id=s.session_id,
                    strategy_id=s.strategy_id,
                    mode=s.trading_mode,
                    status=s.status,
                    desired=(s.session_id in desired_ids)
                    if desired_ids is not None
                    else s.status == "running",
                    supervised=s.task_alive,
                    last_bar_at=s.last_bar_at,
                    last_bar_age_s=(now - s.last_bar_at).total_seconds() if s.last_bar_at else None,
                    last_fill_at=s.last_fill_at,
                    open_positions=s.holdings,
                    flatten_incomplete=s.flatten_incomplete,
                    entries_paused=rt.entries_paused if rt is not None else None,
                    feed_live=rt.feed.is_live(eid) if rt is not None and s.task_alive else False,
                    feed_stale_reasons=rt.feed.stale_reasons(eid) if rt is not None else {},
                    persist_backlog=rt.feed.persist_backlog(eid) if rt is not None else 0,
                    persist_dirty=bool(snap.persist_dirty) if snap is not None else False,
                    error_message=s.error_message,
                    protection_failures=tuple(rt.protect.failures[-10:]) if rt is not None else (),
                    status_persisted=s.persist_error is None,
                )
            )
        for sid in sorted((desired_ids or set()) - set(self._sessions)):
            out.append(
                SessionHealth(
                    session_id=sid,
                    strategy_id="",
                    mode="",
                    status="running",
                    desired=True,
                    supervised=False,
                    last_bar_at=None,
                    last_bar_age_s=None,
                    last_fill_at=None,
                    open_positions=0,
                    flatten_incomplete=False,
                    entries_paused=None,
                    feed_live=False,
                    feed_stale_reasons={},
                    persist_backlog=0,
                    persist_dirty=False,
                    error_message="running in the database but not in this process",
                )
            )
        return HealthSnapshot(
            sessions=tuple(out),
            desired_from_db=desired_ids is not None,
            kill_switch=self.kill_switch_active(),
            risk_available=self.risk is not None and self.risk.available,
        )

    async def graceful_shutdown(self) -> None:
        """Stop runtimes without flattening; DB rows stay 'running' for recovery."""
        if self._stale_cleanup_task is not None and not self._stale_cleanup_task.done():
            self._stale_cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stale_cleanup_task
        for session in list(self._sessions.values()):
            if session._runtime is not None and session.status == "running":
                await session._runtime.shutdown()
                await self._await_task(session)
        for task in list(self._background):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        logger.info("Graceful shutdown complete; running sessions are recovered at startup")

    # -- Internal: build and run ----------------------------------------------

    def _find_strategy_config(self, strategy_id: str) -> StrategyConfig:
        """Locate and load a strategy config by ID from the strategies dir."""
        if not _STRATEGY_DIR.exists():
            raise FileNotFoundError(f"Strategy directory not found: {_STRATEGY_DIR}")
        for path in sorted(_STRATEGY_DIR.iterdir()):
            if path.suffix not in (".yaml", ".yml"):
                continue
            cfg = load_strategy_config(path)
            if cfg.id == strategy_id:
                cfg.enabled = True
                return cfg
        raise KeyError(f"Strategy config not found: {strategy_id}")

    def _allocate(self, risk: RiskService, requested: Decimal | None) -> Decimal:
        account = risk.paper_capital
        if account is None:
            raise PaperCapitalUnavailableError("system_config.paper_capital is not loaded")
        allocated = sum(
            (
                s.paper_capital
                for s in self._sessions.values()
                if (s.status == "running" or s.holdings > 0) and s.paper_capital
            ),
            Decimal("0"),
        )
        available = account - allocated
        wanted = available if requested is None else requested
        if wanted <= 0 or available <= 0:
            raise ValueError(
                "Insufficient paper capital: "
                f"account={account}, allocated={allocated}, requested={requested}"
            )
        return min(wanted, available)

    def _runtime(
        self,
        session: TradingSession,
        cfg: StrategyConfig,
        executor: PaperTradingExecutor,
        required_history: int,
    ) -> SessionRuntime:
        return SessionRuntime(
            session,
            cfg,
            executor=executor,
            risk=self._require_risk(),
            clients=self._clients,
            hydra_config=self._config,
            db_pool=self._db_pool,
            required_history=required_history,
            on_failure=self._on_runtime_failure,
        )

    async def _launch(self, runtime: SessionRuntime, capital: Decimal, *, recovering: bool) -> None:
        """build -> warm-up -> register -> wire -> (adopt) -> prime -> connect -> task."""
        risk = self._require_risk()
        session = runtime.session
        registered = False
        await runtime.build()
        try:
            warm = await runtime.warm_up()
            await risk.register_session(
                session.session_id,
                runtime.executor,
                allocated=capital,
                symbols=session.symbols,
                exchange_id=runtime.exchange_id,
                strategy_id=session.strategy_id,
            )
            registered = True
            await runtime.seed_context()
            await runtime.wire()
            if recovering:
                await runtime.protect.adopt(session.symbols)
            await runtime.prime(warm)
            await runtime.connect()
            if risk.kill_switch_active():
                raise KillSwitchActiveError("Kill switch latched while the session was starting")
        except BaseException:
            await runtime.shutdown()
            if registered:
                await risk.unregister_session(session.session_id)
            session._runtime = None
            raise
        session._task = asyncio.create_task(runtime.run(), name=f"session-{session.session_id}")

    async def _stop_session_internal(
        self, session: TradingSession, reason: str = "stop"
    ) -> FlattenResult:
        runtime = session._runtime
        result = FlattenResult(positions_remaining=session.positions_at_stop)
        if runtime is not None and session.status in ("running", "error"):
            runtime.closing = True
            result = await runtime.flatten()
            await runtime.shutdown()
            await self._await_task(session)
            if self.risk is not None and session.session_id in self.risk.registered_sessions():
                await self.risk.unregister_session(session.session_id)
            if session.status == "running":
                session.status = "stopped"
            session.stopped_at = datetime.now(UTC)
            await self._persist_terminal(session)
            await runtime.broadcast_status()
        elif session.holdings > 0:
            result = await self._close_executor(session, reason)
            if session.status == "running":
                session.status = "stopped"
            session.stopped_at = datetime.now(UTC)
            await self._persist_terminal(session)
        elif session.status == "running":
            session.status = "stopped"
            session.stopped_at = datetime.now(UTC)
            await self._persist_terminal(session)
        logger.info("Session %s stopped (%s)", session.session_id, reason)
        return result

    async def _close_executor(self, session: TradingSession, reason: str) -> FlattenResult:
        """Flatten a paper account that has no runtime (cancel resting orders, sell base)."""
        executor = session._executor
        failures: list[str] = []
        try:
            await executor.flatten(reason=reason)
        except Exception as exc:
            logger.exception("Flatten of %s without a runtime failed", session.session_id)
            failures.append(str(exc))
        remaining = session.holdings
        session.positions_at_stop = remaining
        session.flatten_incomplete = remaining > 0
        risk = self.risk
        if remaining == 0 and risk is not None and session.session_id in risk.registered_sessions():
            await risk.unregister_session(session.session_id)
        return FlattenResult(positions_remaining=remaining, failures=tuple(failures))

    async def _persist_terminal(self, session: TradingSession) -> None:
        """A stopped/errored row must reach the DB, or a restart would resume the session."""
        if await session_store.persist_session(self._db_pool, session, attempts=3):
            session.persist_error = None
            self._unpersisted.discard(session.session_id)
            return
        session.persist_error = (
            f"session status '{session.status}' not persisted; retrying every minute"
        )
        self._unpersisted.add(session.session_id)

    async def _await_task(self, session: TradingSession) -> None:
        task = session._task
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _on_runtime_failure(self, session: TradingSession, reason: str) -> None:
        """A running session cannot continue: error status, flatten, stop the runtime."""
        if session.status != "running":
            return
        logger.error("Session %s failed: %s", session.session_id, reason)
        session.status = "error"
        session.error_message = reason
        session.stopped_at = datetime.now(UTC)
        metrics.record_session_errored(session.strategy_id)
        runtime = session._runtime
        if runtime is not None:
            result = await runtime.flatten()
            await runtime.shutdown()
            await self._await_task(session)
            if (
                result.positions_remaining == 0
                and self.risk is not None
                and session.session_id in self.risk.registered_sessions()
            ):
                await self.risk.unregister_session(session.session_id)
        await self._persist_terminal(session)
        if runtime is not None:
            await runtime.broadcast_status()

    async def retry_unpersisted(self) -> None:
        """Write the stopped/errored rows whose terminal status did not reach the DB yet."""
        for sid in sorted(self._unpersisted):
            session = self._sessions.get(sid)
            if session is not None:
                await self._persist_terminal(session)

    # -- Internal: risk callbacks ---------------------------------------------

    async def _flatten_for_risk(self, reason: str) -> list[str]:
        """Tier-3 breaker: flatten every session (they keep running; entries stay blocked)."""
        failures: list[str] = []
        for session in list(self._sessions.values()):
            runtime = session._runtime
            if runtime is None and session.status == "error" and session.holdings > 0:
                result = await self._close_executor(session, reason)
                await session_store.persist_session(self._db_pool, session)
                if result.positions_remaining:
                    failures.append(
                        f"{session.session_id}: {result.positions_remaining} position(s) remain"
                    )
                continue
            if runtime is None or session.status not in ("running", "error"):
                continue
            was_closing = runtime.closing
            try:
                result = await runtime.flatten()
            finally:
                runtime.closing = was_closing
            if result.positions_remaining:
                failures.append(
                    f"{session.session_id}: {result.positions_remaining} position(s) remain"
                )
            logger.warning("Session %s flattened: %s", session.session_id, reason)
        return failures

    async def _kill_switch_for_risk(self, reason: str) -> None:
        """Tier-4 breaker latched the kill switch: stop sessions in a task of their own."""
        task = asyncio.create_task(self._stop_targets(persisted=True, reason=reason))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

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
            )
            self._sessions[sid] = session
            try:
                await self._recover_one(session)
            except Exception as exc:
                logger.exception("Failed to recover session %s", sid)
                self._mark_error(session, f"recovery failed: {exc}")
            await session_store.persist_session(self._db_pool, session)

    async def _recover_one(self, session: TradingSession) -> None:
        """Restore the paper account first; once restored it is either resumed or closed."""
        if session.trading_mode != "paper":
            session.status = "stopped"
            session.error_message = LIVE_NOT_RESUMED
            session.stopped_at = datetime.now(UTC)
            return
        risk = self._require_risk()
        exchange_id: ExchangeId = client_factory.validate_venue(session.exchange_id)
        rules_error: Exception | None = None
        rules: Any = None
        try:
            rules = await self._market_rules.get(exchange_id, session.symbols)
        except Exception as exc:
            rules_error = exc
        try:
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
        if risk.kill_switch_active():
            await self._close_restored(session, "kill_switch", None)
            return
        try:
            if rules_error is not None:
                raise rules_error
            cfg = self._find_strategy_config(session.strategy_id)
            client_factory.validate_venue(session.exchange_id, cfg.exchange.market_type)
            probe = await probe_strategy(cfg, self._config)
            if probe.order_management:
                raise UnsupportedStrategyError(f"{cfg.strategy_class}: DCA/Grid run in P2")
            if session.paper_capital is None or session.paper_capital <= 0:
                raise PaperCapitalUnavailableError("the session row has no allocated paper_capital")
            runtime = self._runtime(session, cfg, executor, probe.required_history)
            await self._launch(runtime, session.paper_capital, recovering=True)
        except Exception as exc:
            logger.exception("Restored session %s cannot resume", session.session_id)
            await self._close_restored(session, "recovery_failed", f"recovery failed: {exc}")
            return
        logger.info("Recovered session %s for strategy %s", session.session_id, session.strategy_id)

    async def _close_restored(
        self, session: TradingSession, reason: str, failure: str | None
    ) -> None:
        """A restored account that will not resume: sell its holdings at the restored marks."""
        risk = self._require_risk()
        try:
            await risk.register_session(
                session.session_id,
                session._executor,
                allocated=session.paper_capital or Decimal("0"),
                symbols=session.symbols,
                exchange_id=session.exchange_id,
                strategy_id=session.strategy_id,
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
            )

    # -- Background tasks -----------------------------------------------------

    def _ensure_cleanup_loop(self) -> None:
        if self._stale_cleanup_task is None or self._stale_cleanup_task.done():
            self._stale_cleanup_task = asyncio.create_task(self._stale_order_cleanup_loop())

    async def _stale_order_cleanup_loop(self) -> None:
        """Cancel stale non-protective orders of running sessions every minute."""
        while True:
            try:
                await asyncio.sleep(60)
                await self.retry_unpersisted()
                for session in list(self._sessions.values()):
                    om = session._order_manager
                    if session.status != "running" or om is None:
                        continue
                    try:
                        cancelled = await om.cleanup_stale_orders()
                    except Exception:
                        logger.exception("Stale order cleanup failed for %s", session.session_id)
                        continue
                    if cancelled:
                        logger.info(
                            "Cleaned up %d stale orders in session %s",
                            len(cancelled),
                            session.session_id,
                        )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in stale order cleanup loop")
