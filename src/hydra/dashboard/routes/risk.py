from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

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
    circuit_breakers: list[CircuitBreaker]


class CircuitBreakerResetResponse(BaseModel):
    tier: int
    status: str
    message: str


class VarEstimate(BaseModel):
    var_95: float
    var_99: float
    cvar_95: float
    portfolio_value: float
    calculation_method: str


class RiskConfig(BaseModel):
    scope: str = "global"
    max_position_pct: float = 0.10
    max_risk_per_trade: float = 0.02
    max_portfolio_heat: float = 0.06
    max_daily_loss_pct: float = 0.03
    max_drawdown_pct: float = 0.15
    max_concurrent_positions: int = 10
    kill_switch_active: bool = False
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
    kill_switch_active: bool
    running_sessions: int
    circuit_breaker_restrictions: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Placeholder data
# ---------------------------------------------------------------------------

_CIRCUIT_BREAKERS: list[dict[str, Any]] = [
    {
        "tier": 1,
        "label": "Position Level",
        "threshold": "2% per position",
        "current_value": 0.8,
        "status": "Normal",
    },
    {
        "tier": 2,
        "label": "Strategy Level",
        "threshold": "5% daily loss per strategy",
        "current_value": 1.2,
        "status": "Normal",
    },
    {
        "tier": 3,
        "label": "Portfolio Level",
        "threshold": "10% daily portfolio loss",
        "current_value": 3.8,
        "status": "Warning",
    },
    {
        "tier": 4,
        "label": "System Kill Switch",
        "threshold": "15% daily loss - halt all trading",
        "current_value": 3.8,
        "status": "Normal",
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pool_from_request(request: Request) -> Any:
    return getattr(request.app.state, "db_pool", None)


async def _apply_risk_config_update(pool: Any, scope: str, updates: dict[str, Any]) -> None:
    """Apply risk config updates using fixed-column UPDATE (no dynamic SQL)."""
    async with pool.acquire() as conn:
        # Fetch current values
        row = await conn.fetchrow("SELECT * FROM risk_config WHERE scope = $1", scope)
        if row is None:
            return

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
    """Fetch risk config row from DB."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM risk_config WHERE scope = $1", scope)
            if row is None:
                return None
            return dict(row)
    except Exception:
        logger.debug("Failed to fetch risk_config for scope=%s", scope)
        return None


# ---------------------------------------------------------------------------
# Existing endpoints (unchanged behaviour, now enriched from DB)
# ---------------------------------------------------------------------------


@router.get("/status", response_model=RiskStatus)
async def get_risk_status(request: Request, source: str | None = None) -> dict[str, Any]:
    """Current risk status: circuit breaker tiers, drawdown, daily loss."""
    pool = _pool_from_request(request)
    drawdown_limit = 15.0
    daily_loss_limit = 500.0
    current_drawdown = 0.0
    daily_loss = 0.0

    effective_source = source or "paper"

    if pool is not None:
        cfg = await _get_risk_config_from_db(pool)
        if cfg is not None:
            drawdown_limit = float(cfg.get("max_drawdown_pct", 0.15)) * 100
            daily_loss_limit_pct = float(cfg.get("max_daily_loss_pct", 0.03))

        try:
            async with pool.acquire() as conn:
                # Get base capital for limit calculation
                from hydra.dashboard.routes.system import get_paper_capital

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

                # Daily loss from running sessions
                daily_loss_val = await conn.fetchval(
                    "SELECT COALESCE(SUM(pnl), 0) FROM trades "
                    "WHERE source = $1 AND timestamp >= date_trunc('day', now()) "
                    "AND session_id IN "
                    "(SELECT id FROM trading_sessions WHERE status = 'running')",
                    effective_source,
                )
                daily_loss = abs(float(daily_loss_val)) if float(daily_loss_val) < 0 else 0.0

                # Current drawdown from equity curve peak
                equity_rows = await conn.fetch(
                    "SELECT total_value FROM balance_snapshots "
                    "WHERE source = $1 ORDER BY timestamp",
                    effective_source,
                )
                peak = base_capital
                latest = base_capital
                for row in equity_rows:
                    val = float(row["total_value"])
                    if val > peak:
                        peak = val
                    latest = val
                if peak > 0:
                    current_drawdown = round((peak - latest) / peak * 100, 2)
        except Exception:
            logger.exception("Failed to compute risk status from DB")

    return {
        "current_drawdown": current_drawdown,
        "max_drawdown_limit": drawdown_limit,
        "daily_loss": round(daily_loss, 2),
        "daily_loss_limit": round(daily_loss_limit, 2),
        "circuit_breakers": _CIRCUIT_BREAKERS,
    }


@router.get("/circuit-breakers", response_model=list[CircuitBreaker])
async def get_circuit_breakers() -> list[dict[str, Any]]:
    """All 4 circuit breaker tiers with current state."""
    return _CIRCUIT_BREAKERS


@router.post(
    "/circuit-breakers/{tier}/reset",
    response_model=CircuitBreakerResetResponse,
)
async def reset_circuit_breaker(tier: int) -> dict[str, Any]:
    """Manually reset a circuit breaker tier."""
    for cb in _CIRCUIT_BREAKERS:
        if cb["tier"] == tier:
            cb["status"] = "Normal"
            cb["current_value"] = 0.0
            return {
                "tier": tier,
                "status": "Normal",
                "message": f"Tier {tier} circuit breaker reset successfully",
            }
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Circuit breaker tier {tier} not found",
    )


@router.get("/var", response_model=VarEstimate)
async def get_var(request: Request, source: str | None = None) -> VarEstimate:
    """Value at Risk estimate computed from daily PnL history."""
    from hydra.dashboard.routes.system import get_paper_capital

    effective_source = source or "paper"
    portfolio_value = get_paper_capital(request) if effective_source == "paper" else 10000.0

    var_95 = 0.0
    var_99 = 0.0
    cvar_95 = 0.0
    method = "historical_simulation"

    pool = getattr(request.app.state, "db_pool", None)
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                if effective_source != "paper":
                    snapshot = await conn.fetchrow(
                        "SELECT total_value FROM balance_snapshots "
                        "WHERE source = $1 ORDER BY timestamp DESC LIMIT 1",
                        effective_source,
                    )
                    if snapshot:
                        portfolio_value = float(snapshot["total_value"])

                # Current portfolio value = capital + running session PnL
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

                # Daily PnL series for VaR calculation
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
                    # VaR = loss at percentile (negative values are losses)
                    idx_95 = max(0, int(n * 0.05))
                    idx_99 = max(0, int(n * 0.01))
                    var_95 = abs(min(daily_pnls[idx_95], 0))
                    var_99 = abs(min(daily_pnls[idx_99], 0))
                    # CVaR = average of losses beyond VaR
                    tail = [v for v in daily_pnls[: idx_95 + 1] if v < 0]
                    cvar_95 = abs(sum(tail) / len(tail)) if tail else var_95
        except Exception:
            logger.exception("Failed to compute VaR from DB")

    return VarEstimate(
        var_95=round(var_95, 2),
        var_99=round(var_99, 2),
        cvar_95=round(cvar_95, 2),
        portfolio_value=round(portfolio_value, 2),
        calculation_method=method,
    )


# ---------------------------------------------------------------------------
# Risk config CRUD
# ---------------------------------------------------------------------------


@router.get("/config", response_model=RiskConfig)
async def get_risk_config(request: Request) -> dict[str, Any]:
    """Get global risk limits from DB."""
    pool = _pool_from_request(request)
    if pool is not None:
        row = await _get_risk_config_from_db(pool)
        if row is not None:
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

    # Fallback
    return RiskConfig().model_dump()


@router.put("/config", response_model=RiskConfig)
async def update_risk_config(body: RiskConfigUpdate, request: Request) -> dict[str, Any]:
    """Update global risk limits."""
    pool = _pool_from_request(request)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database not available",
        )

    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update",
        )

    await _apply_risk_config_update(pool, "global", updates)

    # Hot-reload risk config in running session manager
    mgr = getattr(request.app.state, "session_manager", None)
    if mgr is not None:
        await mgr.reload_risk_config()

    return await get_risk_config(request)


@router.get("/config/{strategy_id}", response_model=RiskConfig)
async def get_strategy_risk_config(strategy_id: str, request: Request) -> dict[str, Any]:
    """Get per-strategy risk overrides (falls back to global config)."""
    pool = _pool_from_request(request)
    if pool is not None:
        row = await _get_risk_config_from_db(pool, scope=strategy_id)
        if row is not None:
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

    # Fall back to global config
    return await get_risk_config(request)


@router.put("/config/{strategy_id}", response_model=RiskConfig)
async def update_strategy_risk_config(
    strategy_id: str, body: RiskConfigUpdate, request: Request
) -> dict[str, Any]:
    """Create or update per-strategy risk overrides."""
    pool = _pool_from_request(request)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database not available",
        )

    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update",
        )

    # Ensure the strategy-specific row exists
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO risk_config (scope) VALUES ($1) ON CONFLICT (scope) DO NOTHING",
            strategy_id,
        )

    await _apply_risk_config_update(pool, strategy_id, updates)

    row = await _get_risk_config_from_db(pool, scope=strategy_id)
    if row is None:
        raise HTTPException(status_code=500, detail="Failed to read back config")

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


@router.get("/live-status", response_model=RiskStatusLive)
async def get_live_risk_status(request: Request) -> dict[str, Any]:
    """Live circuit breaker state from SessionManager."""
    pool = _pool_from_request(request)
    kill_switch = False
    if pool is not None:
        cfg = await _get_risk_config_from_db(pool)
        if cfg is not None:
            kill_switch = cfg.get("kill_switch_active", False)

    mgr = getattr(request.app.state, "session_manager", None)
    running = 0
    if mgr is not None:
        running = sum(1 for s in mgr.list_sessions() if s.status == "running")

    return {
        "kill_switch_active": kill_switch,
        "running_sessions": running,
        "circuit_breaker_restrictions": None,
    }
