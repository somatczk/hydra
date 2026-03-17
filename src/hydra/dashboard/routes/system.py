from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/system", tags=["system"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Exchange definitions: id -> (display name, env var prefix for API key)
_EXCHANGE_DEFS: list[tuple[str, str, str]] = [
    ("binance", "Binance", "BINANCE_API_KEY"),
    ("bybit", "Bybit", "BYBIT_API_KEY"),
    ("kraken", "Kraken", "KRAKEN_API_KEY"),
    ("okx", "OKX", "OKX_API_KEY"),
]


def _pool_from_request(request: Request) -> object | None:
    """Return the asyncpg connection pool from app state, or ``None``."""
    return getattr(request.app.state, "db_pool", None)


def _get_system_config(request: Request) -> dict[str, Any]:
    """Return persisted system config from app state, if any."""
    return getattr(request.app.state, "system_config", {})


def _set_system_config(request: Request, config: dict[str, Any]) -> None:
    """Persist system config to app state."""
    request.app.state.system_config = config


def _get_exchange_credentials(request: Request) -> dict[str, Any]:
    """Return exchange credentials store from app state."""
    if not hasattr(request.app.state, "exchange_credentials"):
        request.app.state.exchange_credentials = {}
    return request.app.state.exchange_credentials


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
    paper_capital: float = 10000.0


class PlatformConfigUpdate(BaseModel):
    trading_mode: str | None = None
    default_pair: str | None = None
    default_timeframe: str | None = None
    max_concurrent_strategies: int | None = None
    paper_capital: float | None = None


class ServiceHealth(BaseModel):
    service: str
    status: str  # healthy | degraded | down
    latency_ms: float | None = None


class SystemHealth(BaseModel):
    overall: str  # healthy | degraded | down
    services: list[ServiceHealth]


class ExchangeConnectRequest(BaseModel):
    api_key: str
    api_secret: str
    passphrase: str | None = None


class ExchangeConnectResponse(BaseModel):
    id: str
    name: str
    connected: bool
    message: str


# ---------------------------------------------------------------------------
# Placeholder data (fallback when DB is not available)
# ---------------------------------------------------------------------------

