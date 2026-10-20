from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from hydra.dashboard.routes._db import (
    DB_ERRORS,
    api_error,
    db_errors_to_503,
    require_pool,
)
from hydra.risk.circuit_breakers import CircuitBreakerRestrictions, TierNotActiveError
from hydra.risk.service import RiskService
from hydra.risk.state import (
    RiskStateNotPersistedError,
    RiskStateUnavailableError,
    fetch_risk_events,
)

router = APIRouter(prefix="/api/risk", tags=["risk"])
logger = logging.getLogger(__name__)

_STORE_ERRORS: tuple[type[BaseException], ...] = (RiskStateUnavailableError, *DB_ERRORS)


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


class CircuitBreaker(BaseModel):
    tier: int
    label: str
    threshold: str
    threshold_pct: float
    current_value: float  # account drawdown, percent
    status: Literal["Normal", "Warning", "Tripped"]
    active_since: datetime | None = None
    requires_manual_review: bool = False


class CircuitBreakerRestrictionsModel(BaseModel):
    active_tier: int
    can_open_new_trades: bool
    size_multiplier: float
    must_flatten: bool
    is_shutdown: bool
    daily_halt_until: datetime | None
    consecutive_pause_until: datetime | None


class RiskStatus(BaseModel):
    current_drawdown: float
    max_drawdown_limit: float
    daily_loss: float | None
    daily_loss_limit: float | None
    equity: float | None
    peak_equity: float | None
    day_start_equity: float | None
    consecutive_losses: int
    max_consecutive_losses: int
    circuit_breakers: list[CircuitBreaker] | None = None
    circuit_breakers_state: Literal["live", "unavailable"] = "unavailable"
    restrictions: CircuitBreakerRestrictionsModel | None = None


class VarEstimate(BaseModel):
    var_95: float
    var_99: float
    cvar_95: float
    portfolio_value: float
    calculation_method: str
    sample_days: int


class RiskConfig(BaseModel):
    scope: str = "global"
    max_position_pct: float
    max_risk_per_trade: float
    max_portfolio_heat: float
    max_daily_loss_pct: float
    max_drawdown_pct: float
    max_concurrent_positions: int
    max_consecutive_losses: int
    kill_switch_active: bool


class RiskConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_position_pct: float | None = Field(None, ge=0, le=1)
    max_risk_per_trade: float | None = Field(None, ge=0, le=1)
    max_portfolio_heat: float | None = Field(None, ge=0, le=1)
    max_daily_loss_pct: float | None = Field(None, ge=0, le=1)
    max_drawdown_pct: float | None = Field(None, gt=0, le=1)
    max_concurrent_positions: int | None = Field(None, ge=1, le=100)
    max_consecutive_losses: int | None = Field(None, ge=1, le=100)


class BreakerResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)
    confirm_tier: int


class PaperResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)
    confirm: Literal["RESET"]


class RiskEventModel(BaseModel):
    id: int | None
    ts: datetime | None
    scope: str
    kind: str
    tier: int | None
    actor: str
    reason: str
    details: dict[str, Any]


class RiskStatusLive(BaseModel):
    kill_switch_active: bool | None
    kill_switch_state: Literal["active", "inactive", "unknown"]
    running_sessions: int | None
    circuit_breaker_restrictions: CircuitBreakerRestrictionsModel | None = None
    circuit_breakers_state: Literal["live", "unavailable"] = "unavailable"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _risk_config_missing(scope: str = "global") -> HTTPException:
    return api_error(
        503,
        "risk_config_missing",
        f"No risk_config row for scope '{scope}'",
        "unavailable",
        scope=scope,
    )


def _risk_service(request: Request) -> RiskService | None:
    """The engine's RiskService, exposed as ``SessionManager.risk``."""
    mgr = getattr(request.app.state, "session_manager", None)
    svc = getattr(mgr, "risk", None) if mgr is not None else None
    return svc if isinstance(svc, RiskService) else None


def _require_risk(request: Request) -> RiskService:
    svc = _risk_service(request)
    if svc is None:
        raise api_error(
            503,
            "risk_service_unavailable",
            "The trading engine's risk service is not running",
            "unavailable",
        )
    if not svc.available:
        raise api_error(
            503,
            "risk_state_unavailable",
            "Risk state could not be loaded or persisted; opening orders are refused",
            "unavailable",
        )
    return svc


def _paper_only(source: str | None) -> None:
    if source not in (None, "paper"):
        raise api_error(
            503,
            "live_disabled",
            "Live trading is disabled; risk state exists for the paper account only",
            "unavailable",
            source=source,
        )


