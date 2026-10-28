"""Persisted account risk state (risk_state, scope 'paper') and the risk_events audit log."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol

from hydra.risk.circuit_breakers import CircuitBreakerState
from hydra.risk.pretrade import RiskConfig

logger = logging.getLogger(__name__)


class RiskStateUnavailableError(RuntimeError):
    """Risk state, config or paper capital could not be read or written."""


class RiskStateNotPersistedError(RiskStateUnavailableError):
    """A manual change was audited but could not be saved, so it was rolled back."""


@dataclass(slots=True)
class RiskState:
    """Account-level state; None means 'not established yet' (never a fabricated number)."""

    equity: Decimal | None = None
    equity_at: datetime | None = None
    peak_equity: Decimal | None = None
    current_drawdown: Decimal = Decimal("0")
    day: date | None = None
    day_start_equity: Decimal | None = None
    consecutive_losses: int = 0
    last_loss_at: datetime | None = None
    carried_pnl: Decimal = Decimal("0")
    paper_capital: Decimal | None = None
    cb: CircuitBreakerState = field(default_factory=CircuitBreakerState)


@dataclass(frozen=True, slots=True)
class RiskEventRecord:
    kind: str
    actor: str
    reason: str
    tier: int | None = None
    details: dict[str, Any] = field(default_factory=dict)
    ts: datetime | None = None
    id: int | None = None
    scope: str = "paper"


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    config: RiskConfig
    kill_switch: bool
    paper_capital: Decimal


class RiskStateStore(Protocol):
    async def load_config(self) -> LoadedConfig: ...

    async def load_state(self) -> RiskState: ...

    async def save_state(self, state: RiskState) -> None: ...

    async def set_kill_switch(self, active: bool) -> None: ...

    async def append_event(self, event: RiskEventRecord) -> None: ...

    async def recent_events(self, limit: int) -> list[RiskEventRecord]: ...


def _dec(value: Any, default: Decimal | None = None) -> Decimal | None:
    return default if value is None else Decimal(str(value))


def risk_config_from_row(row: Any) -> RiskConfig:
    """risk_config row -> RiskConfig; a NULL limit falls back to the dataclass default."""
    base = RiskConfig()

    def d(name: str) -> Decimal:
        value = row.get(name)
        return Decimal(getattr(base, name)) if value is None else Decimal(str(value))

    def i(name: str) -> int:
        value = row.get(name)
        return int(getattr(base, name) if value is None else value)

    return RiskConfig(
        max_position_pct=d("max_position_pct"),
        max_risk_per_trade=d("max_risk_per_trade"),
        max_portfolio_heat=d("max_portfolio_heat"),
        max_daily_loss_pct=d("max_daily_loss_pct"),
        max_drawdown_pct=d("max_drawdown_pct"),
        max_consecutive_losses=i("max_consecutive_losses"),
        max_concurrent_positions=i("max_concurrent_positions"),
        max_perp_leverage=i("max_perp_leverage"),
    )


def parse_paper_capital(raw: Any) -> Decimal:
    """system_config 'paper_capital' JSONB (asyncpg returns text) -> positive Decimal."""
    value = json.loads(raw) if isinstance(raw, str) else raw
    capital = Decimal(str(value))
    if capital <= 0:
        raise RiskStateUnavailableError(f"system_config paper_capital must be > 0, got {capital}")
    return capital


class PgRiskStateStore:
    """asyncpg-backed store; every call raises RiskStateUnavailableError on failure.

    *capital_key* is the system_config row holding this scope's capital baseline:
    ``paper_capital`` for the paper scope, ``live_capital`` for the live one (Q1).
    """

    def __init__(
        self, pool: Any, scope: str = "paper", *, capital_key: str = "paper_capital"
    ) -> None:
        self._pool = pool
        self._scope = scope
        self._capital_key = capital_key

    async def load_config(self) -> LoadedConfig:
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM risk_config WHERE scope = 'global'")
                capital = await conn.fetchrow(
                    "SELECT value FROM system_config WHERE key = $1", self._capital_key
                )
        except Exception as exc:
            raise RiskStateUnavailableError(f"reading risk config failed: {exc}") from exc
        if row is None:
            raise RiskStateUnavailableError("no risk_config row for scope 'global'")
        if capital is None:
            raise RiskStateUnavailableError(f"no system_config row for '{self._capital_key}'")
        row = dict(row)
        return LoadedConfig(
            config=risk_config_from_row(row),
            kill_switch=bool(row["kill_switch_active"]),
            paper_capital=parse_paper_capital(capital["value"]),
        )

    async def load_state(self) -> RiskState:
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT equity, equity_at, peak_portfolio_value, current_drawdown, "
                    "daily_pnl_date, day_start_equity, consecutive_losses, last_loss_at, "
                    "carried_pnl, paper_capital, cb_tier, cb_tier_since, cb_requires_review, "
                    "daily_halt_until, consecutive_pause_until FROM risk_state WHERE scope = $1",
                    self._scope,
                )
        except Exception as exc:
            raise RiskStateUnavailableError(f"reading risk_state failed: {exc}") from exc
        if row is None:
            raise RiskStateUnavailableError(f"no risk_state row for scope {self._scope!r}")
        peak = _dec(row["peak_portfolio_value"])
        day_start = _dec(row["day_start_equity"])
        return RiskState(
            equity=_dec(row["equity"]),
            equity_at=row["equity_at"],
            peak_equity=peak if peak is not None and peak > 0 else None,
            current_drawdown=_dec(row["current_drawdown"], Decimal("0")) or Decimal("0"),
            day=row["daily_pnl_date"] if day_start is not None else None,
            day_start_equity=day_start,
            consecutive_losses=int(row["consecutive_losses"] or 0),
            last_loss_at=row["last_loss_at"],
            carried_pnl=_dec(row["carried_pnl"], Decimal("0")) or Decimal("0"),
            paper_capital=_dec(row["paper_capital"]),
            cb=CircuitBreakerState.from_row(row),
        )

    async def save_state(self, state: RiskState) -> None:
        cb = state.cb.to_row()
        daily_pnl = (
            state.equity - state.day_start_equity
            if state.equity is not None and state.day_start_equity is not None
            else Decimal("0")
        )
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE risk_state SET equity = $2, equity_at = $3, "
                    "peak_portfolio_value = $4, current_drawdown = $5, daily_pnl_date = $6, "
                    "day_start_equity = $7, daily_pnl = $8, consecutive_losses = $9, "
                    "last_loss_at = $10, carried_pnl = $11, cb_tier = $12, cb_tier_since = $13, "
                    "cb_requires_review = $14, daily_halt_until = $15, "
                    "consecutive_pause_until = $16, paper_capital = $17, updated_at = now() "
                    "WHERE scope = $1",
                    self._scope,
                    state.equity,
                    state.equity_at,
                    state.peak_equity or Decimal("0"),
                    min(state.current_drawdown, Decimal("9999")),
                    state.day or datetime.now(UTC).date(),
                    state.day_start_equity,
                    daily_pnl,
                    state.consecutive_losses,
                    state.last_loss_at,
                    state.carried_pnl,
                    cb["cb_tier"],
                    cb["cb_tier_since"],
                    cb["cb_requires_review"],
                    cb["daily_halt_until"],
                    cb["consecutive_pause_until"],
                    state.paper_capital,
                )
        except Exception as exc:
            raise RiskStateUnavailableError(f"writing risk_state failed: {exc}") from exc

    async def set_kill_switch(self, active: bool) -> None:
        try:
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    "UPDATE risk_config SET kill_switch_active = $1, updated_at = now() "
                    "WHERE scope = 'global'",
                    active,
                )
        except Exception as exc:
            raise RiskStateUnavailableError(f"writing kill switch failed: {exc}") from exc
        if str(result).strip() == "UPDATE 0":
            raise RiskStateUnavailableError("no risk_config row for scope 'global'")

    async def append_event(self, event: RiskEventRecord) -> None:
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO risk_events (scope, kind, tier, actor, reason, details) "
                    "VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
                    self._scope,
                    event.kind,
                    event.tier,
                    event.actor,
                    event.reason,
                    json.dumps(event.details, default=str),
                )
        except Exception as exc:
            raise RiskStateUnavailableError(f"writing risk_events failed: {exc}") from exc

    async def recent_events(self, limit: int) -> list[RiskEventRecord]:
        return await fetch_risk_events(self._pool, limit, scope=self._scope)


async def fetch_risk_events(
    pool: Any, limit: int, scope: str = "paper", actor: str | None = None
) -> list[RiskEventRecord]:
    """Newest first (``ts DESC, id DESC``); ``actor`` filters in SQL, before the LIMIT.

    Callers that filter by actor must pass it here, never post-filter a limited read:
    the newest 500 overall may hold none of the actor's rows.
    """
    query = (
        "SELECT id, ts, scope, kind, tier, actor, reason, details FROM risk_events WHERE scope = $1"
    )
    params: list[Any] = [scope]
    if actor is not None:
        params.append(actor)
        query += f" AND actor = ${len(params)}"
    params.append(limit)
    query += f" ORDER BY ts DESC, id DESC LIMIT ${len(params)}"
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)
    out: list[RiskEventRecord] = []
    for r in rows:
        details = r["details"]
        out.append(
            RiskEventRecord(
                id=int(r["id"]),
                ts=r["ts"],
                scope=str(r["scope"]),
                kind=str(r["kind"]),
                tier=None if r["tier"] is None else int(r["tier"]),
                actor=str(r["actor"]),
                reason=str(r["reason"]),
                details=json.loads(details) if isinstance(details, str) else dict(details or {}),
            )
        )
    return out


class InMemoryRiskStateStore:
    """Process-local store: unit tests and a SessionManager without a database."""

    def __init__(
        self,
        *,
        paper_capital: Decimal,
        config: RiskConfig | None = None,
        kill_switch: bool = False,
        state: RiskState | None = None,
    ) -> None:
        self.config = config or RiskConfig()
        self.paper_capital = paper_capital
        self.kill_switch = kill_switch
        self.state = state or RiskState()
        self.events: list[RiskEventRecord] = []
        self.saves = 0

    async def load_config(self) -> LoadedConfig:
        return LoadedConfig(self.config, self.kill_switch, self.paper_capital)

    async def load_state(self) -> RiskState:
        return _copy_state(self.state)

    async def save_state(self, state: RiskState) -> None:
        self.state = _copy_state(state)
        self.saves += 1

    async def set_kill_switch(self, active: bool) -> None:
        self.kill_switch = active

    async def append_event(self, event: RiskEventRecord) -> None:
        self.events.append(event)

    async def recent_events(self, limit: int) -> list[RiskEventRecord]:
        return list(reversed(self.events))[:limit]


def _copy_state(state: RiskState) -> RiskState:
    return RiskState(
        equity=state.equity,
        equity_at=state.equity_at,
        peak_equity=state.peak_equity,
        current_drawdown=state.current_drawdown,
        day=state.day,
        day_start_equity=state.day_start_equity,
        consecutive_losses=state.consecutive_losses,
        last_loss_at=state.last_loss_at,
        carried_pnl=state.carried_pnl,
        paper_capital=state.paper_capital,
        cb=CircuitBreakerState(
            tier=state.cb.tier,
            tier_since=state.cb.tier_since,
            requires_manual_review=state.cb.requires_manual_review,
            daily_halt_until=state.cb.daily_halt_until,
            consecutive_pause_until=state.cb.consecutive_pause_until,
        ),
    )
