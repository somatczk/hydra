from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from hydra.dashboard.routes._db import (
    DB_ERRORS,
    api_error,
    db_errors_to_503,
    require_pool,
)

router = APIRouter(prefix="/api/risk", tags=["risk"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


class CircuitBreaker(BaseModel):
    tier: int
    label: str
    threshold: str
    current_value: float
    status: str  # Normal | Warning | Alert | Tripped


class RiskStatus(BaseModel):
    current_drawdown: float
    max_drawdown_limit: float
    daily_loss: float
    daily_loss_limit: float
    circuit_breakers: list[CircuitBreaker] | None = None
    circuit_breakers_state: Literal["unavailable"] = "unavailable"


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
    kill_switch_active: bool
    leverage: int = 1
    margin_mode: str = "isolated"  # "isolated" | "cross"


class RiskConfigUpdate(BaseModel):
    max_position_pct: float | None = Field(None, ge=0, le=1)
    max_risk_per_trade: float | None = Field(None, ge=0, le=1)
    max_portfolio_heat: float | None = Field(None, ge=0, le=1)
    max_daily_loss_pct: float | None = Field(None, ge=0, le=1)
    max_drawdown_pct: float | None = Field(None, ge=0, le=1)
    max_concurrent_positions: int | None = Field(None, ge=1, le=100)
    leverage: int | None = Field(None, ge=1, le=125)
    margin_mode: str | None = None


class RiskStatusLive(BaseModel):
    kill_switch_active: bool | None
    kill_switch_state: Literal["active", "inactive", "unknown"]
    running_sessions: int | None
    circuit_breaker_restrictions: None = None
    circuit_breakers_state: Literal["unavailable"] = "unavailable"


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


def _row_to_config(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": row["scope"],
        "max_position_pct": float(row["max_position_pct"]),
        "max_risk_per_trade": float(row["max_risk_per_trade"]),
        "max_portfolio_heat": float(row.get("max_portfolio_heat", 0.06)),
        "max_daily_loss_pct": float(row["max_daily_loss_pct"]),
        "max_drawdown_pct": float(row["max_drawdown_pct"]),
        "max_concurrent_positions": row["max_concurrent_positions"],
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
            "updated_at = now() "
            "WHERE scope = $7",
            updates.get("max_position_pct", float(row["max_position_pct"])),
            updates.get("max_risk_per_trade", float(row["max_risk_per_trade"])),
            updates.get("max_portfolio_heat", float(row.get("max_portfolio_heat", 0.06))),
            updates.get("max_daily_loss_pct", float(row["max_daily_loss_pct"])),
            updates.get("max_drawdown_pct", float(row["max_drawdown_pct"])),
            updates.get("max_concurrent_positions", row["max_concurrent_positions"]),
            scope,
        )


async def _get_risk_config_from_db(pool: Any, scope: str = "global") -> dict[str, Any] | None:
    """Fetch a risk_config row; None only when the row is missing (DB errors propagate)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM risk_config WHERE scope = $1", scope)
    return None if row is None else dict(row)


# ---------------------------------------------------------------------------
# Status, circuit breakers, VaR
# ---------------------------------------------------------------------------


@router.get("/status", response_model=RiskStatus)
async def get_risk_status(request: Request, source: str | None = None) -> dict[str, Any]:
    """Drawdown and daily loss against the configured limits.

    Circuit breakers are reported as unavailable until the engine's
    CircuitBreakerManager is exposed to the dashboard.
    """
    from hydra.dashboard.routes.system import get_paper_capital

    pool = require_pool(request)
    effective_source = source or "paper"

    async with db_errors_to_503("computing risk status"):
        cfg = await _get_risk_config_from_db(pool)
        if cfg is None:
            raise _risk_config_missing()
        drawdown_limit = float(cfg["max_drawdown_pct"]) * 100
        daily_loss_limit_pct = float(cfg["max_daily_loss_pct"])

        async with pool.acquire() as conn:
            if effective_source == "paper":
                base_capital = get_paper_capital(request)
            else:
                snapshot = await conn.fetchrow(
                    "SELECT total_value FROM balance_snapshots "
                    "WHERE source = $1 ORDER BY timestamp DESC LIMIT 1",
                    effective_source,
                )
                base_capital = float(snapshot["total_value"]) if snapshot else 10000.0
            daily_loss_limit = base_capital * daily_loss_limit_pct

            daily_loss_val = float(
                await conn.fetchval(
                    "SELECT COALESCE(SUM(pnl), 0) FROM trades "
                    "WHERE source = $1 AND timestamp >= date_trunc('day', now()) "
                    "AND session_id IN "
                    "(SELECT id FROM trading_sessions WHERE status = 'running')",
                    effective_source,
                )
            )
            daily_loss = abs(daily_loss_val) if daily_loss_val < 0 else 0.0

            equity_rows = await conn.fetch(
                "SELECT total_value FROM balance_snapshots WHERE source = $1 ORDER BY timestamp",
                effective_source,
            )

    peak = base_capital
    latest = base_capital
    for row in equity_rows:
        val = float(row["total_value"])
        if val > peak:
            peak = val
        latest = val
    current_drawdown = round((peak - latest) / peak * 100, 2) if peak > 0 else 0.0

    return {
        "current_drawdown": current_drawdown,
        "max_drawdown_limit": drawdown_limit,
        "daily_loss": round(daily_loss, 2),
        "daily_loss_limit": round(daily_loss_limit, 2),
        "circuit_breakers": None,
        "circuit_breakers_state": "unavailable",
    }


@router.get("/circuit-breakers", response_model=list[CircuitBreaker])
async def get_circuit_breakers() -> list[dict[str, Any]]:
    """Circuit breaker tiers; not wired to the engine yet, so always 503."""
    raise api_error(
        503,
        "circuit_breaker_manager_not_wired",
        "Circuit breaker state is not wired to the trading engine yet",
        "unavailable",
    )


@router.post("/circuit-breakers/{tier}/reset", status_code=501)
async def reset_circuit_breaker(tier: int) -> None:
    """Manual circuit breaker reset; not implemented until the engine is wired."""
    raise api_error(
        501,
        "not_implemented",
        f"Resetting circuit breaker tier {tier} is not implemented",
        "not_implemented",
        tier=tier,
    )


@router.get("/var", response_model=VarEstimate)
async def get_var(request: Request, source: str | None = None) -> VarEstimate:
    """Value at Risk estimate computed from daily PnL history."""
    from hydra.dashboard.routes.system import get_paper_capital

    pool = require_pool(request)
    effective_source = source or "paper"
    portfolio_value = get_paper_capital(request) if effective_source == "paper" else 10000.0

    var_95 = 0.0
    var_99 = 0.0
    cvar_95 = 0.0

    async with db_errors_to_503("computing VaR"), pool.acquire() as conn:
        if effective_source != "paper":
            snapshot = await conn.fetchrow(
                "SELECT total_value FROM balance_snapshots "
                "WHERE source = $1 ORDER BY timestamp DESC LIMIT 1",
                effective_source,
            )
            if snapshot:
                portfolio_value = float(snapshot["total_value"])

        pnl = await conn.fetchval(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades "
            "WHERE source = $1 AND session_id IN "
            "(SELECT id FROM trading_sessions WHERE status = 'running')",
            effective_source,
        )
        fees = await conn.fetchval(
            "SELECT COALESCE(SUM(fee), 0) FROM trades "
            "WHERE source = $1 AND session_id IN "
            "(SELECT id FROM trading_sessions WHERE status = 'running')",
            effective_source,
        )
        portfolio_value += float(pnl) - float(fees)

        rows = await conn.fetch(
            "SELECT SUM(pnl) AS daily_pnl FROM trades "
            "WHERE source = $1 "
            "GROUP BY date_trunc('day', timestamp) "
            "ORDER BY date_trunc('day', timestamp)",
            effective_source,
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
        portfolio_value=round(portfolio_value, 2),
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
    """Update global risk limits."""
    pool = require_pool(request)
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise _no_fields()

    async with db_errors_to_503("updating risk config"):
        await _apply_risk_config_update(pool, "global", updates)

    mgr = getattr(request.app.state, "session_manager", None)
    if mgr is not None:
        await mgr.reload_risk_config()

    return await get_risk_config(request)


@router.get("/config/{strategy_id}", response_model=RiskConfig)
async def get_strategy_risk_config(strategy_id: str, request: Request) -> dict[str, Any]:
    """Get per-strategy risk overrides (falls back to global config)."""
    pool = require_pool(request)
    async with db_errors_to_503("reading strategy risk config"):
        row = await _get_risk_config_from_db(pool, scope=strategy_id)
    if row is not None:
        return _row_to_config(row)
    return await get_risk_config(request)


@router.put("/config/{strategy_id}", response_model=RiskConfig)
async def update_strategy_risk_config(
    strategy_id: str, body: RiskConfigUpdate, request: Request
) -> dict[str, Any]:
    """Create or update per-strategy risk overrides."""
    pool = require_pool(request)
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise _no_fields()

    async with db_errors_to_503("updating strategy risk config"):
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO risk_config (scope) VALUES ($1) ON CONFLICT (scope) DO NOTHING",
                strategy_id,
            )
        await _apply_risk_config_update(pool, strategy_id, updates)
        row = await _get_risk_config_from_db(pool, scope=strategy_id)

    if row is None:
        raise _risk_config_missing(strategy_id)
    return _row_to_config(row)


@router.get("/live-status", response_model=RiskStatusLive)
async def get_live_risk_status(request: Request) -> dict[str, Any]:
    """Kill switch and session status; unknown values are reported as unknown."""
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

    kill_state = "unknown" if kill_switch is None else ("active" if kill_switch else "inactive")

    mgr = getattr(request.app.state, "session_manager", None)
    running: int | None = None
    if mgr is not None:
        running = sum(1 for s in mgr.list_sessions() if s.status == "running")

    return {
        "kill_switch_active": kill_switch,
        "kill_switch_state": kill_state,
        "running_sessions": running,
        "circuit_breaker_restrictions": None,
        "circuit_breakers_state": "unavailable",
    }