def _actor(request: Request) -> str:
    principal = getattr(request.state, "principal", None)
    name = getattr(principal, "name", None)
    kind = getattr(principal, "kind", None)
    if name:
        return f"{kind}:{name}" if kind else str(name)
    return "anonymous"


def _f(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _restrictions_model(
    r: CircuitBreakerRestrictions,
) -> CircuitBreakerRestrictionsModel:
    return CircuitBreakerRestrictionsModel(
        active_tier=r.tier,
        can_open_new_trades=r.can_open_new_trades,
        size_multiplier=float(r.position_size_multiplier),
        must_flatten=r.must_flatten,
        is_shutdown=r.is_shutdown,
        daily_halt_until=r.daily_halt_until,
        consecutive_pause_until=r.consecutive_pause_until,
    )


def _breakers(svc: RiskService) -> list[CircuitBreaker]:
    drawdown_pct = round(float(svc.account().drawdown) * 100, 2)
    return [
        CircuitBreaker(
            tier=b.tier,
            label=b.label,
            threshold=f"{float(b.threshold) * 100:.2f}% drawdown",
            threshold_pct=round(float(b.threshold) * 100, 4),
            current_value=drawdown_pct,
            status=b.status,
            active_since=b.active_since,
            requires_manual_review=b.requires_manual_review,
        )
        for b in svc.breakers()
    ]


def _row_to_config(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": row["scope"],
        "max_position_pct": float(row["max_position_pct"]),
        "max_risk_per_trade": float(row["max_risk_per_trade"]),
        "max_portfolio_heat": float(row["max_portfolio_heat"]),
        "max_daily_loss_pct": float(row["max_daily_loss_pct"]),
        "max_drawdown_pct": float(row["max_drawdown_pct"]),
        "max_concurrent_positions": row["max_concurrent_positions"],
        "max_consecutive_losses": row["max_consecutive_losses"],
        "kill_switch_active": row["kill_switch_active"],
    }


async def _apply_risk_config_update(pool: Any, scope: str, updates: dict[str, Any]) -> None:
    """Apply risk config updates using fixed-column UPDATE (no dynamic SQL)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM risk_config WHERE scope = $1", scope)
        if row is None:
            raise api_error(
                404,
                "risk_config_not_found",
                f"No risk_config row for scope '{scope}'",
                "not_found",
                scope=scope,
            )

        await conn.execute(
            "UPDATE risk_config SET "
            "max_position_pct = $1, "
            "max_risk_per_trade = $2, "
            "max_portfolio_heat = $3, "
            "max_daily_loss_pct = $4, "
            "max_drawdown_pct = $5, "
            "max_concurrent_positions = $6, "
            "max_consecutive_losses = $7, "
            "updated_at = now() "
            "WHERE scope = $8",
            updates.get("max_position_pct", float(row["max_position_pct"])),
            updates.get("max_risk_per_trade", float(row["max_risk_per_trade"])),
            updates.get("max_portfolio_heat", float(row["max_portfolio_heat"])),
            updates.get("max_daily_loss_pct", float(row["max_daily_loss_pct"])),
            updates.get("max_drawdown_pct", float(row["max_drawdown_pct"])),
            updates.get("max_concurrent_positions", row["max_concurrent_positions"]),
            updates.get("max_consecutive_losses", row["max_consecutive_losses"]),
            scope,
        )


async def _get_risk_config_from_db(pool: Any, scope: str = "global") -> dict[str, Any] | None:
    """Fetch a risk_config row; None only when the row is missing (DB errors propagate)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM risk_config WHERE scope = $1", scope)
    return None if row is None else dict(row)


# ---------------------------------------------------------------------------
# Status, circuit breakers, events, VaR
# ---------------------------------------------------------------------------


@router.get("/status", response_model=RiskStatus)
async def get_risk_status(request: Request, source: str | None = None) -> RiskStatus:
    """Account drawdown, daily loss and breakers from the engine's RiskService (paper ledger)."""
    _paper_only(source)
    svc = _require_risk(request)
    view = svc.account()
    cfg = view.config
    start = view.day_start_equity
    daily_loss = (
        max(Decimal("0"), start - view.equity)
        if start is not None and view.equity is not None
        else None
    )
    return RiskStatus(
        current_drawdown=round(float(view.drawdown) * 100, 2),
        max_drawdown_limit=float(cfg.max_drawdown_pct) * 100,
        daily_loss=None if daily_loss is None else round(float(daily_loss), 2),
        daily_loss_limit=None if start is None else round(float(start * cfg.max_daily_loss_pct), 2),
        equity=_f(view.equity),
        peak_equity=_f(view.peak_equity),
        day_start_equity=_f(start),
        consecutive_losses=view.consecutive_losses,
        max_consecutive_losses=cfg.max_consecutive_losses,
        circuit_breakers=_breakers(svc),
        circuit_breakers_state="live",
        restrictions=_restrictions_model(view.restrictions),
    )


@router.get("/circuit-breakers", response_model=list[CircuitBreaker])
async def get_circuit_breakers(request: Request) -> list[CircuitBreaker]:
    """The four drawdown tiers with live state from the RiskService."""
    return _breakers(_require_risk(request))


@router.post("/circuit-breakers/{tier}/reset", response_model=list[CircuitBreaker])
async def reset_circuit_breaker(
    tier: int, body: BreakerResetRequest, request: Request
) -> list[CircuitBreaker]:
    """Reset the active tier (audited first; rebases the high-water mark to current equity)."""
    if tier not in range(1, 5):
        raise api_error(422, "invalid_tier", f"tier must be 1-4, got {tier}", "invalid")
    if body.confirm_tier != tier:
        raise api_error(
            422,
            "confirm_mismatch",
            f"confirm_tier {body.confirm_tier} does not match tier {tier}",
            "invalid",
        )
    svc = _require_risk(request)
    try:
        await svc.reset_breaker(
            tier, actor=_actor(request), reason=body.reason, confirm_tier=body.confirm_tier
        )
    except TierNotActiveError as exc:
        raise api_error(
            409,
            "tier_not_active",
            str(exc),
            "invalid",
            active_tier=svc.account().tier,
        ) from exc
    except ValueError as exc:
        raise api_error(422, "invalid_reset", str(exc), "invalid") from exc
    except RiskStateNotPersistedError as exc:
        logger.exception("Circuit breaker reset not persisted; rolled back")
        raise api_error(
            503,
            "reset_not_persisted",
            "The reset could not be saved and was rolled back; the breaker is unchanged",
            "unavailable",
        ) from exc
    except _STORE_ERRORS as exc:
        logger.exception("Circuit breaker reset not audited; state unchanged")
        raise api_error(
            503,
            "audit_unavailable",
            "The reset could not be written to the audit log; nothing changed",
            "unavailable",
        ) from exc
    return _breakers(svc)


@router.post("/paper-account/reset", response_model=RiskStatus)
async def reset_paper_account(body: PaperResetRequest, request: Request) -> RiskStatus:
    """Rebase the paper ledger (peak, day start, carried PnL) to current equity; audited first.

    Breaker tiers, halts and pauses are untouched (reset those through the breaker reset).
    """
    svc = _require_risk(request)
    try:
        await svc.reset_paper_account(actor=_actor(request), reason=body.reason)
    except _STORE_ERRORS as exc:
        logger.exception("Paper account reset refused or not audited")
        raise api_error(
            503,
            "paper_reset_unavailable",
            "The paper account was not reset: risk state is recovering, a held position has no "
            "mark yet, or the audit log could not be written",
            "unavailable",
        ) from exc
    return await get_risk_status(request)


@router.get("/events", response_model=list[RiskEventModel])
async def get_risk_events(
    request: Request, limit: int = Query(50, ge=1, le=500)
) -> list[RiskEventModel]:
    """Breaker transitions, resets and kill-switch changes, newest first."""
    pool = require_pool(request)
    async with db_errors_to_503("reading risk events"):
        events = await fetch_risk_events(pool, limit)
    return [
        RiskEventModel(
            id=e.id,
            ts=e.ts,
            scope=e.scope,
            kind=e.kind,
            tier=e.tier,
            actor=e.actor,
            reason=e.reason,
            details=e.details,
        )
        for e in events
    ]


@router.get("/var", response_model=VarEstimate)
async def get_var(request: Request, source: str | None = None) -> VarEstimate:
    """Historical-simulation VaR over daily paper PnL, sized against account equity."""
    _paper_only(source)
    svc = _require_risk(request)
    equity = svc.account().equity
    if equity is None:
        raise api_error(
            503, "equity_unknown", "No account equity has been marked yet", "unavailable"
        )
    pool = require_pool(request)

    var_95 = 0.0
    var_99 = 0.0
    cvar_95 = 0.0

    async with db_errors_to_503("computing VaR"), pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT SUM(pnl - fee) AS daily_pnl FROM trades "
            "WHERE source = 'paper' "
            "GROUP BY date_trunc('day', timestamp) "
            "ORDER BY date_trunc('day', timestamp)",
        )

    if len(rows) >= 5:
        daily_pnls = sorted(float(r["daily_pnl"]) for r in rows)
        n = len(daily_pnls)
        idx_95 = max(0, int(n * 0.05))
        idx_99 = max(0, int(n * 0.01))
        var_95 = abs(min(daily_pnls[idx_95], 0))
        var_99 = abs(min(daily_pnls[idx_99], 0))
        tail = [v for v in daily_pnls[: idx_95 + 1] if v < 0]
        cvar_95 = abs(sum(tail) / len(tail)) if tail else var_95

    return VarEstimate(
        var_95=round(var_95, 2),
        var_99=round(var_99, 2),
        cvar_95=round(cvar_95, 2),
        portfolio_value=round(float(equity), 2),
        calculation_method="historical_simulation",
        sample_days=len(rows),
    )


