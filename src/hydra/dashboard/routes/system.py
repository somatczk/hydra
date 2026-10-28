"""Platform config (persisted in ``system_config``), exchange credentials, health, notifications."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, cast
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError, field_validator

from hydra.dashboard.deps import current_config
from hydra.dashboard.routes._db import (
    api_error,
    db_errors_to_503,
    request_trader,
    require_pool,
)
from hydra.dashboard.security import Principal, audit
from hydra.execution import session_store
from hydra.services.live_gate import is_trade_capable, stored_permissions

router = APIRouter(prefix="/api/system", tags=["system"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Exchange definitions: id -> display name; only Binance can be connected until P4 (decision 4)
_EXCHANGE_DEFS: dict[str, str] = {
    "binance": "Binance",
    "bybit": "Bybit",
    "kraken": "Kraken",
    "okx": "OKX",
}
_SUPPORTED_EXCHANGES: frozenset[str] = frozenset({"binance"})
CONNECT_TIMEOUT_S = 10.0

TradingMode = Literal["paper", "live"]
CredentialState = Literal["ok", "undecryptable", "missing"]
Environment = Literal["testnet", "mainnet"]
_SETTING_FIELDS: tuple[str, ...] = (
    "trading_mode",
    "default_pair",
    "default_timeframe",
    "max_concurrent_strategies",
    "paper_capital",
)
_SECRET_FIELDS: tuple[str, ...] = ("webhook_secret", "discord_webhook_url", "slack_webhook_url")

# allowed host -> required path prefix
_WEBHOOK_HOSTS: dict[str, str] = {
    "discord.com": "/api/webhooks/",
    "hooks.slack.com": "/services/",
}


def _pool_from_request(request: Request) -> Any:
    """Return the asyncpg connection pool from app state, or ``None``."""
    return getattr(request.app.state, "db_pool", None)


def _principal(request: Request) -> Principal | None:
    principal = getattr(request.state, "principal", None)
    return principal if isinstance(principal, Principal) else None


def _actor(request: Request) -> str:
    principal = _principal(request)
    return f"{principal.kind}:{principal.name}" if principal is not None else "anonymous"


def _get_exchange_credentials(request: Request) -> dict[str, Any]:
    """Return exchange credentials store from app state."""
    if not hasattr(request.app.state, "exchange_credentials"):
        request.app.state.exchange_credentials = {}
    return cast(dict[str, Any], request.app.state.exchange_credentials)


def _get_credential_errors(request: Request) -> dict[str, str]:
    errors = getattr(request.app.state, "exchange_credential_errors", None)
    if errors is None:
        errors = {}
        request.app.state.exchange_credential_errors = errors
    return cast(dict[str, str], errors)


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


# ---------------------------------------------------------------------------
# system_config persistence
# ---------------------------------------------------------------------------


class StoredConfig(BaseModel):
    """The persisted platform config; every non-secret key is a required row."""

    trading_mode: TradingMode
    default_pair: str = Field(min_length=1)
    default_timeframe: str = Field(min_length=1)
    max_concurrent_strategies: int = Field(ge=1, le=100)
    paper_capital: float = Field(gt=0, allow_inf_nan=False)
    webhook_secret: str = ""
    discord_webhook_url: str = ""
    slack_webhook_url: str = ""


async def load_system_config(pool: Any) -> dict[str, Any]:
    """All system_config rows, JSON-decoded, with the secret rows decrypted.

    A secret the credential key cannot decrypt is left out (so it reads as not set and the
    webhook fails closed) and logged; it has to be entered again in Settings.
    """
    from cryptography.fernet import InvalidToken

    from hydra.core.encryption import decrypt

    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT key, value FROM system_config")
    config: dict[str, Any] = {}
    for row in rows:
        key = row["key"]
        value = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
        if key in _SECRET_FIELDS:
            try:
                value = decrypt(str(value))
            except InvalidToken:
                logger.error(
                    "system_config %s cannot be decrypted with HYDRA_CREDENTIAL_KEY; set it again "
                    "in Settings",
                    key,
                )
                continue
        config[key] = value
    return config


def _validated(raw: dict[str, Any]) -> StoredConfig:
    missing = [key for key in _SETTING_FIELDS if key not in raw]
    if missing:
        raise api_error(
            503,
            "system_config_missing",
            f"system_config has no row for: {', '.join(missing)}",
            "unavailable",
            missing=missing,
        )
    try:
        return StoredConfig.model_validate(raw)
    except ValidationError as exc:
        fields = sorted({str(err["loc"][0]) for err in exc.errors() if err.get("loc")})
        raise api_error(
            503,
            "system_config_invalid",
            f"system_config has invalid values for: {', '.join(fields)}",
            "unavailable",
            fields=fields,
        ) from exc


async def read_system_config(pool: Any) -> StoredConfig:
    """The validated persisted config; 503 when a row is missing, invalid or unreadable."""
    async with db_errors_to_503("reading system config"):
        raw = await load_system_config(pool)
    return _validated(raw)


_UPSERT_SETTING = (
    "INSERT INTO system_config (key, value, updated_at, updated_by) "
    "VALUES ($1, $2::jsonb, now(), $3) "
    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), "
    "updated_by = EXCLUDED.updated_by"
)


async def _save_system_config(pool: Any, updates: dict[str, Any], actor: str) -> None:
    """Upsert the given keys in one transaction; secrets are encrypted, '' deletes a secret."""
    from hydra.core.encryption import CredentialKeyError, encrypt

    try:
        values = {
            key: (encrypt(value) if key in _SECRET_FIELDS and value else value)
            for key, value in updates.items()
        }
    except CredentialKeyError as exc:
        raise api_error(
            503,
            "credential_key_unavailable",
            "Secrets cannot be stored: HYDRA_CREDENTIAL_KEY is not configured",
            "unavailable",
        ) from exc
    async with (
        db_errors_to_503("saving system config"),
        pool.acquire() as conn,
        conn.transaction(),
    ):
        for key, value in values.items():
            if key in _SECRET_FIELDS and value == "":
                await conn.execute("DELETE FROM system_config WHERE key = $1", key)
            else:
                await conn.execute(_UPSERT_SETTING, key, json.dumps(value), actor)


def risk_service(request: Request) -> Any:
    """The process-wide RiskService on the trader's session manager, or ``None``."""
    from hydra.risk.service import RiskService

    backend = request_trader(request)
    svc = getattr(backend, "risk", None)
    return svc if isinstance(svc, RiskService) else None


