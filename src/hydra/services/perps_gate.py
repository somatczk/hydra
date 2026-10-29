"""The perps arm gate: the separate code-gated ceremony for live USD⌂-M perp trading.

P4-E: leveraged trading is its own decision. ``perps_armed`` and ``perps_capital`` ride
``system_config`` beside the spot gate's rows (no migration), and arming spot never arms
perps. The ``live_perps_arming`` executor is the ceremony's only write path — a code
shown once and typed back, the capital set as part of arming — and its heart is the
futures-permission re-fetch fail-closed: at arm time the gate RE-FETCHES the connected
key's Binance ``/sapi/v1/account/apiRestrictions`` record (``sapiGetAccountApiRestrictions``,
the name the P4-A hotfix pinned; futures trading permission is a property of the same
API key) and refuses unless ``enableFutures`` reads live-true with withdrawals disabled.
Any failure — the read errors, the flag absent or false — refuses the arm: nothing arms
on stale or absent permission data. The same re-fetch runs on the testnet path, through
the client of the keys' own validated environment: if the testnet key model cannot
confirm futures trading, the arm refuses honestly; the check is never skipped.

The gate is the positive precondition the live perps executor (P4-D) consumes. Every
live FUTURES surface — the session-start executor, the manual live order executor, the
create routes, the MCP tools and the service layer's live branch — refuses with 409
``perps_not_armed`` (403 ``perps_capital_missing`` when armed without the capital) until
the ceremony armed. P4-C's fail-closed executor build is unchanged: even armed, a live
FUTURES start still stops where P4-C stops it until P4-D lands the executor. Disarm is
ungated toward safety, audited, and honest (the R10 pattern): running live perp sessions
are named and keep running under their start's authorization.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

import yaml

from hydra.core.types import MarketType
from hydra.execution.client_factory import build_readonly_client, venue_market_type
from hydra.services import strategies
from hydra.services.errors import ServiceError, db_errors, require_pool, service_error
from hydra.strategy.config import load_strategy_config

logger = logging.getLogger(__name__)

PERPS_ARMED_KEY = "perps_armed"
PERPS_CAPITAL_KEY = "perps_capital"
PERPS_ACTIONS = ("live_perps_arming",)
"""The ceremony's gated action; its executor is the only ``perps_armed`` write path."""

CONNECTED_EXCHANGE = "binance"
"""The connected exchange whose keys carry the futures permission (the spot ceremony's
trade-only key model; ``enableFutures`` is a property of the same API key)."""

RESTRICTIONS_TIMEOUT_S = 10.0

PERPS_SESSION_NOTE = (
    "Live perp sessions keep running under their start's authorization until they are "
    "stopped or the kill switch fires."
)

_UPSERT_SETTING = (
    "INSERT INTO system_config (key, value, updated_at, updated_by) "
    "VALUES ($1, $2::jsonb, now(), $3) "
    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), "
    "updated_by = EXCLUDED.updated_by"
)


# ---------------------------------------------------------------------------
# The armed row (system_config, no migration)
# ---------------------------------------------------------------------------


async def read_perps_state(pool: Any) -> dict[str, Any]:
    """The perps armed state; anything unreadable counts as disarmed (fail-safe)."""
    row: Any = None
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT value FROM system_config WHERE key = $1", PERPS_ARMED_KEY
                )
        except Exception:
            logger.warning("Perps-arming state unreadable; reporting disarmed", exc_info=True)
            row = None
    value: Any = None
    if row is not None:
        try:
            raw = row["value"]
            value = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            value = None
    if not (isinstance(value, dict) and value.get("armed")):
        return {"armed": False, "armed_by": None, "armed_at": None}
    return {
        "armed": True,
        "armed_by": value.get("by"),
        "armed_at": value.get("at"),
    }


async def positively_perps_disarmed(pool: Any) -> bool:
    """The disarm path's raising read (R31's shape): True only when a successful read
    shows the row absent or explicitly disarmed. A failed read is 503 — never a silent
    ``already_disarmed`` — and content that does not positively read disarmed answers
    False so the disarm write replaces it (toward safety)."""
    require_pool(pool)
    async with db_errors("reading the perps-arming state"), pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM system_config WHERE key = $1", PERPS_ARMED_KEY)
    if row is None:
        return True
    raw = row["value"]
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return False
    return not (isinstance(value, dict) and bool(value.get("armed")))


async def read_perps_capital(pool: Any) -> float | None:
    """The persisted ``perps_capital`` baseline, or None when the row is absent/unusable."""
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT value FROM system_config WHERE key = $1", PERPS_CAPITAL_KEY
            )
        value = json.loads(raw) if isinstance(raw, str) else raw
        capital = float(value)
    except Exception:
        logger.warning("perps_capital unreadable", exc_info=True)
        return None
    return capital if capital > 0 else None


async def require_perps_armed(pool: Any) -> dict[str, Any]:
    """The perps armed state, or the 409 every live FUTURES surface answers unarmed."""
    state = await read_perps_state(pool)
    if not state["armed"]:
        raise service_error(
            409,
            "perps_not_armed",
            "Perps trading is not armed; arm it through the separate code-gated "
            "ceremony first (POST /api/live/perps/arm)",
            "invalid",
        )
    return state