_EXCHANGES_PLACEHOLDER: list[dict[str, Any]] = [
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

_HEALTH_PLACEHOLDER: dict[str, Any] = {
    "overall": "degraded",
    "services": [
        {"service": "TimescaleDB", "status": "down", "latency_ms": None},
        {"service": "Redis", "status": "down", "latency_ms": None},
    ],
}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/config", response_model=PlatformConfig)
async def get_config(request: Request) -> PlatformConfig:
    """Return the platform configuration from persisted state or env vars."""
    persisted = _get_system_config(request)
    if persisted:
        return PlatformConfig(**persisted)

    trading_mode = os.environ.get("HYDRA_TRADING_MODE", "paper")
    default_pair = os.environ.get("HYDRA_DEFAULT_PAIR", "BTC/USDT")
    default_timeframe = os.environ.get("HYDRA_DEFAULT_TIMEFRAME", "1h")
    max_strategies_raw = os.environ.get("HYDRA_MAX_STRATEGIES", "5")
    try:
        max_strategies = int(max_strategies_raw)
    except ValueError:
        max_strategies = 5
    paper_capital_raw = os.environ.get("HYDRA_PAPER_CAPITAL", "10000")
    try:
        paper_capital = float(paper_capital_raw)
    except ValueError:
        paper_capital = 10000.0

    return PlatformConfig(
        trading_mode=trading_mode,
        default_pair=default_pair,
        default_timeframe=default_timeframe,
        max_concurrent_strategies=max_strategies,
        paper_capital=paper_capital,
    )


@router.put("/config", response_model=PlatformConfig)
async def update_config(body: PlatformConfigUpdate, request: Request) -> PlatformConfig:
    """Update platform configuration fields and persist to app state."""
    current = (await get_config(request)).model_dump()
    update_data = body.model_dump(exclude_none=True)
    current.update(update_data)
    config = PlatformConfig(**current)
    _set_system_config(request, config.model_dump())
    return config


@router.get("/exchanges", response_model=list[ExchangeStatus])
async def get_exchanges(request: Request) -> list[dict[str, Any]]:
    """Exchange connection status based on env var API keys and DB pool health."""
    pool = _pool_from_request(request)

    # Check if the DB pool itself is operational (used as connectivity indicator)
    db_connected = False
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                await asyncio.wait_for(conn.fetchval("SELECT 1"), timeout=2.0)
            db_connected = True
        except Exception:
            logger.debug("DB connectivity check failed for exchange status")

    results: list[dict[str, Any]] = []
    for exchange_id, display_name, env_key in _EXCHANGE_DEFS:
        api_key_set = bool(os.environ.get(env_key))
        # Also check runtime-stored credentials
        creds = _get_exchange_credentials(request)
        if exchange_id in creds:
            api_key_set = True
        connected = api_key_set and (db_connected or exchange_id in creds)
        last_sync = "Active" if connected else ("Not connected" if not api_key_set else "Standby")

        results.append(
            {
                "id": exchange_id,
                "name": display_name,
                "connected": connected,
                "api_key_set": api_key_set,
                "last_sync": last_sync,
            }
        )

    return results if results else _EXCHANGES_PLACEHOLDER


@router.post("/exchanges/{exchange_id}/connect", response_model=ExchangeConnectResponse)
async def connect_exchange(
    exchange_id: str, body: ExchangeConnectRequest, request: Request
) -> dict[str, Any]:
    """Store exchange API credentials and mark as connected."""
    known_ids = {eid for eid, _, _ in _EXCHANGE_DEFS}
    if exchange_id not in known_ids:
        raise HTTPException(status_code=404, detail=f"Unknown exchange: {exchange_id}")

    display_name = next(name for eid, name, _ in _EXCHANGE_DEFS if eid == exchange_id)
    creds = _get_exchange_credentials(request)
    creds[exchange_id] = {
        "api_key": body.api_key,
        "api_secret": body.api_secret,
        "passphrase": body.passphrase,
    }
    logger.info("Exchange credentials stored for %s", exchange_id)

    return {
        "id": exchange_id,
        "name": display_name,
        "connected": True,
        "message": f"Successfully connected to {display_name}",
    }


@router.delete("/exchanges/{exchange_id}/connect", response_model=ExchangeConnectResponse)
async def disconnect_exchange(exchange_id: str, request: Request) -> dict[str, Any]:
    """Remove stored exchange credentials."""
    known_ids = {eid for eid, _, _ in _EXCHANGE_DEFS}
    if exchange_id not in known_ids:
        raise HTTPException(status_code=404, detail=f"Unknown exchange: {exchange_id}")

    display_name = next(name for eid, name, _ in _EXCHANGE_DEFS if eid == exchange_id)
    creds = _get_exchange_credentials(request)
    creds.pop(exchange_id, None)
    logger.info("Exchange credentials removed for %s", exchange_id)

    return {
        "id": exchange_id,
        "name": display_name,
        "connected": False,
        "message": f"Disconnected from {display_name}",
    }


@router.get("/health", response_model=SystemHealth)
async def get_system_health(request: Request) -> dict[str, Any]:
    """Detailed health check covering DB and Redis with real latency."""
    services: list[dict[str, Any]] = []

    # --- TimescaleDB health ---
    pool = _pool_from_request(request)
    if pool is not None:
        try:
            start = time.monotonic()
            async with pool.acquire() as conn:
                await asyncio.wait_for(conn.fetchval("SELECT 1"), timeout=5.0)
            elapsed_ms = round((time.monotonic() - start) * 1000, 2)
            services.append(
                {"service": "TimescaleDB", "status": "healthy", "latency_ms": elapsed_ms}
            )
        except TimeoutError:
            services.append({"service": "TimescaleDB", "status": "degraded", "latency_ms": None})
        except Exception:
            logger.debug("TimescaleDB health check failed", exc_info=True)
            services.append({"service": "TimescaleDB", "status": "down", "latency_ms": None})
    else:
        services.append({"service": "TimescaleDB", "status": "down", "latency_ms": None})

    # --- Redis health ---
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        try:
            import redis.asyncio as aioredis

            start = time.monotonic()
            r = aioredis.from_url(redis_url, socket_connect_timeout=2)
            await asyncio.wait_for(r.ping(), timeout=3.0)
            elapsed_ms = round((time.monotonic() - start) * 1000, 2)
            await r.aclose()
            services.append({"service": "Redis", "status": "healthy", "latency_ms": elapsed_ms})
        except ImportError:
            services.append(
                {
                    "service": "Redis",
                    "status": "degraded",
                    "latency_ms": None,
                }
            )
        except Exception:
            logger.debug("Redis health check failed", exc_info=True)
            services.append({"service": "Redis", "status": "down", "latency_ms": None})
    else:
        services.append({"service": "Redis", "status": "down", "latency_ms": None})

    # --- Determine overall status ---
    statuses = [s["status"] for s in services]
    if all(s == "healthy" for s in statuses):
        overall = "healthy"
    elif any(s == "down" for s in statuses):
        overall = "degraded"
    else:
        overall = "degraded"

    return {"overall": overall, "services": services}
