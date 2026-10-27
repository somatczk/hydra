"""Trading session registry and public API.

``SessionManager`` lives on ``app.state.session_manager``. ``startup()`` loads both RiskServices
(paper, and the live scope when ``system_config.live_capital`` exists), recovers running
sessions — live rows resume through their own branch — then loads recent rows for display.
The service layer refuses a live start before this manager sees it (the armed check, R1);
this manager's live branch then checks credentials, ``live_capital`` and the kill switch.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from hydra.core import metrics
from hydra.core.config import HydraConfig
from hydra.core.types import ExchangeId, OrderType, Side
from hydra.execution import client_factory, session_store
from hydra.execution.client_factory import ClientFactory
from hydra.execution.errors import (
    ConfigReloadRefusedError,
    DuplicateSessionError,
    KillSwitchActiveError,
    LiveCapitalUnavailableError,
    LiveCredentialsError,
    PaperCapitalUnavailableError,
    SessionNotRunningError,
    SessionPersistError,
)
from hydra.execution.flatten import FlattenResult
from hydra.execution.paper_trading import PaperStateUnavailableError, PaperTradingExecutor
from hydra.execution.session_health import (
    HealthSnapshot as HealthSnapshot,
)
from hydra.execution.session_health import (
    SessionHealth as SessionHealth,
)
from hydra.execution.session_health import (
    SessionHealthMixin,
)
from hydra.execution.session_recovery import (
    PRE_P1_STATE as PRE_P1_STATE,
)
from hydra.execution.session_recovery import (
    SessionRecoveryMixin,
)
from hydra.execution.session_recovery import TradingSession as TradingSession
from hydra.execution.session_wiring import ManualOrderResult, SessionRuntime, probe_strategy
from hydra.risk.market_rules import MarketRulesCache
from hydra.risk.service import RiskService
from hydra.risk.state import (
    InMemoryRiskStateStore,
    PgRiskStateStore,
    RiskStateStore,
    RiskStateUnavailableError,
)
from hydra.strategy.config import StrategyConfig, config_hash, load_strategy_config
from hydra.strategy.engine import validate_strategy_config

if TYPE_CHECKING:
    from hydra.execution.protective_orders import ProtectionSpec

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
_STRATEGY_DIR = _CONFIG_DIR / "strategies"

_QUOTE = "USDT"


@dataclass(frozen=True, slots=True)
class ReloadResult:
    session_id: str
    from_hash: str | None
    to_hash: str


@dataclass(frozen=True, slots=True)
class KillSwitchReport:
    active: bool
    persisted: bool
    sessions_stopped: int
    positions_remaining: int
    flatten_failures: tuple[str, ...] = ()


def _non_parameter_changes(old: StrategyConfig, new: StrategyConfig) -> list[str]:
    """Fields whose change needs a restart (decision 6): everything but plain parameters.

    ``parameters.rules`` carries the entry/exit rule trees, so a rule change is refused like a
    risk or sizing change, not applied as a tunable parameter.
    """
    a = old.model_dump(mode="json", exclude={"enabled", "name", "parameters"})
    b = new.model_dump(mode="json", exclude={"enabled", "name", "parameters"})
    changed = sorted(k for k in a if a[k] != b[k])
    if (old.parameters.get("rules") or None) != (new.parameters.get("rules") or None):
        changed.append("parameters.rules")
    return changed


async def _close_client_quietly(client: Any) -> None:
    close = getattr(client, "close", None)
    if close is not None:
        try:
            await close()
        except Exception:
            logger.debug("Live client close failed", exc_info=True)


def _scope_flatten(mgr: SessionManager, scope: str) -> Callable[[str], Awaitable[list[str]]]:
    """A tier-3 flatten callback bound to one scope's sessions (Q1: per-scope targets)."""

    async def _flatten(reason: str) -> list[str]:
        return await mgr._flatten_for_risk(reason, scope=scope)

    return _flatten