async def _reload_paper_capital(request: Request) -> None:
    """Hand paper_capital to the risk ledger, which rebases via its audited paper reset."""
    try:
        await request_trader(request).reload_paper_capital(_actor(request))
    except Exception as exc:
        logger.exception("paper_capital saved but the running engine did not reload it")
        raise api_error(
            503,
            "risk_reload_failed",
            "paper_capital was saved but the running engine could not apply it; save it again",
            "unavailable",
        ) from exc


def _to_public(stored: StoredConfig, pending: Decimal | None = None) -> dict[str, Any]:
    data = stored.model_dump()
    public = {k: v for k, v in data.items() if k not in _SECRET_FIELDS}
    for field in _SECRET_FIELDS:
        public[f"{field}_set"] = bool(data.get(field))
    public["paper_capital_pending"] = None if pending is None else float(pending)
    return public


def _as_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def pending_capital(request: Request, stored: Any) -> Decimal | None:
    """The saved paper_capital the risk ledger does not run on yet, or ``None`` when in sync."""
    svc = risk_service(request)
    if svc is None:
        return None
    if svc.pending_paper_capital is not None:
        return cast(Decimal, svc.pending_paper_capital)
    wanted = _as_decimal(stored)
    applied = svc.paper_capital
    if wanted is not None and (applied is None or applied != wanted):
        return wanted
    return None


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


class ExchangeStatus(BaseModel):
    id: str
    name: str
    supported: bool
    connected: bool
    api_key_set: bool
    credential_state: CredentialState
    last_sync: str | None
    environment: Environment
    validated_environment: Environment | None = None
    # R9: the connect-time permission record (None before P3-C or on testnet)
    trade_enabled: bool | None = None
    ip_restrict: bool | None = None


class PlatformConfig(BaseModel):
    """Public view of the platform config: secrets are reported as *_set only."""

    trading_mode: TradingMode
    default_pair: str
    default_timeframe: str
    max_concurrent_strategies: int
    paper_capital: float
    paper_capital_pending: float | None = None
    webhook_secret_set: bool
    discord_webhook_url_set: bool
    slack_webhook_url_set: bool


