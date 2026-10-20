from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from hydra.dashboard.routes._db import api_error, db_errors_to_503, require_pool

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

TradingMode = Literal["paper", "live"]
_TRADING_MODES: frozenset[str] = frozenset({"paper", "live"})
_SECRET_FIELDS: tuple[str, ...] = ("webhook_secret", "discord_webhook_url", "slack_webhook_url")
_DEFAULT_PAPER_CAPITAL = 10000.0

# allowed host -> required path prefix
_WEBHOOK_HOSTS: dict[str, str] = {
    "discord.com": "/api/webhooks/",
    "hooks.slack.com": "/services/",
}


def _pool_from_request(request: Request) -> Any:
    """Return the asyncpg connection pool from app state, or ``None``."""
    return getattr(request.app.state, "db_pool", None)


def _get_system_config(request: Request) -> dict[str, Any]:
    """Return persisted system config from app state, if any."""
    return getattr(request.app.state, "system_config", None) or {}


def _set_system_config(request: Request, config: dict[str, Any]) -> None:
    """Persist system config to app state."""
    request.app.state.system_config = config


def _get_exchange_credentials(request: Request) -> dict[str, Any]:
    """Return exchange credentials store from app state."""
    if not hasattr(request.app.state, "exchange_credentials"):
        request.app.state.exchange_credentials = {}
    return request.app.state.exchange_credentials


