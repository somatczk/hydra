from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/system", tags=["system"])


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


class ExchangeStatus(BaseModel):
    id: str
    name: str
    connected: bool
    api_key_set: bool
    last_sync: str


class PlatformConfig(BaseModel):
    trading_mode: str = "paper"
    default_pair: str = "BTC/USDT"
    default_timeframe: str = "1h"
    max_concurrent_strategies: int = 5


class PlatformConfigUpdate(BaseModel):
    trading_mode: str | None = None
    default_pair: str | None = None
    default_timeframe: str | None = None
    max_concurrent_strategies: int | None = None


class ServiceHealth(BaseModel):
    service: str
    status: str  # healthy | degraded | down
    latency_ms: float | None = None


class SystemHealth(BaseModel):
    overall: str  # healthy | degraded | down
    services: list[ServiceHealth]


# ---------------------------------------------------------------------------
# Placeholder data
# ---------------------------------------------------------------------------

_CONFIG = PlatformConfig()

_EXCHANGES: list[dict[str, Any]] = [
    {
        "id": "binance",
        "name": "Binance",
        "connected": True,
        "api_key_set": True,
        "last_sync": "2 min ago",
    },
    {
        "id": "bybit",
        "name": "Bybit",
        "connected": True,
        "api_key_set": True,
        "last_sync": "5 min ago",
    },
    {
        "id": "kraken",
        "name": "Kraken",
        "connected": False,
        "api_key_set": False,
        "last_sync": "Never",
    },
    {
        "id": "okx",
        "name": "OKX",
        "connected": False,
        "api_key_set": True,
        "last_sync": "3 days ago",
    },
]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/config", response_model=PlatformConfig)
async def get_config() -> PlatformConfig:
    """Return the platform configuration (safe subset)."""
    return _CONFIG


@router.put("/config", response_model=PlatformConfig)
async def update_config(body: PlatformConfigUpdate) -> PlatformConfig:
    """Update platform configuration fields."""
    global _CONFIG
    update_data = body.model_dump(exclude_none=True)
    current = _CONFIG.model_dump()
    current.update(update_data)
    _CONFIG = PlatformConfig(**current)
    return _CONFIG


@router.get("/exchanges", response_model=list[ExchangeStatus])
async def get_exchanges() -> list[dict[str, Any]]:
    """Exchange connection status per exchange."""
    return _EXCHANGES


@router.get("/health", response_model=SystemHealth)
async def get_system_health() -> dict[str, Any]:
    """Detailed health check covering DB, Redis, and exchanges."""
    services = [
        {"service": "TimescaleDB", "status": "healthy", "latency_ms": 2.1},
        {"service": "Redis", "status": "healthy", "latency_ms": 0.5},
        {"service": "Binance WS", "status": "healthy", "latency_ms": 45.0},
        {"service": "Bybit WS", "status": "healthy", "latency_ms": 52.0},
        {"service": "Kraken WS", "status": "down", "latency_ms": None},
        {"service": "OKX WS", "status": "down", "latency_ms": None},
    ]
    all_healthy = all(s["status"] == "healthy" for s in services)
    any_down = any(s["status"] == "down" for s in services)
    if all_healthy:
        overall = "healthy"
    elif any_down:
        overall = "degraded"
    else:
        overall = "degraded"
    return {"overall": overall, "services": services}
