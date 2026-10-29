"""The live gate: the armed row, trade-only credentials and the four live executors.

Model (c): the persisted ``live_armed`` ``system_config`` row ({``armed``, ``by``, ``at``};
an absent or unreadable row is disarmed, fail-safe) is the operator's master switch,
turned ON only through the code-gated ``live_arming`` approval executor — no REST path
writes it — and OFF directly (disarm is toward safety, ungated and audited). Every live
session start and every manual live order then needs its own one-time code; strategy
orders inside a running live session flow under that start's standing authorization,
still bounded by the kill switch, risk limits, breakers and exchange stops.

The four executors (``live_arming``, ``live_session_start``, ``live_order``,
``credential_delete``) are the only live paths. Each re-validates its target state at
execution time — the approval may have sat — and calls through the ``TraderBackend``
(``deps.trader``) like every route, so the executors work in both the single-process and
the split layout. The API surfaces (the session-start and quick-trade routes, the
quick-trade tool, the webhook) refuse live with 409 ``live_gate_required`` instead; see
seam #1. The separate perps ceremony's executor (``live_perps_arming``,
:mod:`hydra.services.perps_gate`) registers on the same store beside them: it is the
only ``perps_armed`` write path and never arms spot trading (P4-E).

Trade-only credentials: arming re-fetches Binance ``/sapi/v1/account/apiRestrictions``
through the keyed client (R8) — not just the connect-time snapshot — and refuses unless
the keys are mainnet-validated, spot-trade-enabled and withdrawal-disabled. Credential
delete is code-gated while the stored key can trade; both the gated executor and the
direct route refuse while any live session runs (the trader's keyed pool entry outlives
an API-side evict).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from hydra.core.logging import get_logger
from hydra.execution.client_factory import build_readonly_client
from hydra.risk.state import RiskEventRecord
from hydra.services.approvals import Approval, ApprovalStore, write_approval_event
from hydra.services.errors import ServiceError, db_errors, require_pool, service_error
from hydra.services.perps_gate import (
    execute_perps_arming,
    is_perp_strategy,
    is_perp_venue,
    require_perps_gate,
)
from hydra.services.sessions import QuickTradeRequest, StartSessionRequest
from hydra.services.trader import LocalTrader

logger = logging.getLogger(__name__)
audit_logger = get_logger("hydra.audit")

LIVE_ARMED_KEY = "live_armed"
LIVE_CAPITAL_KEY = "live_capital"
LIVE_VENUE = "binance"
RESTRICTIONS_TIMEOUT_S = 10.0

LIVE_ACTIONS = ("live_arming", "live_session_start", "live_order", "credential_delete")
"""The four gated live actions; the executors below are the only live paths (seam #1)."""

LIVE_EXECUTOR_NOTE = (
    "Type the code in the Hydra UI Activity page to approve. The code is single-use "
    "and expires in 10 minutes."
)

LIVE_SESSION_NOTE = (
    "Live sessions keep running under their start's authorization until they are "
    "stopped or the kill switch fires."
)

_UPSERT_SETTING = (
    "INSERT INTO system_config (key, value, updated_at, updated_by) "
    "VALUES ($1, $2::jsonb, now(), $3) "
    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), "
    "updated_by = EXCLUDED.updated_by"
)


# ---------------------------------------------------------------------------
# The armed row
# ---------------------------------------------------------------------------


async def read_live_state(pool: Any) -> dict[str, Any]:
    """The armed state; anything unreadable counts as disarmed (fail-safe, never 503)."""
    row: Any = None
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT value FROM system_config WHERE key = $1", LIVE_ARMED_KEY
                )
        except Exception:
            logger.warning("Live-arming state unreadable; reporting disarmed", exc_info=True)
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


async def positively_disarmed(pool: Any) -> bool:
    """The disarm path's raising read (R31): True only when a successful read shows
    the row absent or explicitly disarmed. A failed read is 503 — never a silent
    ``already_disarmed`` — and content that does not positively read disarmed answers
    False so the disarm write replaces it (toward safety)."""
    require_pool(pool)
    async with db_errors("reading the live-arming state"), pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM system_config WHERE key = $1", LIVE_ARMED_KEY)
    if row is None:
        return True
    raw = row["value"]
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return False
    return not (isinstance(value, dict) and bool(value.get("armed")))


