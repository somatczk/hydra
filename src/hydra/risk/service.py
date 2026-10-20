"""One RiskService per process: account equity, persisted breakers, kill switch and order checks.

Account equity (the persistent paper ledger) = paper_capital + carried PnL of stopped sessions
+ sum over running sessions of (quote balance + signed position qty x mark - allocated capital).
Marks come from closed bars. Every breaker transition, reset and kill-switch change is written to
risk_events and logged on 'hydra.audit'. If the state cannot be loaded or the last save failed,
opening orders are refused ('risk state unavailable'); reducing orders still pass.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from hydra.core import metrics
from hydra.core.events import OrderFillEvent
from hydra.core.types import Direction, OrderRequest, Position
from hydra.risk.circuit_breakers import (
    TIER_ACTIONS,
    TIER_LABELS,
    CircuitBreakerConfig,
    CircuitBreakerManager,
    CircuitBreakerRestrictions,
    Transition,
)
from hydra.risk.market_rules import MarketRules, MarketRulesCache
from hydra.risk.pretrade import (
    OPEN,
    REDUCE,
    Intent,
    PortfolioState,
    PreTradeRiskManager,
    RiskConfig,
    RiskDecision,
    gross_exposure,
)
from hydra.risk.sizing import size_entry
from hydra.risk.state import (
    RiskEventRecord,
    RiskState,
    RiskStateStore,
    RiskStateUnavailableError,
)

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("hydra.audit")

QUOTE = "USDT"
ENGINE_ACTOR = "risk_engine"
RATE_WINDOW = timedelta(minutes=1)


class RiskExecutor(Protocol):
    async def get_balance(self) -> dict[str, Decimal]: ...

    async def get_positions(self, symbol: str | None = None) -> list[Position]: ...


FlattenCallback = Callable[[str], Awaitable[Sequence[str]]]
KillSwitchCallback = Callable[[str], Awaitable[None]]


class RiskLockTimeoutError(RuntimeError):
    """An opening order did not get the RiskService lock in time."""


@dataclass(frozen=True, slots=True)
class KillSwitchResult:
    """activate_kill_switch result; the session owner fills in what stopping the sessions did."""

    active: bool
    persisted: bool
    sessions_stopped: int = 0
    flatten_failures: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BreakerView:
    tier: int
    label: str
    action: str
    threshold: Decimal
    status: str
    active_since: datetime | None
    requires_manual_review: bool


@dataclass(frozen=True, slots=True)
class AccountView:
    available: bool
    paper_capital: Decimal | None
    equity: Decimal | None
    peak_equity: Decimal | None
    drawdown: Decimal
    day: date | None
    day_start_equity: Decimal | None
    daily_pnl: Decimal
    daily_loss: Decimal
    consecutive_losses: int
    carried_pnl: Decimal
    gross_exposure: Decimal
    tier: int
    kill_switch: bool
    restrictions: CircuitBreakerRestrictions
    config: RiskConfig


@dataclass(slots=True)
class _Session:
    session_id: str
    executor: RiskExecutor
    allocated: Decimal
    symbols: list[str]
    exchange_id: str
    strategy_id: str
    rules: dict[str, MarketRules]
    openings: deque[datetime] = field(default_factory=deque)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _cb_config(cfg: RiskConfig) -> CircuitBreakerConfig:
    return CircuitBreakerConfig(
        max_drawdown_pct=cfg.max_drawdown_pct,
        daily_loss_limit=cfg.max_daily_loss_pct,
        max_consecutive_losses=cfg.max_consecutive_losses,
        consecutive_pause_hours=cfg.consecutive_loss_cooldown_hours,
    )


class RiskService:
    def __init__(
        self,
        store: RiskStateStore,
        *,
        scope: str = "paper",
        clock: Callable[[], datetime] = _utcnow,
        market_rules: MarketRulesCache | None = None,
        lock_timeout: float = 5.0,
        on_must_flatten: FlattenCallback | None = None,
        on_kill_switch: KillSwitchCallback | None = None,
    ) -> None:
        self._store = store
        self._scope = scope
        self._clock = clock
        self._rules = market_rules or MarketRulesCache()
        self._lock_timeout = lock_timeout
        self.on_must_flatten = on_must_flatten
        self.on_kill_switch = on_kill_switch

        self.lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._config = RiskConfig()
        self._pretrade = PreTradeRiskManager(self._config)
        self._cb = CircuitBreakerManager(_cb_config(self._config))
        self._state = RiskState()
        self._paper_capital: Decimal | None = None
        self._kill_switch = False
        self._loaded = False
        self._persist_ok = True
        self._sessions: dict[str, _Session] = {}
        self._marks: dict[tuple[str, str], Decimal] = {}
        self._mark_ts: dict[tuple[str, str], datetime] = {}
        self._gross = Decimal("0")

    # ------------------------------------------------------------------
    # Lifecycle and config
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._loaded and self._persist_ok

    @property
    def config(self) -> RiskConfig:
        return self._config

    @property
    def paper_capital(self) -> Decimal | None:
        return self._paper_capital

    async def load(self) -> None:
        """Read config, paper capital and persisted state; raises RiskStateUnavailableError."""
        try:
            loaded = await self._store.load_config()
            state = await self._store.load_state()
        except Exception as exc:
            self._loaded = False
            logger.exception("Risk state unavailable; opening orders are refused")
            if isinstance(exc, RiskStateUnavailableError):
                raise
            raise RiskStateUnavailableError(str(exc)) from exc
        self._apply_config(loaded.config)
        self._paper_capital = loaded.paper_capital
        self._state = state
        self._cb = CircuitBreakerManager(_cb_config(loaded.config), state.cb)
        self._kill_switch = self._kill_switch or loaded.kill_switch
        self._loaded = True
        self._persist_ok = True
        metrics.set_kill_switch_active(self._kill_switch)
        metrics.set_circuit_breaker_tier(self._scope, self._cb.state.tier)

    def _apply_config(self, cfg: RiskConfig) -> None:
        self._config = cfg
        self._pretrade = PreTradeRiskManager(cfg)
        self._cb.reconfigure(_cb_config(cfg))

    async def reload_config(self, actor: str = "system") -> None:
        """Re-read risk_config and paper_capital; a paper_capital change rebases the ledger."""
        if not self._loaded:
            await self.load()
            await self.evaluate()
            return
        loaded = await self._store.load_config()
        self._apply_config(loaded.config)
        if loaded.kill_switch and not self._kill_switch:
            self._kill_switch = True
            metrics.set_kill_switch_active(True)
        if self._paper_capital is not None and loaded.paper_capital != self._paper_capital:
            await self.reset_paper_account(
                actor=actor,
                reason=f"paper_capital changed {self._paper_capital} -> {loaded.paper_capital}",
                paper_capital=loaded.paper_capital,
            )
        else:
            self._paper_capital = loaded.paper_capital
            await self.evaluate()

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    async def register_session(
        self,
        session_id: str,
        executor: RiskExecutor,
        *,
        allocated: Decimal,
        symbols: Iterable[str],
        exchange_id: str,
        strategy_id: str = "",
    ) -> None:
        """Add a session to the account; loads its market rules (failure aborts the start)."""
        if allocated <= 0:
            raise ValueError(f"allocated capital must be > 0, got {allocated}")
        syms = [str(s) for s in symbols]
        rules = await self._rules.get(exchange_id, syms)
        self._sessions[session_id] = _Session(
            session_id=session_id,
            executor=executor,
            allocated=allocated,
            symbols=syms,
            exchange_id=exchange_id,
            strategy_id=strategy_id,
            rules=rules,
        )

    async def unregister_session(self, session_id: str, actor: str = ENGINE_ACTOR) -> Decimal:
        """Remove a session and carry its PnL (equity - allocated) into the account."""
        async with self._state_lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return Decimal("0")
            equity, _, _ = await self._session_equity(session)
            pnl = equity - session.allocated
            self._state.carried_pnl += pnl
        await self._audit(
            "session_carried",
            actor,
            f"session {session_id} stopped",
            details={"session_id": session_id, "pnl": pnl, "carried": self._state.carried_pnl},
        )
        await self.evaluate()
        return pnl

    def registered_sessions(self) -> list[str]:
        return list(self._sessions)

    def _marks_for(self, exchange_id: str) -> dict[str, Decimal]:
        return {sym: price for (ex, sym), price in self._marks.items() if ex == exchange_id}

    async def _session_equity(
        self, session: _Session
    ) -> tuple[Decimal, list[Position], dict[str, Decimal]]:
        balances = await session.executor.get_balance()
        positions = await session.executor.get_positions()
        marks = self._marks_for(session.exchange_id)
        equity = balances.get(QUOTE, Decimal("0"))
        for pos in positions:
            if pos.direction == Direction.FLAT or pos.quantity <= 0:
                continue
            price = marks.get(str(pos.symbol)) or pos.avg_entry_price
            signed = pos.quantity if pos.direction == Direction.LONG else -pos.quantity
            equity += signed * price
        return equity, positions, balances

    async def _account(
        self,
    ) -> tuple[Decimal, Decimal, dict[str, tuple[Decimal, list[Position], dict[str, Decimal]]]]:
        per: dict[str, tuple[Decimal, list[Position], dict[str, Decimal]]] = {}
        equity = (self._paper_capital or Decimal("0")) + self._state.carried_pnl
        gross = Decimal("0")
        for sid, session in self._sessions.items():
            per[sid] = await self._session_equity(session)
            equity += per[sid][0] - session.allocated
            gross += gross_exposure(per[sid][1], self._marks_for(session.exchange_id))
        return equity, gross, per

    # ------------------------------------------------------------------
    # Marks, fills and breaker evaluation
    # ------------------------------------------------------------------

    async def on_mark(self, exchange_id: str, symbol: str, price: Decimal, ts: datetime) -> None:
        """Closed-bar close for *symbol*; stale or repeated bar timestamps are ignored."""
        key = (exchange_id, str(symbol))
        last = self._mark_ts.get(key)
        if price <= 0 or (last is not None and ts <= last):
            return
        self._marks[key] = price
        self._mark_ts[key] = ts
        await self.evaluate()

    def mark(self, exchange_id: str, symbol: str) -> Decimal | None:
        return self._marks.get((exchange_id, str(symbol)))

    async def on_fill(self, session_id: str, fill: OrderFillEvent) -> None:
        """Closing fills (realized_pnl set) drive the consecutive-loss counter."""
        async with self._state_lock:
            now = self._clock()
            transitions = self._cb.expire(now)
            self._apply_counter_resets(transitions)
            if fill.realized_pnl is not None:
                if fill.realized_pnl < 0:
                    self._state.consecutive_losses += 1
                    self._state.last_loss_at = now
                else:
                    self._state.consecutive_losses = 0
        await self._audit_transitions(transitions)
        await self.evaluate()

    def _apply_counter_resets(self, transitions: list[Transition]) -> None:
        if any(t.kind == "consecutive_reset" for t in transitions):
            self._state.consecutive_losses = 0

    async def evaluate(self) -> list[Transition]:
        """Recompute equity, drawdown and daily loss; update breakers; persist; act."""
        async with self._state_lock:
            if not self._loaded:
                return []
            now = self._clock()
            equity, gross, _ = await self._account()
            st = self._state
            st.equity = equity
            st.equity_at = now
            today = now.astimezone(UTC).date()
            if st.day != today or st.day_start_equity is None:
                st.day = today
                st.day_start_equity = equity
            if st.peak_equity is None or equity > st.peak_equity:
                st.peak_equity = equity
            peak = st.peak_equity
            st.current_drawdown = (
                max(Decimal("0"), (peak - equity) / peak) if peak > 0 else Decimal("0")
            )
            start = st.day_start_equity
            daily_loss = max(Decimal("0"), (start - equity) / start) if start > 0 else Decimal("0")
            transitions = self._cb.update(
                drawdown=st.current_drawdown,
                daily_loss=daily_loss,
                consecutive_losses=st.consecutive_losses,
                now=now,
            )
            self._apply_counter_resets(transitions)
            st.cb = self._cb.state
            self._gross = gross
            await self._save()
            self._publish_metrics(equity, gross, start)
        await self._audit_transitions(transitions)
        await self._act(transitions)
        return transitions

    async def _act(self, transitions: list[Transition]) -> None:
        escalations = [t for t in transitions if t.kind == "cb_escalate"]
        if not escalations:
            return
        top = max(t.tier for t in escalations)
        if top >= 4:
            await self.activate_kill_switch(
                ENGINE_ACTOR, f"circuit breaker tier 4: drawdown {self._state.current_drawdown:.4f}"
            )
        if top >= 3 and self.on_must_flatten is not None:
            failures = await self.on_must_flatten(f"circuit breaker tier {top}")
            if failures:
                await self._audit(
                    "flatten_failed",
                    ENGINE_ACTOR,
                    "tier flatten left positions",
                    top,
                    {"failures": list(failures)},
                )
        if top >= 4 and self.on_kill_switch is not None:
            await self.on_kill_switch(f"circuit breaker tier {top}")

    async def _save(self) -> None:
        try:
            await self._store.save_state(self._state)
        except Exception:
            self._persist_ok = False
            metrics.record_risk_state_persist_failure()
            logger.exception("Risk state not persisted; opening orders are refused until it is")
            return
        self._persist_ok = True

    def _publish_metrics(self, equity: Decimal, gross: Decimal, day_start: Decimal) -> None:
        dd = min(Decimal("1"), self._state.current_drawdown)
        metrics.update_portfolio(float(equity), float(dd), float(equity - day_start))
        if equity > 0:
            metrics.set_portfolio_heat(float(gross / equity))
        metrics.set_circuit_breaker_tier(self._scope, self._cb.state.tier)

    # ------------------------------------------------------------------
    # Order path
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def order_slot(self, intent: Intent) -> AsyncIterator[None]:
        """Hold the account lock from check through create_order; openings give up on timeout."""
        start = time.monotonic()
        if intent == OPEN:
            try:
                await asyncio.wait_for(self.lock.acquire(), timeout=self._lock_timeout)
            except TimeoutError as exc:
                metrics.record_risk_lock_timeout()
                raise RiskLockTimeoutError(
                    f"risk lock not acquired within {self._lock_timeout}s"
                ) from exc
        else:
            await self.lock.acquire()
        metrics.observe_risk_lock_wait(time.monotonic() - start)
        try:
            yield
        finally:
            self.lock.release()

    async def snapshot(self, session_id: str) -> PortfolioState:
        session = self._sessions[session_id]
        now = self._clock()
        equity, _, per = await self._account()
        sleeve, positions, balances = per[session_id]
        account_positions = [p for value in per.values() for p in value[1]]
        return PortfolioState(
            portfolio_value=equity,
            positions=positions,
            balances=balances,
            marks=self._marks_for(session.exchange_id),
            sleeve_equity=sleeve,
            account_positions=account_positions,
            restrictions=self._cb.restrictions(now),
            kill_switch=self._kill_switch,
            state_available=self.available,
            opening_orders_last_minute=self._openings(session, now),
            market_rules=session.rules,
            quote_currency=QUOTE,
        )

    def _openings(self, session: _Session, now: datetime) -> int:
        while session.openings and now - session.openings[0] >= RATE_WINDOW:
            session.openings.popleft()
        return len(session.openings)

    def record_opening(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is not None:
            session.openings.append(self._clock())

    async def check(self, order: OrderRequest, session_id: str, intent: Intent) -> RiskDecision:
        if order.quantity <= 0:
            return RiskDecision(False, "sanity", f"non-positive quantity {order.quantity}", intent)
        if intent == REDUCE:
            return RiskDecision(True, "reduce", "reducing order: entry checks exempt", REDUCE)
        if session_id not in self._sessions:
            return RiskDecision(
                False, "unknown_session", f"session {session_id!r} not registered", OPEN
            )
        state = await self.snapshot(session_id)
        return self._pretrade.evaluate(order, state, OPEN)

    async def size_entry(
        self, session_id: str, symbol: str, strategy_fraction: Decimal
    ) -> Decimal | None:
        """Entry qty: sleeve equity x min(strategy, global max_position_pct) x CB multiplier."""
        session = self._sessions[session_id]
        mark = self.mark(session.exchange_id, symbol)
        rules = session.rules.get(str(symbol))
        if mark is None or rules is None:
            return None
        sleeve, _, balances = await self._session_equity(session)
        return size_entry(
            sleeve_equity=sleeve,
            fraction=min(strategy_fraction, self._config.max_position_pct),
            mark=mark,
            size_multiplier=self._cb.restrictions(self._clock()).position_size_multiplier,
            rules=rules,
            quote_balance=balances.get(QUOTE, Decimal("0")),
            fee_buffer=self._config.fee_buffer,
        )

    def market_rules(self, session_id: str) -> dict[str, MarketRules]:
        return dict(self._sessions[session_id].rules)

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------

    def kill_switch_active(self) -> bool:
        return self._kill_switch

    async def activate_kill_switch(self, actor: str, reason: str) -> KillSwitchResult:
        """Latch in memory first, then persist; a failed write keeps the latch (persisted=False)."""
        self._kill_switch = True
        metrics.set_kill_switch_active(True)
        persisted = True
        try:
            await self._store.set_kill_switch(True)
        except Exception:
            persisted = False
            metrics.record_risk_state_persist_failure()
            logger.exception("Kill switch latched in memory but not persisted")
        await self._audit(
            "kill_switch_on", actor, reason, self._cb.state.tier, {"persisted": persisted}
        )
        return KillSwitchResult(active=True, persisted=persisted)

    async def release_kill_switch(self, actor: str, reason: str) -> None:
        """Audit, persist, then unlatch; any failure raises and the switch stays latched."""
        await self._audit("kill_switch_off", actor, reason, self._cb.state.tier, required=True)
        await self._store.set_kill_switch(False)
        self._kill_switch = False
        metrics.set_kill_switch_active(False)

    # ------------------------------------------------------------------
    # Manual actions
    # ------------------------------------------------------------------

    async def reset_breaker(self, tier: int, *, actor: str, reason: str, confirm_tier: int) -> None:
        """Clear the active tier and rebase the high-water mark to current equity.

        Raises ValueError (bad tier or confirmation), TierNotActiveError, or the store's error
        when the audit row cannot be written; in every failure the breaker state is unchanged.
        """
        if tier not in range(1, 5):
            raise ValueError(f"tier must be 1-4, got {tier}")
        if confirm_tier != tier:
            raise ValueError(f"confirm_tier {confirm_tier} does not match tier {tier}")
        self._cb.reset_check(tier)
        st = self._state
        await self._audit(
            "cb_reset",
            actor,
            reason,
            tier,
            {"old_peak": st.peak_equity, "new_peak": st.equity, "drawdown": st.current_drawdown},
            required=True,
        )
        async with self._state_lock:
            self._cb.reset(tier)
            st.peak_equity = st.equity
            st.current_drawdown = Decimal("0")
            st.cb = self._cb.state
            await self._save()
            metrics.set_circuit_breaker_tier(self._scope, self._cb.state.tier)

    async def reset_paper_account(
        self, *, actor: str, reason: str, paper_capital: Decimal | None = None
    ) -> None:
        """Rebase the ledger: carried PnL 0, peak and day start at current equity, breakers cleared.

        The audit row is written first; if it fails nothing changes.
        """
        st = self._state
        new_capital = paper_capital if paper_capital is not None else self._paper_capital
        await self._audit(
            "paper_reset",
            actor,
            reason,
            self._cb.state.tier,
            {
                "old_capital": self._paper_capital,
                "new_capital": new_capital,
                "old_equity": st.equity,
                "old_peak": st.peak_equity,
                "old_carried": st.carried_pnl,
            },
            required=True,
        )
        async with self._state_lock:
            self._paper_capital = new_capital
            st.carried_pnl = Decimal("0")
            equity, _, _ = await self._account()
            st.peak_equity = equity
            st.day_start_equity = equity
            st.day = self._clock().astimezone(UTC).date()
            st.current_drawdown = Decimal("0")
            st.consecutive_losses = 0
            self._cb.clear()
            st.cb = self._cb.state
        await self.evaluate()

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    def restrictions(self) -> CircuitBreakerRestrictions:
        return self._cb.restrictions(self._clock())

    def breakers(self) -> list[BreakerView]:
        st = self._cb.state
        dd = self._state.current_drawdown
        views: list[BreakerView] = []
        for i, threshold in enumerate(self._cb.thresholds(), start=1):
            if st.tier >= i:
                status = "Tripped"
            elif dd >= threshold * Decimal("0.8"):
                status = "Warning"
            else:
                status = "Normal"
            views.append(
                BreakerView(
                    tier=i,
                    label=TIER_LABELS[i - 1],
                    action=TIER_ACTIONS[i - 1],
                    threshold=threshold,
                    status=status,
                    active_since=st.tier_since if st.tier >= i else None,
                    requires_manual_review=st.requires_manual_review and st.tier >= i >= 3,
                )
            )
        return views

    def account(self) -> AccountView:
        st = self._state
        equity = st.equity
        start = st.day_start_equity
        daily_pnl = equity - start if equity is not None and start is not None else Decimal("0")
        daily_loss = (
            max(Decimal("0"), -daily_pnl) / start
            if start is not None and start > 0
            else Decimal("0")
        )
        return AccountView(
            available=self.available,
            paper_capital=self._paper_capital,
            equity=equity,
            peak_equity=st.peak_equity,
            drawdown=st.current_drawdown,
            day=st.day,
            day_start_equity=start,
            daily_pnl=daily_pnl,
            daily_loss=daily_loss,
            consecutive_losses=st.consecutive_losses,
            carried_pnl=st.carried_pnl,
            gross_exposure=self._gross,
            tier=self._cb.state.tier,
            kill_switch=self._kill_switch,
            restrictions=self.restrictions(),
            config=self._config,
        )

    async def recent_events(self, limit: int = 50) -> list[RiskEventRecord]:
        return await self._store.recent_events(limit)

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    async def _audit_transitions(self, transitions: list[Transition]) -> None:
        for t in transitions:
            await self._audit(
                t.kind, ENGINE_ACTOR, t.detail, t.tier, {"previous_tier": t.previous_tier}
            )

    async def _audit(
        self,
        kind: str,
        actor: str,
        reason: str,
        tier: int | None = None,
        details: dict[str, Any] | None = None,
        *,
        required: bool = False,
    ) -> None:
        record = RiskEventRecord(
            kind=kind,
            actor=actor,
            reason=reason,
            tier=tier,
            details=dict(details or {}),
            ts=self._clock(),
            scope=self._scope,
        )
        try:
            await self._store.append_event(record)
        except Exception:
            if required:
                raise
            metrics.record_risk_state_persist_failure()
            logger.exception("risk_events write failed for %s", kind)
        audit_logger.info(
            json.dumps(
                {
                    "event": "risk." + kind,
                    "scope": self._scope,
                    "actor": actor,
                    "reason": reason,
                    "tier": tier,
                    "details": record.details,
                },
                default=str,
            )
        )


__all__ = [
    "AccountView",
    "BreakerView",
    "KillSwitchResult",
    "RiskExecutor",
    "RiskLockTimeoutError",
    "RiskService",
]