# ---------------------------------------------------------------------------
# Risk config CRUD
# ---------------------------------------------------------------------------


def _no_fields() -> HTTPException:
    return api_error(400, "no_fields", "No fields to update", "invalid")


@router.get("/config", response_model=RiskConfig)
async def get_risk_config(request: Request) -> dict[str, Any]:
    """Get global risk limits from DB."""
    pool = require_pool(request)
    async with db_errors_to_503("reading risk config"):
        row = await _get_risk_config_from_db(pool)
    if row is None:
        raise _risk_config_missing()
    return _row_to_config(row)


@router.put("/config", response_model=RiskConfig)
async def update_risk_config(body: RiskConfigUpdate, request: Request) -> dict[str, Any]:
    """Update global risk limits; the running engine reloads them."""
    pool = require_pool(request)
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise _no_fields()

    async with db_errors_to_503("updating risk config"):
        await _apply_risk_config_update(pool, "global", updates)

    mgr = getattr(request.app.state, "session_manager", None)
    if mgr is not None:
        await mgr.reload_risk_config()
    svc = _risk_service(request)
    if svc is not None:
        try:
            await svc.reload_config(actor=_actor(request))
        except _STORE_ERRORS as exc:
            logger.exception("Risk config saved but the engine did not reload it")
            raise api_error(
                503,
                "risk_reload_failed",
                "Limits were saved but the running engine could not reload them",
                "unavailable",
            ) from exc

    return await get_risk_config(request)