async def read_live_capital(pool: Any) -> float | None:
    """The persisted ``live_capital`` baseline, or None when the row is absent/unusable."""
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT value FROM system_config WHERE key = $1", LIVE_CAPITAL_KEY
            )
        value = json.loads(raw) if isinstance(raw, str) else raw
        capital = float(value)
    except Exception:
        logger.warning("live_capital unreadable", exc_info=True)
        return None
    return capital if capital > 0 else None


async def require_armed(pool: Any) -> dict[str, Any]:
    """The armed state, or the 409 both live create-surfaces and executors answer."""
    state = await read_live_state(pool)
    if not state["armed"]:
        raise service_error(
            409,
            "live_not_armed",
            "Live trading is not armed; arm it through the code-gated ceremony first "
            "(POST /api/live/arm)",
            "invalid",
        )
    return state


async def write_armed_transition(
    pool: Any, *, armed: bool, actor: str, reason: str, live_capital: float | None = None
) -> dict[str, Any]:
    """One audited transaction: the armed row (plus ``live_capital`` when arming) and
    its ``risk_events`` row — the arm ceremony never writes out-of-band SQL (R2).

    Returns the written row (``{armed, by, at}``) so the caller answers what landed.
    """
    require_pool(pool)
    at = datetime.now(UTC).isoformat()
    details: dict[str, Any] = {}
    if live_capital is not None:
        details["live_capital"] = live_capital
    async with (
        db_errors("writing the live-arming state"),
        pool.acquire() as conn,
        conn.transaction(),
    ):
        await conn.execute(
            _UPSERT_SETTING,
            LIVE_ARMED_KEY,
            json.dumps({"armed": armed, "by": actor, "at": at}),
            actor,
        )
        if live_capital is not None:
            await conn.execute(_UPSERT_SETTING, LIVE_CAPITAL_KEY, json.dumps(live_capital), actor)
        await conn.execute(
            "INSERT INTO risk_events (scope, kind, tier, actor, reason, details) "
            "VALUES ('paper', $1, NULL, $2, $3, $4::jsonb)",
            "live_armed" if armed else "live_disarmed",
            actor,
            reason,
            json.dumps(details),
        )
    logger.info("Live gate %s by %s", "armed" if armed else "disarmed", actor)
    return {"armed": armed, "by": actor, "at": at}


# ---------------------------------------------------------------------------
# Trade-only credentials
# ---------------------------------------------------------------------------


def normalize_restrictions(raw: Any) -> dict[str, bool]:
    """Binance ``apiRestrictions`` -> ``{enable_spot_and_margin_trading,
    enable_withdrawals, ip_restrict}`` (absent flags count as disabled)."""
    if not isinstance(raw, dict):
        return {
            "enable_spot_and_margin_trading": False,
            "enable_withdrawals": False,
            "ip_restrict": False,
        }
    return {
        "enable_spot_and_margin_trading": bool(raw.get("enableSpotAndMarginTrading")),
        "enable_withdrawals": bool(raw.get("enableWithdrawals")),
        "ip_restrict": bool(raw.get("ipRestrict")),
    }


def stored_permissions(raw: Any) -> dict[str, Any] | None:
    """The stored ``validated_permissions`` JSONB value (asyncpg returns text), or None."""
    if raw is None or isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def is_trade_capable(raw: Any) -> bool:
    """True when the stored ``validated_permissions`` record a trade-enabled key — spot
    trading for a spot venue, futures trading for a binanceusdm key (the connect-time
    record; the UI flag and the delete-gate's source)."""
    permissions = stored_permissions(raw)
    if permissions is None:
        return False
    return bool(permissions.get("enable_spot_and_margin_trading")) or bool(
        permissions.get("enable_futures")
    )


def _not_live_capable(message: str) -> ServiceError:
    return service_error(409, "credentials_not_live_capable", message, "invalid")