async def require_perps_gate(pool: Any) -> dict[str, Any]:
    """The positive precondition every live FUTURES surface checks (P4-D consumes this):
    armed AND the capital set, or the refusal. Anything unreadable counts unarmed
    (fail-closed, the spot deep check's semantics — a DB-less or broken read never
    trades leveraged money)."""
    state = await require_perps_armed(pool)
    if await read_perps_capital(pool) is None:
        raise service_error(
            403,
            "perps_capital_missing",
            "Perps trading is armed but system_config has no perps_capital; disarm and "
            "re-arm through the ceremony to set it",
            "invalid",
        )
    return state


async def write_perps_armed_transition(
    pool: Any, *, armed: bool, actor: str, reason: str, perps_capital: float | None = None
) -> dict[str, Any]:
    """One audited transaction: the ``perps_armed`` row (plus ``perps_capital`` when
    arming) and its ``risk_events`` row — the arm ceremony never writes out-of-band SQL
    (R2's shape). Returns the written row (``{armed, by, at}``)."""
    require_pool(pool)
    at = datetime.now(UTC).isoformat()
    details: dict[str, Any] = {}
    if perps_capital is not None:
        details["perps_capital"] = perps_capital
    async with (
        db_errors("writing the perps-arming state"),
        pool.acquire() as conn,
        conn.transaction(),
    ):
        await conn.execute(
            _UPSERT_SETTING,
            PERPS_ARMED_KEY,
            json.dumps({"armed": armed, "by": actor, "at": at}),
            actor,
        )
        if perps_capital is not None:
            await conn.execute(_UPSERT_SETTING, PERPS_CAPITAL_KEY, json.dumps(perps_capital), actor)
        await conn.execute(
            "INSERT INTO risk_events (scope, kind, tier, actor, reason, details) "
            "VALUES ('paper', $1, NULL, $2, $3, $4::jsonb)",
            "perps_armed" if armed else "perps_disarmed",
            actor,
            reason,
            json.dumps(details),
        )
    logger.info("Perps gate %s by %s", "armed" if armed else "disarmed", actor)
    return {"armed": armed, "by": actor, "at": at}


# ---------------------------------------------------------------------------
# The futures-permission re-fetch fail-closed (the heart)
# ---------------------------------------------------------------------------


def normalize_futures_permissions(raw: Any) -> dict[str, bool]:
    """The sapi ``apiRestrictions`` flags the perps gate checks; an absent flag is
    disabled, never skipped."""
    if not isinstance(raw, dict):
        return {"enable_futures": False, "enable_withdrawals": False}
    return {
        "enable_futures": bool(raw.get("enableFutures")),
        "enable_withdrawals": bool(raw.get("enableWithdrawals")),
    }


def _not_futures_capable(message: str) -> ServiceError:
    return service_error(409, "credentials_not_futures_capable", message, "invalid")


async def require_futures_capable_credentials(pool: Any) -> dict[str, bool]:
    """The connected key re-fetched live: ``enableFutures`` true with withdrawals
    disabled, or the refusal.

    The stored ``validated_permissions`` snapshot is never consulted here — the truth is
    the exchange's own apiRestrictions read at arm time (the R8 shape, futures' leg).
    The keys' validated environment selects the client: mainnet keys re-fetch from
    mainnet, testnet keys from the sandbox (the ceremony must be arm-able on testnet —
    the battery arms it there), and a row validated for neither environment is refused.
    Whatever the environment answers governs: a read that errors, or a record without a
    live-true ``enableFutures``, refuses the arm honestly.
    """
    from cryptography.fernet import InvalidToken

    from hydra.core.encryption import decrypt

    require_pool(pool)
    async with db_errors("reading the stored exchange credentials"), pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT encrypted_key, encrypted_secret, encrypted_passphrase, "
            "validated_environment FROM exchange_credentials WHERE exchange_id = $1",
            CONNECTED_EXCHANGE,
        )
    if row is None:
        raise service_error(
            403,
            "live_credentials_missing",
            f"No {CONNECTED_EXCHANGE} credentials are stored; connect keys in Settings first",
            "not_found",
        )
    try:
        creds = {
            "api_key": decrypt(str(row["encrypted_key"])),
            "api_secret": decrypt(str(row["encrypted_secret"])),
            "passphrase": (
                None
                if row["encrypted_passphrase"] is None
                else decrypt(str(row["encrypted_passphrase"]))
            ),
        }
    except InvalidToken:
        raise service_error(
            403,
            "live_credentials_missing",
            f"The stored {CONNECTED_EXCHANGE} credentials cannot be decrypted; reconnect "
            "them in Settings",
            "invalid",
        ) from None
    environment = str(row["validated_environment"] or "")
    if environment not in ("mainnet", "testnet"):
        raise _not_futures_capable(
            f"The stored {CONNECTED_EXCHANGE} credentials are not validated for mainnet or "
            "testnet; reconnect them in Settings so the futures permission can be checked"
        )
    client = build_readonly_client(CONNECTED_EXCHANGE, creds, testnet=environment == "testnet")
    try:
        raw = await asyncio.wait_for(
            client.fetch_api_restrictions(), timeout=RESTRICTIONS_TIMEOUT_S
        )
    except Exception as exc:
        raise _not_futures_capable(
            f"The {CONNECTED_EXCHANGE} key's futures permission could not be re-validated "
            f"on {environment}: {exc}; nothing was armed"
        ) from exc
    finally:
        try:
            await client.close()
        except Exception:
            logger.debug("Failed to close the futures permission-check client", exc_info=True)
    permissions = normalize_futures_permissions(raw)
    if permissions["enable_withdrawals"]:
        raise _not_futures_capable(
            f"The stored {CONNECTED_EXCHANGE} key has withdrawals enabled; perps trading "
            "needs a trade-only key (disable withdrawals on the key and reconnect it in "
            "Settings)"
        )
    if not permissions["enable_futures"]:
        raise _not_futures_capable(
            f"The stored {CONNECTED_EXCHANGE} key cannot trade futures; perps trading needs "
            "futures enabled on the key (enable futures trading on the key and reconnect "
            "it in Settings)"
        )
    return permissions


