from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

router = APIRouter(prefix="/api/risk", tags=["risk"])


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
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/status", response_model=RiskStatus)
async def get_risk_status() -> dict[str, Any]:
    """Current risk status: circuit breaker tiers, drawdown, daily loss."""
    return {
        "current_drawdown": 4.2,
        "max_drawdown_limit": 15.0,
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