async def require_live_capable_credentials(pool: Any) -> dict[str, bool]:
    """Mainnet + spot-trade-enabled + withdrawals-disabled keys, or the refusal.

    R8: the stored snapshot is only the connect-time record — the permissions are
    RE-FETCHED through the keyed mainnet client here, so a key whose withdrawals were
    enabled since connecting refuses arming. Anything the re-fetch cannot confirm is
    refused (fail-closed); only the nothing-stored / undecryptable cases are 403.
    """
    from cryptography.fernet import InvalidToken

    from hydra.core.encryption import decrypt

    require_pool(pool)
    async with db_errors("reading the stored exchange credentials"), pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT encrypted_key, encrypted_secret, encrypted_passphrase, "
            "validated_environment FROM exchange_credentials WHERE exchange_id = $1",
            LIVE_VENUE,
        )
    if row is None:
        raise service_error(
            403,
            "live_credentials_missing",
            f"No {LIVE_VENUE} credentials are stored; connect trade-only keys in Settings first",
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
            f"The stored {LIVE_VENUE} credentials cannot be decrypted; reconnect them in Settings",
            "invalid",
        ) from None
    if str(row["validated_environment"] or "") != "mainnet":
        raise _not_live_capable(
            f"The stored {LIVE_VENUE} credentials are not validated for mainnet; reconnect "
            "them in Settings with mainnet selected"
        )
    client = build_readonly_client(LIVE_VENUE, creds, testnet=False)
    try:
        raw = await asyncio.wait_for(
            client.fetch_api_restrictions(), timeout=RESTRICTIONS_TIMEOUT_S
        )
    except Exception as exc:
        raise _not_live_capable(
            f"The {LIVE_VENUE} key permissions could not be re-validated: {exc}; nothing was armed"
        ) from exc
    finally:
        try:
            await client.close()
        except Exception:
            logger.debug("Failed to close the permission-check client", exc_info=True)
    restrictions = normalize_restrictions(raw)
    if restrictions["enable_withdrawals"]:
        raise _not_live_capable(
            f"The stored {LIVE_VENUE} key has withdrawals enabled; live trading needs a "
            "trade-only key (disable withdrawals on the key and reconnect it in Settings)"
        )
    if not restrictions["enable_spot_and_margin_trading"]:
        raise _not_live_capable(
            f"The stored {LIVE_VENUE} key cannot trade spot; live trading needs a "
            "trade-enabled key (enable spot trading on the key and reconnect it in Settings)"
        )
    return restrictions


# ---------------------------------------------------------------------------
# The approval seam
# ---------------------------------------------------------------------------


def _backend(deps: Any) -> Any:
    """The ``TraderBackend`` the executors call through, like every route."""
    backend = getattr(deps, "trader", None)
    if backend is not None:
        return backend
    return LocalTrader(getattr(deps, "db_pool", None), None)


async def running_live_sessions(backend: Any) -> list[str]:
    """The ids of the running live sessions (disarm honesty, R10; the delete gate)."""
    sessions = await backend.list_sessions()
    return [
        str(s["session_id"])
        for s in sessions
        if s.get("trading_mode") == "live" and s.get("status") == "running"
    ]


async def session_trading_mode(backend: Any, session_id: str) -> str | None:
    """One listed session's trading mode by id, or None when no such session is listed."""
    for s in await backend.list_sessions():
        if s.get("session_id") == session_id:
            return None if s.get("trading_mode") is None else str(s.get("trading_mode"))
    return None


async def create_live_approval(
    store: ApprovalStore,
    *,
    action: str,
    reason: str,
    params: dict[str, Any],
    requested_by: str,
) -> Approval:
    """Request a gated live action; a different pending request of the same action
    (different params) answers 409 ``approval_conflict`` — the store dedupes identical
    retries into the same approval."""
    for pending in await store.list_pending():
        if pending.action == action and pending.params != params:
            raise service_error(
                409,
                "approval_conflict",
                f"A different {action!r} request is already pending with different "
                "parameters; deny it or let it expire before requesting again",
                "invalid",
                action=action,
            )
    return await store.create(action, reason, params, requested_by=requested_by)