def _validate_webhook_url(url: str, allowed_host: str) -> str:
    """Accept only https URLs on the exact webhook host; raises ValueError otherwise."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError("webhook URL must use https")
    if parts.username is not None or parts.password is not None:
        raise ValueError("webhook URL must not contain credentials")
    if parts.hostname != allowed_host:
        raise ValueError(f"webhook URL host must be {allowed_host}")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("webhook URL has an invalid port") from exc
    if port not in (None, 443):
        raise ValueError("webhook URL must use the default https port")
    if {".", ".."} & set(unquote(parts.path).split("/")):
        raise ValueError("webhook URL path must not contain dot segments")
    if not parts.path.startswith(_WEBHOOK_HOSTS[allowed_host]):
        raise ValueError(f"webhook URL path must start with {_WEBHOOK_HOSTS[allowed_host]}")
    return url


def _coerce_trading_mode(value: Any, origin: str) -> str:
    if value in _TRADING_MODES:
        return str(value)
    logger.warning("Ignoring invalid trading_mode %r from %s; using 'paper'", value, origin)
    return "paper"


def _env_config() -> dict[str, Any]:
    max_strategies_raw = os.environ.get("HYDRA_MAX_STRATEGIES", "5")
    try:
        max_strategies = int(max_strategies_raw)
    except ValueError:
        max_strategies = 5
    return {
        "trading_mode": _coerce_trading_mode(
            os.environ.get("HYDRA_TRADING_MODE", "paper"), "HYDRA_TRADING_MODE"
        ),
        "default_pair": os.environ.get("HYDRA_DEFAULT_PAIR", "BTC/USDT"),
        "default_timeframe": os.environ.get("HYDRA_DEFAULT_TIMEFRAME", "1h"),
        "max_concurrent_strategies": max_strategies,
        "paper_capital": _DEFAULT_PAPER_CAPITAL,
        **dict.fromkeys(_SECRET_FIELDS, ""),
    }


def _load_stored_config(request: Request) -> dict[str, Any]:
    """Stored config (raw secrets included); env defaults fill missing keys."""
    stored = {**_env_config(), **_get_system_config(request)}
    stored["trading_mode"] = _coerce_trading_mode(stored["trading_mode"], "stored config")
    return stored


def _to_public(stored: dict[str, Any]) -> dict[str, Any]:
    public = {k: v for k, v in stored.items() if k not in _SECRET_FIELDS}
    for field in _SECRET_FIELDS:
        public[f"{field}_set"] = bool(stored.get(field))
    return public


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
    """Public view of the platform config: secrets are reported as *_set only."""

    trading_mode: TradingMode
    default_pair: str
    default_timeframe: str
    max_concurrent_strategies: int
    paper_capital: float
    webhook_secret_set: bool
    discord_webhook_url_set: bool
    slack_webhook_url_set: bool


class PlatformConfigUpdate(BaseModel):
    """Omitted or null fields are unchanged; an empty string clears a secret."""

    trading_mode: TradingMode | None = None
    default_pair: str | None = None
    default_timeframe: str | None = None
    max_concurrent_strategies: int | None = Field(None, ge=1, le=100)
    paper_capital: float | None = Field(None, gt=0, allow_inf_nan=False)
    webhook_secret: str | None = None
    discord_webhook_url: str | None = None
    slack_webhook_url: str | None = None

    @field_validator("discord_webhook_url")
    @classmethod
    def _check_discord(cls, v: str | None) -> str | None:
        return _validate_webhook_url(v, "discord.com") if v else v

    @field_validator("slack_webhook_url")
    @classmethod
    def _check_slack(cls, v: str | None) -> str | None:
        return _validate_webhook_url(v, "hooks.slack.com") if v else v


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
# Endpoints
# ---------------------------------------------------------------------------


def get_paper_capital(request: Request) -> float:
    """Read the paper_capital from system config (single source of truth).

    Used by portfolio and risk routes for the central-wallet model.
    """
    cfg = _get_system_config(request)
    if cfg and "paper_capital" in cfg:
        return float(cfg["paper_capital"])
    return _DEFAULT_PAPER_CAPITAL


@router.get("/config", response_model=PlatformConfig)
async def get_config(request: Request) -> dict[str, Any]:
    """Return the platform configuration; secrets are never returned."""
    return _to_public(_load_stored_config(request))


@router.put("/config", response_model=PlatformConfig)
async def update_config(body: PlatformConfigUpdate, request: Request) -> dict[str, Any]:
    """Merge the given fields into the stored config (in memory until P1 persistence)."""
    stored = _load_stored_config(request)
    stored.update(body.model_dump(exclude_none=True))
    _set_system_config(request, stored)
    return _to_public(stored)


def _unknown_exchange(exchange_id: str) -> HTTPException:
    return api_error(404, "unknown_exchange", f"Unknown exchange: {exchange_id}", "not_found")


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

    return results


@router.post("/exchanges/{exchange_id}/connect", response_model=ExchangeConnectResponse)
async def connect_exchange(
    exchange_id: str, body: ExchangeConnectRequest, request: Request
) -> dict[str, Any]:
    """Encrypt and persist exchange API credentials; 503 if they cannot be stored."""
    known_ids = {eid for eid, _, _ in _EXCHANGE_DEFS}
    if exchange_id not in known_ids:
        raise _unknown_exchange(exchange_id)

    display_name = next(name for eid, name, _ in _EXCHANGE_DEFS if eid == exchange_id)
    from hydra.core.encryption import encrypt

    pool = require_pool(request)
    async with db_errors_to_503("storing exchange credentials"), pool.acquire() as conn:
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
    """Remove stored exchange credentials; 503 (and no change) if the DB delete fails."""
    known_ids = {eid for eid, _, _ in _EXCHANGE_DEFS}
    if exchange_id not in known_ids:
        raise _unknown_exchange(exchange_id)

    display_name = next(name for eid, name, _ in _EXCHANGE_DEFS if eid == exchange_id)

    pool = require_pool(request)
    async with db_errors_to_503("deleting exchange credentials"), pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM exchange_credentials WHERE exchange_id = $1",
            exchange_id,
        )

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
    """Get notification preferences; 503 when they cannot be read."""
    pool = require_pool(request)
    async with db_errors_to_503("reading notification preferences"), pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT preferences FROM notification_preferences ORDER BY id LIMIT 1"
        )
    if row is None:
        return {"preferences": {}}
    prefs = row["preferences"]
    if isinstance(prefs, str):
        prefs = json.loads(prefs)
    return {"preferences": prefs}


@router.put("/notifications")
async def update_notifications(body: NotificationPreferences, request: Request) -> dict[str, Any]:
    """Update notification preferences (upsert)."""
    pool = require_pool(request)
    prefs_json = json.dumps(body.preferences)
    async with db_errors_to_503("updating notification preferences"), pool.acquire() as conn:
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
    return {"preferences": body.preferences}


# ---------------------------------------------------------------------------
# Notification test endpoint
# ---------------------------------------------------------------------------


class NotificationTestResponse(BaseModel):
    discord_sent: bool
    slack_sent: bool
    message: str


@router.post("/notifications/test", response_model=NotificationTestResponse)
async def test_notifications(request: Request) -> NotificationTestResponse:
    """Send a test message to all configured webhook channels.

    Stored URLs are re-validated before any request leaves the server, because
    state can predate the allowlist.
    """
    from hydra.ops.notifications import DiscordWebhookNotifier, SlackWebhookNotifier

    cfg_dict = _get_system_config(request)
    discord_url: str = cfg_dict.get("discord_webhook_url", "")
    slack_url: str = cfg_dict.get("slack_webhook_url", "")

    if not discord_url and not slack_url:
        return NotificationTestResponse(
            discord_sent=False,
            slack_sent=False,
            message=(
                "No webhook URLs configured. "
                "Set discord_webhook_url or slack_webhook_url in platform config."
            ),
        )

    for field, url, host in (
        ("discord_webhook_url", discord_url, "discord.com"),
        ("slack_webhook_url", slack_url, "hooks.slack.com"),
    ):
        if not url:
            continue
        try:
            _validate_webhook_url(url, host)
        except ValueError as exc:
            raise api_error(
                422, "invalid_webhook_url", f"{field}: {exc}", "invalid", field=field
            ) from exc

    title = "Hydra — Test Notification"
    body = "This is a test message from the Hydra trading platform."

    discord_sent = (
        await DiscordWebhookNotifier(discord_url).send(title, body) if discord_url else False
    )
    slack_sent = await SlackWebhookNotifier(slack_url).send(title, body) if slack_url else False

    parts: list[str] = []
    if discord_url:
        parts.append(f"Discord: {'ok' if discord_sent else 'failed'}")
    if slack_url:
        parts.append(f"Slack: {'ok' if slack_sent else 'failed'}")

    return NotificationTestResponse(
        discord_sent=discord_sent,
        slack_sent=slack_sent,
        message=", ".join(parts),
    )


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
        raise api_error(
            404, "credentials_not_found", f"No credentials stored for {exchange_id}", "not_found"
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


def _exchange_error(exchange_id: str) -> HTTPException:
    logger.warning("Exchange request to %s failed", exchange_id, exc_info=True)
    return api_error(502, "exchange_error", f"Request to {exchange_id} failed", "unavailable")


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
        raise _exchange_error(exchange_id) from exc


@router.get("/exchanges/{exchange_id}/orders")
async def get_exchange_orders(exchange_id: str, request: Request) -> dict[str, Any]:
    """Fetch open orders from a configured exchange."""
    client = await _get_exchange_client(exchange_id, request)
    try:
        orders = await client.fetch_open_orders()
        return {"exchange_id": exchange_id, "orders": orders}
    except Exception as exc:
        _get_exchange_clients(request).pop(exchange_id, None)
        raise _exchange_error(exchange_id) from exc


@router.get("/exchanges/{exchange_id}/positions")
async def get_exchange_positions(exchange_id: str, request: Request) -> dict[str, Any]:
    """Fetch open positions from a configured exchange."""
    client = await _get_exchange_client(exchange_id, request)
    try:
        positions = await client.fetch_positions()
        return {"exchange_id": exchange_id, "positions": positions}
    except Exception as exc:
        _get_exchange_clients(request).pop(exchange_id, None)
        raise _exchange_error(exchange_id) from exc