@router.get("/config/{strategy_id}", response_model=RiskConfig)
async def get_strategy_risk_config(strategy_id: str, request: Request) -> dict[str, Any]:
    """Per-strategy overrides are not enforced in P1; this returns the global limits that are."""
    return await get_risk_config(request)


@router.put("/config/{strategy_id}", status_code=501)
async def update_strategy_risk_config(strategy_id: str) -> None:
    """Per-strategy overrides are not enforced in P1, so writing them is refused."""
    raise api_error(
        501,
        "not_implemented",
        "Per-strategy risk overrides are not enforced yet; only global limits apply",
        "not_implemented",
        strategy_id=strategy_id,
    )


@router.get("/live-status", response_model=RiskStatusLive)
async def get_live_risk_status(request: Request) -> RiskStatusLive:
    """Kill switch, sessions and breaker restrictions; unknown values are reported as unknown."""
    kill_switch: bool | None = None
    pool = getattr(request.app.state, "db_pool", None)
    if pool is not None:
        try:
            cfg = await _get_risk_config_from_db(pool)
        except DB_ERRORS:
            logger.warning("Kill switch state unknown: risk_config read failed", exc_info=True)
            cfg = None
        if cfg is not None:
            kill_switch = bool(cfg["kill_switch_active"])

    svc = _risk_service(request)
    if svc is not None and svc.kill_switch_active():
        kill_switch = True

    kill_state: Literal["active", "inactive", "unknown"] = (
        "unknown" if kill_switch is None else ("active" if kill_switch else "inactive")
    )

    mgr = getattr(request.app.state, "session_manager", None)
    running: int | None = None
    if mgr is not None:
        running = sum(1 for s in mgr.list_sessions() if s.status == "running")

    live = svc is not None and svc.available
    return RiskStatusLive(
        kill_switch_active=kill_switch,
        kill_switch_state=kill_state,
        running_sessions=running,
        circuit_breaker_restrictions=_restrictions_model(svc.restrictions())
        if svc is not None and live
        else None,
        circuit_breakers_state="live" if live else "unavailable",
    )