def live_gate_required(where: str) -> ServiceError:
    """The seam-#1 API-surface refusal: the code-gated ceremony is the only live path."""
    return service_error(
        409,
        "live_gate_required",
        f"This surface never trades live; live trading goes through the code-gated "
        f"{where} ceremony",
        "invalid",
    )


def live_approval_store(state: Any) -> ApprovalStore:
    """The engine's approval store with the four live executors registered
    (idempotent), wired exactly like ``routes/activity.py`` wires it."""
    store = getattr(state, "approval_store", None)
    if not isinstance(store, ApprovalStore):

        async def write(event: RiskEventRecord) -> None:
            pool = getattr(state, "db_pool", None)
            if pool is not None:
                await write_approval_event(pool, event)

        store = ApprovalStore(event_writer=write)
        state.approval_store = store
    store.register("live_arming", execute_live_arming)
    store.register("live_session_start", execute_live_session_start)
    store.register("live_order", execute_live_order)
    store.register("credential_delete", execute_credential_delete)
    store.register("live_perps_arming", execute_perps_arming)
    return store


def approval_response(approval: Approval, *, note: str = LIVE_EXECUTOR_NOTE) -> dict[str, Any]:
    """The pinned seam-#9 create-route response: the code flows to the requester."""
    return {
        "status": "approval_required",
        "approval_id": approval.id,
        "code": approval.code,
        "expires_at": approval.expires_at.isoformat(),
        "params": approval.params,
        "note": note,
    }


# ---------------------------------------------------------------------------
# The four executors (seam #1: the only live paths)
# ---------------------------------------------------------------------------


async def execute_live_arming(deps: Any, params: dict[str, Any], actor: str) -> dict[str, Any]:
    """Arm the platform + write ``live_capital`` as one audited transition (R2).

    Re-validates at execution time: the kill switch must be off and the stored keys
    must still be live-capable (R8 re-fetch). An already-armed platform is a clean
    no-op, mirroring the release executor.
    """
    backend = _backend(deps)
    if await backend.kill_switch_active():
        raise service_error(
            409,
            "kill_switch_active",
            "The kill switch is active; release it before arming live trading",
            "invalid",
        )
    pool = require_pool(getattr(deps, "db_pool", None))
    await require_live_capable_credentials(pool)
    reason = str(params.get("reason") or "live arming")
    try:
        capital = float(params["live_capital"])
    except (KeyError, TypeError, ValueError) as exc:
        raise service_error(
            422, "invalid_params", "live_capital must be a positive number", "invalid"
        ) from exc
    if not capital > 0:  # not <= 0: NaN fails every comparison
        raise service_error(
            422, "invalid_params", "live_capital must be a positive number", "invalid"
        )
    state = await read_live_state(pool)
    if state["armed"]:
        return {
            "status": "already_armed",
            "live_armed": True,
            "armed_by": state["armed_by"],
            "armed_at": state["armed_at"],
            "live_capital": await read_live_capital(pool),
            "message": "Live trading is already armed; nothing changed",
        }
    written = await write_armed_transition(
        pool, armed=True, actor=actor, reason=reason, live_capital=capital
    )
    return {
        "status": "armed",
        "live_armed": True,
        "armed_by": written["by"],
        "armed_at": written["at"],
        "live_capital": capital,
        "message": "Live trading is armed; live session starts and manual live orders "
        "now need their own one-time code",
    }


async def execute_live_session_start(
    deps: Any, params: dict[str, Any], actor: str
) -> dict[str, Any]:
    """Start a live session; the service layer's armed check is the fail-closed
    backstop, the kill-switch refusal and the credentials/capital checks are the
    manager's. A perps strategy answers to the separate perps ceremony (P4-E), not the
    spot gate."""
    pool = getattr(deps, "db_pool", None)
    strategy_id = str(params.get("strategy_id") or "")
    if is_perp_strategy(strategy_id):
        await require_perps_gate(pool)
    else:
        await require_armed(pool)
    backend = _backend(deps)
    if await backend.kill_switch_active():
        raise service_error(
            409,
            "kill_switch_active",
            "The kill switch is active; new sessions cannot start",
            "invalid",
        )
    if not strategy_id:
        raise service_error(422, "invalid_params", "strategy_id is required", "invalid")
    try:
        body = StartSessionRequest(
            strategy_id=strategy_id,
            trading_mode="live",
            paper_capital=params.get("paper_capital"),
        )
    except ValidationError as exc:
        raise service_error(422, "invalid_params", str(exc), "invalid") from exc
    started: dict[str, Any] = await backend.start_session(actor, body)
    return started


