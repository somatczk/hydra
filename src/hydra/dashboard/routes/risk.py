from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from hydra.dashboard.routes._db import (
    DB_ERRORS,
    ServiceErrorRoute,
    api_error,
    db_errors_to_503,
    request_pool,
    request_trader,
    require_pool,
)
from hydra.risk.state import RiskStateUnavailableError, fetch_risk_events
from hydra.services.live_gate import read_live_state

router = APIRouter(prefix="/api/risk", tags=["risk"], route_class=ServiceErrorRoute)
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
    # additive (A2): the live-scope numbers, served when source=live
    equity: float | None = None
    current_drawdown: float | None = None
    active_tier: int | None = None
    # additive (R9): the armed gate; fail-safe false when the row is unreadable
    live_armed: bool = False


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


def _paper_only(source: str | None) -> None:
    """This read exists for the paper account only; live has its own scope reads."""
    if source not in (None, "paper"):
        raise api_error(
            503,
            "live_disabled",
            "This read exists for the paper account only; the live-scope risk reads are "
            "GET /api/risk/status?source=live and /api/risk/live-status?source=live",
            "unavailable",
            source=source,
        )


def _validate_source(source: str | None) -> None:
    """The source vocabulary is paper, live, or unset; anything else fails closed (R29 A6)."""
    if source not in (None, "paper", "live"):
        raise api_error(
            422,
            "invalid_source",
            f"source must be 'paper' or 'live', got {source!r}",
            "invalid",
            source=source,
        )


def _actor(request: Request) -> str:
    principal = getattr(request.state, "principal", None)
    name = getattr(principal, "name", None)
    kind = getattr(principal, "kind", None)
    if name:
        return f"{kind}:{name}" if kind else str(name)
    return "anonymous"


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
async def get_risk_status(request: Request, source: str | None = None) -> dict[str, Any]:
    """Account drawdown, daily loss and breakers from the engine's RiskService.

    ``source=live`` reads the live-scoped RiskService (Q1) through the same
    ``live_status`` op (R11); no live service built is 503 ``live_risk_unavailable``.
    An unrecognized source is refused 422, never silently served as paper (A6≡S8).
    """
    _validate_source(source)
    if source == "live":
        return await request_trader(request).live_status(source="live")
    return await request_trader(request).risk_status()


@router.get("/circuit-breakers", response_model=list[CircuitBreaker])
async def get_circuit_breakers(request: Request) -> list[dict[str, Any]]:
    """The four drawdown tiers with live state from the RiskService."""
    return await request_trader(request).circuit_breakers()


@router.post("/circuit-breakers/{tier}/reset", response_model=list[CircuitBreaker])
async def reset_circuit_breaker(
    tier: int, body: BreakerResetRequest, request: Request
) -> list[dict[str, Any]]:
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
    return await request_trader(request).reset_breaker(
        tier, _actor(request), body.reason, body.confirm_tier
    )


@router.post("/paper-account/reset", response_model=RiskStatus)
async def reset_paper_account(body: PaperResetRequest, request: Request) -> dict[str, Any]:
    """Rebase the paper ledger (peak, day start, carried PnL) to current equity; audited first.

    Breaker tiers, halts and pauses are untouched (reset those through the breaker reset).
    """
    await request_trader(request).reset_paper_account(_actor(request), body.reason)
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
async def get_var(request: Request, source: str | None = None) -> dict[str, Any]:
    """Historical-simulation VaR over daily paper PnL, sized against account equity."""
    _paper_only(source)
    return await request_trader(request).var_estimate()


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

    try:
        await request_trader(request).reload_paper_capital(_actor(request))
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
async def get_live_risk_status(request: Request, source: str | None = None) -> dict[str, Any]:
    """Kill switch, sessions and breaker restrictions; unknown values are reported as unknown.

    ``source=live`` answers the same picture on the live scope plus its account numbers
    (equity, current drawdown, active tier); 503 ``live_risk_unavailable`` when no live
    service is built. An unrecognized source is refused 422, never silently served as
    paper (A6≡S8). ``live_armed`` is the master gate's row, merged here (R9): it reads
    disarmed when the row is absent or unreadable (fail-safe).
    """
    _validate_source(source)
    body = await request_trader(request).live_status(source=source or "paper")
    state = await read_live_state(request_pool(request))
    return {**body, "live_armed": state["armed"]}
