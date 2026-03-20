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


def _pool_from_request(request: Request) -> Any:
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
    # Encrypt and persist to DB
    from hydra.core.encryption import encrypt

    pool = _pool_from_request(request)
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO exchange_credentials "
                    "(exchange_id, encrypted_key, encrypted_secret, "
                    "encrypted_passphrase, updated_at) "
                    "VALUES ($1, $2, $3, $4, now()) "
                    "ON CONFLICT (exchange_id) DO UPDATE SET "
                    "encrypted_key = EXCLUDED.encrypted_key, "
                    "encrypted_secret = EXCLUDED.encrypted_secret, "
                    "encrypted_passphrase = EXCLUDED.encrypted_passphrase, "
                    "updated_at = now()",
                    exchange_id,
                    encrypt(body.api_key),
                    encrypt(body.api_secret),
                    encrypt(body.passphrase) if body.passphrase else None,
                )
        except Exception:
            logger.exception("Failed to persist encrypted credentials for %s", exchange_id)

    # Keep in app state for fast access (plaintext in memory)
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

    # Remove from DB
    pool = _pool_from_request(request)
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM exchange_credentials WHERE exchange_id = $1",
                    exchange_id,
                )
        except Exception:
            logger.exception("Failed to delete credentials for %s", exchange_id)

    creds = _get_exchange_credentials(request)
    creds.pop(exchange_id, None)
    # Evict and close cached exchange client
    clients = _get_exchange_clients(request)
    old_client = clients.pop(exchange_id, None)
    if old_client is not None:
        try:
            await old_client.close()
        except Exception:
            logger.debug("Failed to close exchange client for %s", exchange_id)
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
            await r.close()
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


# ---------------------------------------------------------------------------
# Notification preferences
# ---------------------------------------------------------------------------


class NotificationPreferences(BaseModel):
    preferences: dict[str, Any] = {}


@router.get("/notifications")
async def get_notifications(request: Request) -> dict[str, Any]:
    """Get notification preferences."""
    pool = _pool_from_request(request)
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT preferences FROM notification_preferences ORDER BY id LIMIT 1"
                )
                if row is not None:
                    import json

                    prefs = row["preferences"]
                    if isinstance(prefs, str):
                        prefs = json.loads(prefs)
                    return {"preferences": prefs}
        except Exception:
            logger.exception("Failed to fetch notification preferences")
    return {"preferences": {}}


@router.put("/notifications")
async def update_notifications(body: NotificationPreferences, request: Request) -> dict[str, Any]:
    """Update notification preferences (upsert)."""
    pool = _pool_from_request(request)
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        import json

        prefs_json = json.dumps(body.preferences)
        async with pool.acquire() as conn:
            # Upsert: try update first, then insert if no rows exist
            result = await conn.execute(
                "UPDATE notification_preferences "
                "SET preferences = $1::jsonb, updated_at = now() "
                "WHERE id = (SELECT id FROM notification_preferences ORDER BY id LIMIT 1)",
                prefs_json,
            )
            if result == "UPDATE 0":
                await conn.execute(
                    "INSERT INTO notification_preferences (preferences) VALUES ($1::jsonb)",
                    prefs_json,
                )
    except Exception as exc:
        logger.exception("Failed to update notification preferences")
        raise HTTPException(status_code=500, detail="Failed to update preferences") from exc
    return {"preferences": body.preferences}


# ---------------------------------------------------------------------------
# Exchange data endpoints (balance, orders, positions)
# ---------------------------------------------------------------------------


def _get_exchange_clients(request: Request) -> dict[str, Any]:
    """Return the cached exchange client map from app state."""
    if not hasattr(request.app.state, "_exchange_clients"):
        request.app.state._exchange_clients = {}
    return request.app.state._exchange_clients


async def _get_exchange_client(exchange_id: str, request: Request) -> Any:
    """Return (or create and cache) an ExchangeClient for the given exchange."""
    clients = _get_exchange_clients(request)
    if exchange_id in clients:
        return clients[exchange_id]

    creds = _get_exchange_credentials(request)
    if exchange_id not in creds:
        raise HTTPException(
            status_code=404,
            detail=f"No credentials stored for {exchange_id}",
        )
    from hydra.execution.exchange_client import ExchangeClient

    cred = creds[exchange_id]
    client = ExchangeClient(
        exchange_id=exchange_id,  # type: ignore[arg-type]
        config={
            "apiKey": cred["api_key"],
            "secret": cred["api_secret"],
            **({"password": cred["passphrase"]} if cred.get("passphrase") else {}),
        },
        testnet=False,
    )
    clients[exchange_id] = client
    return client


@router.get("/exchanges/{exchange_id}/balance")
async def get_exchange_balance(exchange_id: str, request: Request) -> dict[str, Any]:
    """Fetch live balance from a configured exchange."""
    client = await _get_exchange_client(exchange_id, request)
    try:
        balances = await client.fetch_balance()
        return {"exchange_id": exchange_id, "balances": {k: float(v) for k, v in balances.items()}}
    except Exception as exc:
        # Evict cached client on error so next request creates a fresh one
        _get_exchange_clients(request).pop(exchange_id, None)
        raise HTTPException(status_code=502, detail=f"Exchange error: {exc}") from exc


@router.get("/exchanges/{exchange_id}/orders")
async def get_exchange_orders(exchange_id: str, request: Request) -> dict[str, Any]:
    """Fetch open orders from a configured exchange."""
    client = await _get_exchange_client(exchange_id, request)
    try:
        orders = await client.fetch_open_orders()
        return {"exchange_id": exchange_id, "orders": orders}
    except Exception as exc:
        _get_exchange_clients(request).pop(exchange_id, None)
        raise HTTPException(status_code=502, detail=f"Exchange error: {exc}") from exc


@router.get("/exchanges/{exchange_id}/positions")
async def get_exchange_positions(exchange_id: str, request: Request) -> dict[str, Any]:
    """Fetch open positions from a configured exchange."""
    client = await _get_exchange_client(exchange_id, request)
    try:
        positions = await client.fetch_positions()
        return {"exchange_id": exchange_id, "positions": positions}
    except Exception as exc:
        _get_exchange_clients(request).pop(exchange_id, None)
        raise HTTPException(status_code=502, detail=f"Exchange error: {exc}") from exc