_ORDER_FIELDS = (
    "symbol",
    "side",
    "order_type",
    "quantity",
    "price",
    "take_profit",
    "stop_loss",
    "trailing_stop_pct",
    "session_id",
)


async def execute_live_order(deps: Any, params: dict[str, Any], actor: str) -> dict[str, Any]:
    """One manual live order on a running live session (the quick-trade seam). A live
    session on the perps venue answers to the separate perps ceremony (P4-E)."""
    pool = getattr(deps, "db_pool", None)
    backend = _backend(deps)
    session_id = str(params.get("session_id") or "")
    target = next(
        (s for s in await backend.list_sessions() if s.get("session_id") == session_id), None
    )
    if is_perp_venue(None if target is None else target.get("exchange_id")):
        await require_perps_gate(pool)
    else:
        await require_armed(pool)
    if await backend.kill_switch_active():
        raise service_error(
            409,
            "kill_switch_active",
            "The kill switch is active; manual live orders cannot be placed",
            "invalid",
        )
    if target is None:
        raise service_error(
            404, "session_not_found", f"Session {session_id} not found", "not_found"
        )
    if target.get("trading_mode") != "live" or target.get("status") != "running":
        raise service_error(
            409,
            "not_a_live_session",
            f"Session {session_id} is not a running live session",
            "invalid",
        )
    try:
        order = QuickTradeRequest(**{k: params[k] for k in _ORDER_FIELDS if k in params})
    except ValidationError as exc:
        raise service_error(422, "invalid_params", str(exc), "invalid") from exc
    placed: dict[str, Any] = await backend.quick_trade(order)
    return placed


async def execute_credential_delete(
    deps: Any, params: dict[str, Any], actor: str
) -> dict[str, Any]:
    """Delete trade-capable stored credentials after the code; refuses while any live
    session runs — in the split layout the trader's keyed pool entry outlives an
    API-side evict (must-fix #5)."""
    exchange_id = str(params.get("exchange_id") or "")
    pool = require_pool(getattr(deps, "db_pool", None))
    async with db_errors("reading the stored exchange credentials"), pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM exchange_credentials WHERE exchange_id = $1", exchange_id
        )
    if row is None:
        raise service_error(
            404,
            "credentials_not_found",
            f"No credentials stored for {exchange_id}",
            "not_found",
        )
    running = await running_live_sessions(_backend(deps))
    if running:
        raise service_error(
            409,
            "credential_delete_gated",
            f"Credentials cannot be deleted while a live session runs "
            f"({', '.join(running)}); stop it first",
            "invalid",
            running_live_sessions=running,
        )
    async with db_errors("deleting exchange credentials"), pool.acquire() as conn:
        await conn.execute("DELETE FROM exchange_credentials WHERE exchange_id = $1", exchange_id)
    clients = getattr(deps, "_exchange_clients", None)
    old_client = clients.pop(exchange_id, None) if isinstance(clients, dict) else None
    if old_client is not None:
        try:
            await old_client.close()
        except Exception:
            logger.debug("Failed to close exchange client for %s", exchange_id)
    for attr in ("exchange_credentials", "exchange_credential_errors"):
        cache = getattr(deps, attr, None)
        if isinstance(cache, dict):
            cache.pop(exchange_id, None)
    audit_logger.info("audit", action="exchange_disconnected", actor=actor, exchange_id=exchange_id)
    return {
        "exchange_id": exchange_id,
        "deleted": True,
        "message": f"Disconnected from {exchange_id}",
    }
