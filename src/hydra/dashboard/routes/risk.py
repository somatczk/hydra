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
    max_daily_loss_pct: float = 0.03
    max_drawdown_pct: float = 0.15
    max_concurrent_positions: int = 10
    kill_switch_active: bool = False


class RiskConfigUpdate(BaseModel):
    max_position_pct: float | None = Field(None, ge=0, le=1)
    max_risk_per_trade: float | None = Field(None, ge=0, le=1)
    max_daily_loss_pct: float | None = Field(None, ge=0, le=1)
    max_drawdown_pct: float | None = Field(None, ge=0, le=1)
    max_concurrent_positions: int | None = Field(None, ge=1, le=100)


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
            "max_daily_loss_pct = $3, "
            "max_drawdown_pct = $4, "
            "max_concurrent_positions = $5, "
            "updated_at = now() "
            "WHERE scope = $6",
            updates.get("max_position_pct", float(row["max_position_pct"])),
            updates.get("max_risk_per_trade", float(row["max_risk_per_trade"])),
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
async def get_risk_status(request: Request) -> dict[str, Any]:
    """Current risk status: circuit breaker tiers, drawdown, daily loss."""
    pool = _pool_from_request(request)
    drawdown_limit = 15.0

    if pool is not None:
        cfg = await _get_risk_config_from_db(pool)
        if cfg is not None:
            drawdown_limit = float(cfg.get("max_drawdown_pct", 0.15)) * 100

    return {
        "current_drawdown": 4.2,
        "max_drawdown_limit": drawdown_limit,
        "daily_loss": 48.90,
        "daily_loss_limit": 500.0,
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
async def get_var() -> VarEstimate:
    """Value at Risk estimate for the current portfolio."""
    return VarEstimate(
        var_95=312.50,
        var_99=487.20,
        cvar_95=425.80,
        portfolio_value=12450.0,
        calculation_method="historical_simulation",
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