class SessionManager(SessionRecoveryMixin, SessionHealthMixin):
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
        live_risk: RiskService | None = None,
    ) -> None:
        self._sessions: dict[str, TradingSession] = {}
        self._db_pool = db_pool
        self._config = config
        self._clients = clients or ClientFactory()
        self._market_rules = MarketRulesCache(
            loader=self._clients.markets_loader or self._clients.rest_pool.markets_loader
        )
        self._risk_store = risk_store
        self._paper_capital = paper_capital
        self.risk: RiskService | None = risk
        self.live_risk: RiskService | None = live_risk
        self._live_risk_loaded = False
        self._stale_cleanup_task: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self._lock = asyncio.Lock()
        self._unpersisted: set[str] = set()

    # -- Startup -------------------------------------------------------------

    async def startup(self) -> None:
        """Load risk state, recover running sessions, then load recent rows for display.

        A missing ``system_config.paper_capital`` or unreadable risk state raises: there is no
        default capital to fall back to. The live-scoped service loads only when
        ``system_config.live_capital`` exists; paper behaviour is unaffected without it.
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
        live = self._build_live_risk()
        if live is not None and await self._live_capital_configured():
            try:
                await live.load()
                self._live_risk_loaded = True
            except RiskStateUnavailableError as exc:
                logger.info("Live risk service not loaded yet: %s", exc)
        await self._recover()
        if risk is not None:
            await risk.recovery_complete()
        if live is not None and self._live_risk_loaded:
            await live.recovery_complete()
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
            self.risk.on_must_flatten = _scope_flatten(self, "paper")
        if self.risk.on_kill_switch is None:
            self.risk.on_kill_switch = self._kill_switch_for_risk
        return self.risk

    def _build_live_risk(self) -> RiskService | None:
        """Q1's second, live-scoped service; the kill switch stays one global row."""
        if self.live_risk is None:
            if self._db_pool is None:
                return None
            self.live_risk = RiskService(
                PgRiskStateStore(self._db_pool, scope="live", capital_key="live_capital"),
                scope="live",
                market_rules=self._market_rules,
            )
        if self.live_risk.on_must_flatten is None:
            self.live_risk.on_must_flatten = _scope_flatten(self, "live")
        if self.live_risk.on_kill_switch is None:
            self.live_risk.on_kill_switch = self._kill_switch_for_risk
        return self.live_risk

    async def _live_capital_configured(self) -> bool:
        """True when the system_config 'live_capital' row exists (the live load needs it)."""
        if self._db_pool is None:
            return False
        try:
            async with self._db_pool.acquire() as conn:
                found = await conn.fetchval(
                    "SELECT 1 FROM system_config WHERE key = 'live_capital'"
                )
        except Exception:
            logger.debug("live_capital probe failed", exc_info=True)
            return False
        return found is not None

    async def _ensure_live_risk(self) -> RiskService:
        """The live-scoped service, loaded and evaluating; 403 when its baseline is missing."""
        live = self._build_live_risk()
        if live is None:
            raise LiveCapitalUnavailableError(
                "live trading needs the database (live_capital and credentials live there)"
            )
        if not self._live_risk_loaded:
            try:
                await live.load()
            except RiskStateUnavailableError as exc:
                raise LiveCapitalUnavailableError(str(exc)) from exc
            self._live_risk_loaded = True
            await live.recovery_complete()
        return live

    def _risk_for(self, session: TradingSession) -> RiskService:
        """The service a session registers its sleeve in: the live scope for live rows."""
        if session.trading_mode == "live":
            live = self.live_risk
            if live is not None:
                return live
        return self._require_risk()

    def _require_risk(self) -> RiskService:
        if self.risk is None:
            raise PaperCapitalUnavailableError(
                "paper trading needs system_config.paper_capital (no database is configured)"
            )
        return self.risk

    # -- Public API ----------------------------------------------------------

    def kill_switch_active(self) -> bool:
        """The one global switch, as either scope's service sees it (they latch in lockstep)."""
        return any(
            svc is not None and svc.kill_switch_active() for svc in (self.risk, self.live_risk)
        )

    async def start_session(
        self,
        strategy_id: str,
        trading_mode: str,
        paper_capital: Decimal | None = None,
        actor: str | None = None,
    ) -> str:
        """Start a paper or live session; returns its id. Raises the typed errors in ``errors``.

        The service layer refuses a live start first (the armed check, R1); this live branch
        then checks stored credentials, ``live_capital`` and the kill switch, builds the keyed
        client and launches the same runtime path with the live-scoped RiskService (Q1).
        *actor* names the starting principal (``user:<name>`` / ``service:mcp``) and is
        persisted as the row's ``created_by``.
        """
        if self.kill_switch_active():
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
                return await self._start_live_session(cfg, exchange_id, actor)
            risk = self._require_risk()
            required_history = await probe_strategy(cfg, self._config)
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
                strategy_config=cfg.model_dump(mode="json"),
                config_hash=config_hash(cfg),
                created_by=actor,
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
            runtime = self._runtime(session, cfg, executor, required_history)
            await self._launch(runtime, capital, recovering=False)
            session.status = "running"
            session.started_at = datetime.now(UTC)
            self._sessions[session_id] = session
            self._ensure_cleanup_loop()
            await session_store.persist_session(self._db_pool, session)
        logger.info("Started paper session %s for strategy %s", session_id, strategy_id)
        return session_id

    # -- Live sessions ---------------------------------------------------------

    async def _start_live_session(
        self, cfg: StrategyConfig, exchange_id: ExchangeId, actor: str | None
    ) -> str:
        """Credentials, capital, the keyed client, then the same launch path (Q1's live scope)."""
        creds = await self._live_credentials(exchange_id)
        allocation = await self._live_allocation()
        await self._ensure_live_risk()
        client = self._clients.trading_client_factory(
            exchange_id, creds, self._config.trading.testnet
        )
        try:
            free = (await client.fetch_balance()).get(_QUOTE, Decimal("0"))
        except Exception as exc:
            await _close_client_quietly(client)
            raise LiveCredentialsError(
                f"stored {exchange_id} credentials could not read the account: {exc}"
            ) from exc
        seed = min(free, allocation)
        if seed <= 0:
            await _close_client_quietly(client)
            raise ValueError(
                f"no free {_QUOTE} to allocate for a live session (free {free}, "
                f"live_capital {allocation})"
            )
        required_history = await probe_strategy(cfg, self._config)
        session_id = str(uuid.uuid4())
        session = TradingSession(
            session_id=session_id,
            strategy_id=cfg.id,
            trading_mode="live",
            exchange_id=exchange_id,
            symbols=list(cfg.symbols),
            timeframe=str(cfg.timeframes.primary),
            paper_capital=seed,
            strategy_config=cfg.model_dump(mode="json"),
            config_hash=config_hash(cfg),
            created_by=actor,
        )
        rules = await self._market_rules.get(exchange_id, cfg.symbols)
        executor = client_factory.build_live_executor(
            exchange_id,
            client=client,
            market_rules=rules,
            db_pool=self._db_pool,
            strategy_id=cfg.id,
            session_id=session_id,
            initial_quote=seed,
            poll_interval=float(self._config.trading.live_poll_interval_s),
        )
        await executor.checkpoint()
        runtime = self._runtime(session, cfg, executor, required_history)
        await self._launch(runtime, seed, recovering=False)
        session.status = "running"
        session.started_at = datetime.now(UTC)
        self._sessions[session_id] = session
        self._ensure_cleanup_loop()
        await session_store.persist_session(self._db_pool, session)
        logger.info("Started live session %s for strategy %s", session_id, cfg.id)
        return session_id

    async def _live_credentials(self, exchange_id: ExchangeId) -> dict[str, str | None]:
        """Stored, decryptable, environment-validated keys; 403 when any leg is missing."""
        pool = self._db_pool
        if pool is None:
            raise LiveCredentialsError("live trading needs stored credentials (no database)")
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT encrypted_key, encrypted_secret, encrypted_passphrase, validated_at,"
                " validated_environment FROM exchange_credentials WHERE exchange_id = $1",
                exchange_id,
            )
        if row is None:
            raise LiveCredentialsError(f"no stored credentials for {exchange_id}")
        environment = "testnet" if self._config.trading.testnet else "mainnet"
        if row["validated_at"] is None or str(row["validated_environment"]) != environment:
            raise LiveCredentialsError(
                f"stored {exchange_id} credentials are not validated for the {environment} "
                "environment; reconnect them in Settings"
            )
        from cryptography.fernet import InvalidToken

        from hydra.core.encryption import CredentialKeyError, decrypt

        try:
            return {
                "api_key": decrypt(row["encrypted_key"]),
                "api_secret": decrypt(row["encrypted_secret"]),
                "passphrase": (
                    decrypt(row["encrypted_passphrase"]) if row["encrypted_passphrase"] else None
                ),
            }
        except (InvalidToken, CredentialKeyError) as exc:
            raise LiveCredentialsError(
                f"stored {exchange_id} credentials cannot be decrypted: {exc}"
            ) from exc

    async def _live_allocation(self) -> Decimal:
        """The live budget a NEW live session may seed (F6): ``live_capital`` minus every
        other running-or-holding live session's allocation, mirroring paper's ``_allocate``
        (R2: the live_capital read only — the write is the arm ceremony's)."""
        if self._db_pool is None:
            raise LiveCapitalUnavailableError("live trading needs system_config live_capital")
        async with self._db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM system_config WHERE key = 'live_capital'")
        if row is None:
            raise LiveCapitalUnavailableError(
                "system_config has no 'live_capital' row; it is set by the arm ceremony"
            )
        raw = row["value"]
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
            capital = Decimal(str(value))
        except (ValueError, ArithmeticError) as exc:
            raise LiveCapitalUnavailableError(
                f"system_config live_capital is unreadable: {exc}"
            ) from exc
        if capital <= 0:
            raise LiveCapitalUnavailableError(
                f"system_config live_capital must be > 0, got {capital}"
            )
        allocated = sum(
            (
                s.paper_capital
                for s in self._sessions.values()
                if s.trading_mode == "live"
                and (s.status == "running" or s.holdings > 0)
                and s.paper_capital
            ),
            Decimal("0"),
        )
        available = capital - allocated
        if available <= 0:
            raise ValueError(
                f"Insufficient live capital: live_capital={capital}, allocated={allocated}"
            )
        return available

    async def _recover_live_one(self, session: TradingSession) -> None:
        """Restart recovery of a live row: restore the sleeve, rebuild the client, resume.

        Restore or rebuild failure marks the session error and flattens exactly like the
        paper path; fills that happened while down are applied exactly once by the
        poller's first sweep (the live_fills unique constraint is the guard) and the
        protective group is re-adopted from the tracked rows.
        """
        from hydra.execution.live_executor import LiveTradingExecutor

        exchange_id = client_factory.validate_venue(session.exchange_id)
        rules_error: Exception | None = None
        rules: dict[str, Any] | None = None
        try:
            rules = await self._market_rules.get(exchange_id, session.symbols)
        except Exception as exc:
            rules_error = exc
        try:
            creds = await self._live_credentials(exchange_id)
            await self._ensure_live_risk()
            client = self._clients.trading_client_factory(
                exchange_id, creds, self._config.trading.testnet
            )
        except Exception as exc:
            logger.exception("Live session %s cannot rebuild its client", session.session_id)
            await self._close_restored(session, "recovery_failed", f"live setup failed: {exc}")
            return
        try:
            if rules_error is not None:
                raise rules_error
            executor = await LiveTradingExecutor.restore_live(
                self._db_pool,
                session.session_id,
                client=client,
                market_rules=rules,
                poll_interval=float(self._config.trading.live_poll_interval_s),
            )
            if executor is None:
                raise PaperStateUnavailableError(f"no stored live state for {session.session_id!r}")
            if self.kill_switch_active():
                session._executor = executor
                await self._close_restored(session, "kill_switch", None)
                return
            if session.paper_capital is None or session.paper_capital <= 0:
                raise LiveCapitalUnavailableError("the session row has no live allocation")
            cfg = self._recovery_config(session)
            client_factory.validate_venue(session.exchange_id, cfg.exchange.market_type)
            required_history = await probe_strategy(cfg, self._config)
            session._executor = executor
            runtime = self._runtime(session, cfg, executor, required_history)
            await self._launch(runtime, session.paper_capital, recovering=True)
        except Exception as exc:
            logger.exception("Restored live session %s cannot resume", session.session_id)
            await self._close_restored(session, "recovery_failed", f"recovery failed: {exc}")
            return
        logger.info(
            "Recovered live session %s for strategy %s", session.session_id, session.strategy_id
        )

    async def reload_config(self, strategy_id: str) -> ReloadResult:
        """Queue the strategy file's plain ``parameters`` for the session's next closed bar.

        Raises ``SessionNotRunningError`` (no running session), ``KeyError`` /
        ``FileNotFoundError`` (no strategy file to reload from), ``ValueError`` (invalid file)
        or ``ConfigReloadRefusedError`` (a change beyond plain parameter values — rules, risk,
        sizing, symbols, timeframes, class, or a ``required_history`` the session's context
        cannot hold: restart instead).
        """
        session = next(
            (
                s
                for s in self._sessions.values()
                if s.strategy_id == strategy_id and s.status == "running" and s._runtime
            ),
            None,
        )
        runtime = session._runtime if session is not None else None
        if session is None or runtime is None:
            raise SessionNotRunningError(f"Strategy {strategy_id} has no running session")
        new = self._strict_strategy_config(strategy_id)
        changed = _non_parameter_changes(runtime.cfg, new)
        max_bars = runtime.context.max_bars
        if max_bars is not None and await probe_strategy(new, self._config) > max_bars:
            changed.append("parameters.required_history")
        if changed:
            raise ConfigReloadRefusedError(changed)
        runtime.request_reload(new)
        return ReloadResult(session.session_id, session.config_hash, config_hash(new))

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
        """Kill switch: latch first, then flatten and stop every running or errored session.

        Both scope services latch (Q1): each writes the one global switch row and audits
        its own ``kill_switch_on`` row, so paper and live stay in lockstep.
        """
        persisted = False
        for svc in (self.risk, self.live_risk):
            if svc is None:
                continue
            latched = await svc.activate_kill_switch(actor, reason)
            persisted = persisted or latched.persisted
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
        failures.extend(await self._sweep_live_orders(targets))
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

    async def _sweep_live_orders(self, sessions: list[TradingSession]) -> list[str]:
        """After every flatten, cancel ALL orders on each live-traded symbol (Q2).

        Blast radius: every open order on the symbol's account, hydra's or not; a
        single-session stop stays scoped to its own order lists. The duck-typed gate is
        the executor's ``cancel_all_orders`` (only the live executor exposes it), and
        the swept symbols are the ones the executor actually traded — held positions or
        tracked open orders — not the configured superset (A9).
        """
        sweep: dict[Any, set[str]] = {}
        for session in sessions:
            executor = session._executor
            if getattr(executor, "cancel_all_orders", None) is None:
                continue
            sweep.setdefault(executor, set()).update(await self._traded_symbols(session, executor))
        failures: list[str] = []
        for executor, symbols in sweep.items():
            for symbol in sorted(symbols):
                try:
                    await executor.cancel_all_orders(symbol)
                except Exception as exc:
                    logger.exception("Kill-switch sweep failed for %s", symbol)
                    failures.append(f"{symbol}: cancel-all failed ({exc})")
        return failures

    async def _traded_symbols(self, session: TradingSession, executor: Any) -> set[str]:
        """The symbols a live session actually traded (held, or with tracked open orders).

        An executor exposing neither read keeps the configured superset — the sweep
        errs toward cancelling, never toward skipping.
        """
        get_positions = getattr(executor, "get_positions", None)
        fetch_open_orders = getattr(executor, "fetch_open_orders", None)
        if get_positions is None and fetch_open_orders is None:
            return set(session.symbols)
        traded: set[str] = set()
        for symbol in session.symbols:
            try:
                held = get_positions is not None and any(
                    p.quantity > 0 for p in await get_positions(symbol)
                )
                tracked = fetch_open_orders is not None and bool(await fetch_open_orders(symbol))
            except Exception:
                logger.exception("Traded-symbol read failed for %s; sweeping it", symbol)
                traded.add(symbol)
                continue
            if held or tracked:
                traded.add(symbol)
        return traded

    async def release_kill_switch(self, actor: str = "operator", reason: str = "") -> None:
        """Release the kill switch (raises when it cannot be persisted; stays latched)."""
        for svc in (self.risk, self.live_risk):
            if svc is not None:
                await svc.release_kill_switch(actor, reason)
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
        for session in list(self._sessions.values()):
            await _close_client_quietly(getattr(session._executor, "exchange_client", None))
        for task in list(self._background):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        logger.info("Graceful shutdown complete; running sessions are recovered at startup")

    # -- Internal: build and run ----------------------------------------------

    def _strict_strategy_config(self, strategy_id: str) -> StrategyConfig:
        """Locate the file for *strategy_id* and validate it strictly (reload's 422 path).

        Raises ``KeyError`` when no file has the id (unreadable files are skipped, consistent
        with ``_find_strategy_config``), ``ValueError``/``TypeError`` when the matching file is
        invalid.
        """
        if not _STRATEGY_DIR.exists():
            raise FileNotFoundError(f"Strategy directory not found: {_STRATEGY_DIR}")
        for path in sorted(_STRATEGY_DIR.iterdir()):
            if path.suffix not in (".yaml", ".yml"):
                continue
            try:
                raw = yaml.safe_load(path.read_text())
            except (OSError, yaml.YAMLError) as exc:
                logger.error("Skipping unreadable strategy file %s: %s", path.name, exc)
                continue
            if not isinstance(raw, dict) or raw.get("id") != strategy_id:
                continue
            cfg = validate_strategy_config(raw)
            cfg.enabled = True
            return cfg
        raise KeyError(f"Strategy config not found: {strategy_id}")

    def _find_strategy_config(self, strategy_id: str) -> StrategyConfig:
        """Locate and load a strategy config by ID; other bad files are skipped and logged (SM-15).

        Raises ``KeyError`` only when no valid file has the id. A loadable file with an invalid
        strategy_class or rules still surfaces later (``probe_strategy``), so callers keep their
        existing error types for that.
        """
        if not _STRATEGY_DIR.exists():
            raise FileNotFoundError(f"Strategy directory not found: {_STRATEGY_DIR}")
        for path in sorted(_STRATEGY_DIR.iterdir()):
            if path.suffix not in (".yaml", ".yml"):
                continue
            try:
                cfg = load_strategy_config(path)
            except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
                logger.error("Skipping invalid strategy config %s: %s", path.name, exc)
                continue
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
        if session.trading_mode == "live":
            live = self.live_risk
            if live is None:
                raise LiveCapitalUnavailableError(
                    "a live session needs the live-scoped risk service"
                )
            return SessionRuntime(
                session,
                cfg,
                executor=executor,
                risk=live,
                clients=self._clients,
                hydra_config=self._config,
                db_pool=self._db_pool,
                required_history=required_history,
                on_failure=self._on_runtime_failure,
            )
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
        session = runtime.session
        risk = self._risk_for(session)
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
            if self.kill_switch_active():
                raise KillSwitchActiveError("Kill switch latched while the session was starting")
        except BaseException:
            await runtime.shutdown()
            if registered:
                await risk.unregister_session(session.session_id)
            await _close_client_quietly(getattr(session._executor, "exchange_client", None))
            session._runtime = None
            raise
        session._task = asyncio.create_task(runtime.run(), name=f"session-{session.session_id}")

    async def _set_live_stop_gap(self, session: TradingSession) -> None:
        """Complete the gauge lifecycle at stop time (R27).

        The per-bar hook of a running session owns ``hydra_live_protective_gap``
        otherwise, and a stopped session runs no more bars: a live stop that leaves
        positions sets the gap for the symbols still held (the alert keeps firing —
        R13), while a clean flatten clears the series.
        """
        if session.trading_mode != "live":
            return
        symbols = list(session.symbols)
        gap = session.positions_at_stop > 0
        if gap:
            symbols = await self._live_held_symbols(session)
        for symbol in symbols:
            metrics.set_live_protective_gap(
                session.session_id, session.strategy_id, session.exchange_id, symbol, gap
            )

    async def _live_held_symbols(self, session: TradingSession) -> list[str]:
        """The symbols a stopped live session still holds; unreadable means all of them."""
        get_positions = getattr(session._executor, "get_positions", None)
        if get_positions is None:
            return list(session.symbols)
        held: list[str] = []
        for symbol in session.symbols:
            try:
                positions = await get_positions(symbol)
            except Exception:
                logger.exception(
                    "Held symbols of %s unreadable; paging every symbol", session.session_id
                )
                return list(session.symbols)
            if any(p.quantity > 0 for p in positions):
                held.append(symbol)
        return held or list(session.symbols)

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
            risk = self._risk_for(session)
            if session.session_id in risk.registered_sessions():
                await risk.unregister_session(session.session_id)
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
        await self._set_live_stop_gap(session)
        logger.info("Session %s stopped (%s)", session.session_id, reason)
        return result

    async def _close_executor(self, session: TradingSession, reason: str) -> FlattenResult:
        """Flatten a paper account that has no runtime (cancel resting orders, sell base)."""
        executor = session._executor
        if executor is None:
            # nothing was restored: no sleeve to flatten, nothing to carry
            return FlattenResult(positions_remaining=session.holdings)
        failures: list[str] = []
        try:
            await executor.flatten(reason=reason)
        except Exception as exc:
            logger.exception("Flatten of %s without a runtime failed", session.session_id)
            failures.append(str(exc))
        remaining = session.holdings
        session.positions_at_stop = remaining
        session.flatten_incomplete = remaining > 0
        risk = self._risk_for(session)
        if remaining == 0 and session.session_id in risk.registered_sessions():
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
            risk = self._risk_for(session)
            if result.positions_remaining == 0 and session.session_id in risk.registered_sessions():
                await risk.unregister_session(session.session_id)
        await self._set_live_stop_gap(session)
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

    async def _flatten_for_risk(self, reason: str, *, scope: str = "paper") -> list[str]:
        """Tier-3 breaker: flatten the sessions of *scope* (they keep running; entries blocked)."""
        failures: list[str] = []
        for session in list(self._sessions.values()):
            if session.trading_mode != scope:
                continue
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