# ---------------------------------------------------------------------------
# The venue truth (the strategy's YAML, the session's venue)
# ---------------------------------------------------------------------------


def is_perp_strategy(strategy_id: str) -> bool:
    """True only when the strategy's saved YAML positively reads the perps venue.

    The same file ``SessionManager._find_strategy_config`` loads decides the venue: the
    gate scans that loader's own directory, over both extensions it accepts (``.yaml``
    and ``.yml``), in its order, with its loader and its skip semantics — so this never
    disagrees with the manager: an unreadable or missing file is not a perp (that start
    refuses with ``strategy_not_found`` anyway).
    """
    config_dir = strategies._CONFIG_DIR
    if not config_dir.is_dir():
        return False
    for path in sorted(config_dir.iterdir()):
        if path.suffix not in (".yaml", ".yml"):
            continue
        try:
            cfg = load_strategy_config(path)
        except (OSError, ValueError, TypeError, yaml.YAMLError):
            continue  # the manager's loader skips bad files the same way (SM-15)
        if cfg.id == strategy_id:
            return cfg.exchange.market_type is MarketType.FUTURES
    return False


def is_perp_venue(exchange_id: Any) -> bool:
    """True when a session's venue is the USD⌂-M perps exchange."""
    return venue_market_type(str(exchange_id)) is MarketType.FUTURES if exchange_id else False


# ---------------------------------------------------------------------------
# The executor (the ceremony's only write path)
# ---------------------------------------------------------------------------


def _backend(deps: Any) -> Any:
    """The ``TraderBackend`` the executor checks through, like every route."""
    backend = getattr(deps, "trader", None)
    if backend is not None:
        return backend
    from hydra.services.trader import LocalTrader

    return LocalTrader(getattr(deps, "db_pool", None), None)


async def running_live_perps_sessions(backend: Any) -> list[str]:
    """The ids of the running live sessions on the perps venue (disarm honesty, R10)."""
    sessions = await backend.list_sessions()
    return [
        str(s["session_id"])
        for s in sessions
        if s.get("trading_mode") == "live"
        and s.get("status") == "running"
        and is_perp_venue(s.get("exchange_id"))
    ]


async def execute_perps_arming(deps: Any, params: dict[str, Any], actor: str) -> dict[str, Any]:
    """Arm perps + write ``perps_capital`` as one audited transition.

    Re-validates at execution time: the kill switch must be off and the connected key's
    futures permission re-fetch must confirm ``enableFutures`` live (fail-closed). An
    already-armed perps gate is a clean no-op, mirroring the spot executor.
    """
    if await _backend(deps).kill_switch_active():
        raise service_error(
            409,
            "kill_switch_active",
            "The kill switch is active; release it before arming perps trading",
            "invalid",
        )
    pool = require_pool(getattr(deps, "db_pool", None))
    await require_futures_capable_credentials(pool)
    reason = str(params.get("reason") or "perps arming")
    try:
        capital = float(params["perps_capital"])
    except (KeyError, TypeError, ValueError) as exc:
        raise service_error(
            422, "invalid_params", "perps_capital must be a positive number", "invalid"
        ) from exc
    if not capital > 0:  # not <= 0: NaN fails every comparison
        raise service_error(
            422, "invalid_params", "perps_capital must be a positive number", "invalid"
        )
    state = await read_perps_state(pool)
    if state["armed"]:
        return {
            "status": "already_armed",
            "perps_armed": True,
            "armed_by": state["armed_by"],
            "armed_at": state["armed_at"],
            "perps_capital": await read_perps_capital(pool),
            "message": "Perps trading is already armed; nothing changed",
        }
    written = await write_perps_armed_transition(
        pool, armed=True, actor=actor, reason=reason, perps_capital=capital
    )
    return {
        "status": "armed",
        "perps_armed": True,
        "armed_by": written["by"],
        "armed_at": written["at"],
        "perps_capital": capital,
        "message": "Perps trading is armed; live perp session starts and manual live "
        "orders now need their own one-time code",
    }