class PlatformConfigUpdate(BaseModel):
    """Omitted or null fields are unchanged; an empty string clears a secret.

    ``trading_mode`` is deliberately absent (the legacy row is frozen at ``paper`` and
    unwritable via the API — the armed row is the single live gate), and
    ``live_capital`` is the live baseline (R2: also written by the arm ceremony).
    """

    default_pair: str | None = Field(None, min_length=1)
    default_timeframe: str | None = Field(None, min_length=1)
    max_concurrent_strategies: int | None = Field(None, ge=1, le=100)
    paper_capital: float | None = Field(None, gt=0, allow_inf_nan=False)
    live_capital: float | None = Field(None, gt=0, allow_inf_nan=False)
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
    api_key: str = Field(min_length=1)
    api_secret: str = Field(min_length=1)
    passphrase: str | None = None


class ExchangeConnectResponse(BaseModel):
    id: str
    name: str
    connected: bool
    message: str
    validated_at: str | None = None
    environment: Environment
    trade_enabled: bool | None = None
    ip_restrict: bool | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


async def get_paper_capital(pool: Any) -> float:
    """The persisted global paper_capital; 503 when the row is missing or invalid."""
    async with db_errors_to_503("reading paper capital"), pool.acquire() as conn:
        raw = await conn.fetchval("SELECT value FROM system_config WHERE key = 'paper_capital'")
    if raw is None:
        raise api_error(
            503,
            "system_config_missing",
            "system_config has no row for: paper_capital",
            "unavailable",
            missing=["paper_capital"],
        )
    try:
        capital = float(json.loads(raw) if isinstance(raw, str) else raw)
    except (TypeError, ValueError) as exc:
        raise api_error(
            503,
            "system_config_invalid",
            "system_config paper_capital is not a number",
            "unavailable",
        ) from exc
    if not capital > 0:
        raise api_error(
            503, "system_config_invalid", "system_config paper_capital must be > 0", "unavailable"
        )
    return capital


@router.get("/config", response_model=PlatformConfig)
async def get_config(request: Request) -> dict[str, Any]:
    """Return the persisted platform configuration; secrets are never returned."""
    pool = require_pool(request)
    stored = await read_system_config(pool)
    return _to_public(stored, pending_capital(request, stored.paper_capital))


@router.put("/config", response_model=PlatformConfig)
async def update_config(body: PlatformConfigUpdate, request: Request) -> dict[str, Any]:
    """Persist the given fields; a paper_capital change goes through the audited paper reset."""
    pool = require_pool(request)
    updates = body.model_dump(exclude_none=True)
    if updates:
        await _save_system_config(pool, updates, _actor(request))
        async with db_errors_to_503("reading system config"):
            request.app.state.system_config = await load_system_config(pool)
        audit(
            _principal(request),
            "system_config_updated",
            keys=sorted(k for k in updates if k not in _SECRET_FIELDS),
            secrets_set=sorted(k for k in updates if k in _SECRET_FIELDS and updates[k]),
            secrets_cleared=sorted(k for k in updates if k in _SECRET_FIELDS and not updates[k]),
        )
        if "paper_capital" in updates:
            await _reload_paper_capital(request)
    stored = await read_system_config(pool)
    return _to_public(stored, pending_capital(request, stored.paper_capital))


def _unknown_exchange(exchange_id: str) -> HTTPException:
    return api_error(404, "unknown_exchange", f"Unknown exchange: {exchange_id}", "not_found")


def _require_supported(exchange_id: str) -> str:
    if exchange_id not in _EXCHANGE_DEFS:
        raise _unknown_exchange(exchange_id)
    if exchange_id not in _SUPPORTED_EXCHANGES:
        raise api_error(
            400,
            "unsupported_exchange",
            f"{_EXCHANGE_DEFS[exchange_id]} is not supported yet; only Binance can be connected",
            "invalid",
        )
    return exchange_id


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts is not None else None


def current_environment() -> Environment:
    """Where keyed calls go: the Binance sandbox while ``trading.testnet`` is true."""
    return "testnet" if current_config().trading.testnet else "mainnet"


def _venue(exchange_id: str, environment: Environment) -> str:
    return f"{_EXCHANGE_DEFS[exchange_id]} {environment}"


@router.get("/exchanges", response_model=list[ExchangeStatus])
async def get_exchanges(request: Request) -> list[dict[str, Any]]:
    """Stored-credential state per exchange; connected means the keys passed a read-only check."""
    pool = require_pool(request)
    async with db_errors_to_503("reading exchange credentials"), pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT exchange_id, validated_at, validated_environment, validated_permissions "
            "FROM exchange_credentials"
        )
    stored = {row["exchange_id"]: row for row in rows}
    errors = _get_credential_errors(request)
    environment = current_environment()

    results: list[dict[str, Any]] = []
    for exchange_id, display_name in _EXCHANGE_DEFS.items():
        state: CredentialState
        if exchange_id not in stored:
            state = "missing"
        elif exchange_id in errors:
            state = "undecryptable"
        else:
            state = "ok"
        row = stored.get(exchange_id)
        validated_at = row["validated_at"] if row is not None else None
        validated_env = row["validated_environment"] if row is not None else None
        permissions = stored_permissions(row["validated_permissions"]) if row is not None else None
        results.append(
            {
                "id": exchange_id,
                "name": display_name,
                "supported": exchange_id in _SUPPORTED_EXCHANGES,
                "connected": state == "ok"
                and validated_at is not None
                and validated_env == environment,
                "api_key_set": exchange_id in stored,
                "credential_state": state,
                "last_sync": _iso(validated_at),
                "environment": environment,
                "validated_environment": validated_env,
                "trade_enabled": (
                    None
                    if permissions is None
                    else bool(permissions.get("enable_spot_and_margin_trading"))
                ),
                "ip_restrict": None
                if permissions is None
                else bool(permissions.get("ip_restrict")),
            }
        )
    return results


async def _evict_client(request: Request, exchange_id: str) -> None:
    old_client = _get_exchange_clients(request).pop(exchange_id, None)
    if old_client is not None:
        try:
            await old_client.close()
        except Exception:
            logger.debug("Failed to close exchange client for %s", exchange_id)


@router.post("/exchanges/{exchange_id}/connect", response_model=ExchangeConnectResponse)
async def connect_exchange(
    exchange_id: str, body: ExchangeConnectRequest, request: Request
) -> dict[str, Any]:
    """Check the keys with a read-only call, then encrypt and store them; nothing on failure."""
    exchange = _require_supported(exchange_id)
    display_name = _EXCHANGE_DEFS[exchange]
    pool = require_pool(request)
    creds = {
        "api_key": body.api_key,
        "api_secret": body.api_secret,
        "passphrase": body.passphrase or None,
    }
    environment = current_environment()
    restrictions = await _check_credentials(exchange, creds, environment)

    from hydra.core.encryption import encrypt

    async with db_errors_to_503("storing exchange credentials"), pool.acquire() as conn:
        validated_at = await conn.fetchval(
            "INSERT INTO exchange_credentials "
            "(exchange_id, encrypted_key, encrypted_secret, encrypted_passphrase, updated_at, "
            "validated_at, validated_environment, validated_permissions) "
            "VALUES ($1, $2, $3, $4, now(), now(), $5, $6::jsonb) "
            "ON CONFLICT (exchange_id) DO UPDATE SET "
            "encrypted_key = EXCLUDED.encrypted_key, "
            "encrypted_secret = EXCLUDED.encrypted_secret, "
            "encrypted_passphrase = EXCLUDED.encrypted_passphrase, "
            "updated_at = now(), validated_at = now(), "
            "validated_environment = EXCLUDED.validated_environment, "
            "validated_permissions = EXCLUDED.validated_permissions "
            "RETURNING validated_at",
            exchange,
            encrypt(body.api_key),
            encrypt(body.api_secret),
            encrypt(body.passphrase) if body.passphrase else None,
            environment,
            None if restrictions is None else json.dumps(restrictions),
        )

    _get_exchange_credentials(request)[exchange] = creds
    _get_credential_errors(request).pop(exchange, None)
    await _evict_client(request, exchange)
    audit(_principal(request), "exchange_connected", exchange_id=exchange, environment=environment)

    return {
        "id": exchange,
        "name": display_name,
        "connected": True,
        "message": f"{_venue(exchange, environment)} accepted these keys; they are stored",
        "validated_at": _iso(validated_at),
        "environment": environment,
        "trade_enabled": (
            None if restrictions is None else restrictions["enable_spot_and_margin_trading"]
        ),
        "ip_restrict": None if restrictions is None else restrictions["ip_restrict"],
    }


@router.delete("/exchanges/{exchange_id}/connect", response_model=ExchangeConnectResponse)
async def disconnect_exchange(exchange_id: str, request: Request) -> dict[str, Any]:
    """Remove stored exchange credentials; 503 (and no change) if the DB delete fails.

    Trade-capable keys need the code-gated ``POST /api/live/credential-delete``
    ceremony (the executor also refuses while any live session runs); read-only keys
    delete directly, exactly as before.
    """
    if exchange_id not in _EXCHANGE_DEFS:
        raise _unknown_exchange(exchange_id)
    display_name = _EXCHANGE_DEFS[exchange_id]

    pool = require_pool(request)
    async with db_errors_to_503("reading exchange credentials"), pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT validated_permissions FROM exchange_credentials WHERE exchange_id = $1",
            exchange_id,
        )
    if row is not None and is_trade_capable(row["validated_permissions"]):
        raise api_error(
            409,
            "credential_delete_gated",
            f"The stored {display_name} keys can trade; deleting them needs the code-gated "
            "POST /api/live/credential-delete ceremony",
            "invalid",
        )
    async with db_errors_to_503("deleting exchange credentials"), pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM exchange_credentials WHERE exchange_id = $1",
            exchange_id,
        )

    _get_exchange_credentials(request).pop(exchange_id, None)
    _get_credential_errors(request).pop(exchange_id, None)
    await _evict_client(request, exchange_id)
    audit(_principal(request), "exchange_disconnected", exchange_id=exchange_id)

    return {
        "id": exchange_id,
        "name": display_name,
        "connected": False,
        "message": f"Disconnected from {display_name}",
        "environment": current_environment(),
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


@router.get("/readiness")
async def get_readiness(request: Request) -> dict[str, Any]:
    """The health monitor's last ``/readyz`` result, when it ran and whether ``/readyz`` would
    still serve it (``stale``, judged on the server's clock); never runs a check."""
    monitor = getattr(request.app.state, "health_monitor", None)
    last = getattr(monitor, "last", None)
    if monitor is None or last is None:
        raise api_error(
            503, "readiness_unavailable", "No readiness check has run yet", "unavailable"
        )
    return {
        **last.body(),
        "checked_at": datetime.fromtimestamp(last.checked_at, UTC).isoformat(),
        "stale": monitor.fresh() is None,
    }


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

    pool = require_pool(request)
    async with db_errors_to_503("reading system config"):
        cfg_dict = await load_system_config(pool)
    discord_url = str(cfg_dict.get("discord_webhook_url") or "")
    slack_url = str(cfg_dict.get("slack_webhook_url") or "")

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
    return cast(dict[str, Any], request.app.state._exchange_clients)


def _readonly_client(exchange_id: str, creds: dict[str, Any], environment: Environment) -> Any:
    """E's read-only Binance SPOT client; the sandbox on ``testnet``."""
    from hydra.execution.client_factory import build_readonly_client

    return build_readonly_client(exchange_id, creds, testnet=environment == "testnet")


async def _keyed_read(read: Any, name: str) -> Any:
    """One keyed read with the key-check error mapping; 422 on rejection, 502 on
    unreachability or refusal, both with nothing stored."""
    import ccxt

    try:
        return await asyncio.wait_for(read, CONNECT_TIMEOUT_S)
    except ccxt.AuthenticationError as exc:
        logger.info("%s rejected the submitted API keys (%s)", name, type(exc).__name__)
        raise api_error(
            422,
            "credentials_rejected",
            f"{name} rejected these API keys (key, secret or permissions); nothing was stored",
            "invalid",
        ) from exc
    except (ccxt.NetworkError, TimeoutError) as exc:
        logger.warning("%s unreachable while checking keys (%s)", name, type(exc).__name__)
        raise api_error(
            502,
            "exchange_unreachable",
            f"{name} could not be reached to check the keys; nothing was stored",
            "unavailable",
        ) from exc
    except ccxt.ExchangeError as exc:
        logger.warning("%s refused the key check (%s)", name, type(exc).__name__)
        raise api_error(
            502,
            "exchange_error",
            f"{name} returned an error while checking the keys; nothing was stored",
            "unavailable",
        ) from exc


async def _check_credentials(
    exchange_id: str, creds: dict[str, Any], environment: Environment
) -> dict[str, Any] | None:
    """One read-only balance call, then the key's permissions on mainnet (never trades).

    Returns the normalized ``validated_permissions`` record for storage, or ``None``
    on testnet (the apiRestrictions read is a mainnet-only check). A
    withdrawals-enabled key is refused 422 ``credentials_can_withdraw`` — nothing is
    stored — so a connected key is trade-only.
    """
    from hydra.services.live_gate import normalize_restrictions

    name = _venue(exchange_id, environment)
    client = _readonly_client(exchange_id, creds, environment)
    try:
        await _keyed_read(client.fetch_balance(), name)
        restrictions: dict[str, Any] | None = None
        if environment == "mainnet":
            raw = await _keyed_read(client.fetch_api_restrictions(), name)
            restrictions = normalize_restrictions(raw)
            if restrictions["enable_withdrawals"]:
                raise api_error(
                    422,
                    "credentials_can_withdraw",
                    f"{name} reports these keys can withdraw; live trading needs "
                    "trade-only keys (disable withdrawals on the key); nothing was stored",
                    "invalid",
                )
        return restrictions
    finally:
        try:
            await client.close()
        except Exception:
            logger.debug("Failed to close the key-check client for %s", exchange_id)


async def _get_exchange_client(exchange_id: str, request: Request) -> Any:
    """Return (or create and cache) the read-only client for a connected exchange."""
    exchange = _require_supported(exchange_id)
    clients = _get_exchange_clients(request)
    if exchange in clients:
        return clients[exchange]

    creds = _get_exchange_credentials(request)
    if exchange not in creds:
        raise api_error(
            404, "credentials_not_found", f"No credentials stored for {exchange}", "not_found"
        )
    client = _readonly_client(exchange, creds[exchange], current_environment())
    clients[exchange] = client
    return client


async def _exchange_error(request: Request, exchange_id: str) -> HTTPException:
    logger.warning("Exchange request to %s failed", exchange_id, exc_info=True)
    await _evict_client(request, exchange_id)
    venue = _venue(exchange_id, current_environment())
    return api_error(502, "exchange_error", f"Request to {venue} failed", "unavailable")


async def _order_symbols(request: Request) -> list[str]:
    """default_pair plus the symbols of running sessions (Binance wants a symbol per call).

    Running sessions are read from the database, so the order book still covers them when
    the trading engine is down.
    """
    pool = require_pool(request)
    stored = await read_system_config(pool)
    symbols = [stored.default_pair]
    async with db_errors_to_503("reading running sessions"):
        rows = await session_store.load_running(pool)
    for row in rows:
        symbols += [str(s) for s in row["symbols"]]
    unique: dict[str, str] = {}
    for symbol in symbols:
        unique.setdefault(symbol.replace("/", "").upper(), symbol)
    return list(unique.values())


@router.get("/exchanges/{exchange_id}/balance")
async def get_exchange_balance(exchange_id: str, request: Request) -> dict[str, Any]:
    """Fetch live balance from a configured exchange."""
    client = await _get_exchange_client(exchange_id, request)
    try:
        balances = await client.fetch_balance()
        return {"exchange_id": exchange_id, "balances": {k: float(v) for k, v in balances.items()}}
    except Exception as exc:
        raise await _exchange_error(request, exchange_id) from exc


@router.get("/exchanges/{exchange_id}/orders")
async def get_exchange_orders(exchange_id: str, request: Request) -> dict[str, Any]:
    """Open orders for default_pair and every running session's symbols."""
    client = await _get_exchange_client(exchange_id, request)
    symbols = await _order_symbols(request)
    try:
        orders: list[dict[str, Any]] = []
        for symbol in symbols:
            orders += await client.fetch_open_orders(symbol)
        return {"exchange_id": exchange_id, "symbols": symbols, "orders": orders}
    except Exception as exc:
        raise await _exchange_error(request, exchange_id) from exc


@router.get("/exchanges/{exchange_id}/positions")
async def get_exchange_positions(exchange_id: str) -> dict[str, Any]:
    """A spot account has no positions (holdings are the balance); 501 without a call."""
    _require_supported(exchange_id)
    raise api_error(
        501,
        "spot_no_positions",
        f"{_EXCHANGE_DEFS[exchange_id]} is connected as a spot account: it has no positions, "
        "holdings are listed under Balance",
        "not_implemented",
    )
