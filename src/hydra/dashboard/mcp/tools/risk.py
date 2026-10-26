"""Risk tools (live status, status, positions, kill switch on / gated release).

``risk_live_status`` and ``risk_status`` mirror the REST ``/api/risk`` reads (unknown
values stay unknown); ``positions`` reads every running session's held positions through
the session service. ``kill_switch_on`` is ungated (fail-safe: it takes the same
``mgr.stop_all`` path the REST route takes and latches), while ``kill_switch_release``
is the ONE gated tool: it creates an approval in the engine's ``ApprovalStore`` and
answers ``approval_required`` with the one-time code — the release itself runs only when
a human types that code in the Hydra UI Activity page, re-validating the switch state at
execution time (an already-released switch is a clean no-op).
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hydra.dashboard.mcp import server
from hydra.dashboard.mcp.tools import _deps
from hydra.dashboard.security import audit
from hydra.risk.circuit_breakers import CircuitBreakerRestrictions
from hydra.risk.service import RiskService
from hydra.risk.state import RiskEventRecord
from hydra.services import sessions as sessions_svc
from hydra.services.approvals import (
    APPROVAL_ACTOR,
    ApprovalStore,
    write_approval_event,
)
from hydra.services.errors import DB_ERRORS, ServiceError, service_error
from hydra.services.sessions import KillSwitchNotPersistedError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared reads (mirroring routes/risk.py)
# ---------------------------------------------------------------------------


def _risk_service(mgr: Any) -> RiskService | None:
    """The engine's RiskService, exposed as ``SessionManager.risk``."""
    svc = getattr(mgr, "risk", None) if mgr is not None else None
    return svc if isinstance(svc, RiskService) else None


def _require_risk(mgr: Any) -> RiskService:
    svc = _risk_service(mgr)
    if svc is None:
        raise _deps.tool_error(
            service_error(
                503,
                "risk_service_unavailable",
                "The trading engine's risk service is not running",
                "unavailable",
            )
        )
    if not svc.available:
        raise _deps.tool_error(
            service_error(
                503,
                "risk_state_unavailable",
                "Risk state could not be loaded or persisted; opening orders are refused",
                "unavailable",
            )
        )
    return svc


def _f(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _restrictions(r: CircuitBreakerRestrictions) -> dict[str, Any]:
    return {
        "active_tier": r.tier,
        "can_open_new_trades": r.can_open_new_trades,
        "size_multiplier": float(r.position_size_multiplier),
        "must_flatten": r.must_flatten,
        "is_shutdown": r.is_shutdown,
        "daily_halt_until": r.daily_halt_until,
        "consecutive_pause_until": r.consecutive_pause_until,
    }


def _breakers(svc: RiskService) -> list[dict[str, Any]]:
    drawdown_pct = round(float(svc.account().drawdown) * 100, 2)
    return [
        {
            "tier": b.tier,
            "label": b.label,
            "threshold": f"{float(b.threshold) * 100:.2f}% drawdown",
            "threshold_pct": round(float(b.threshold) * 100, 4),
            "current_value": drawdown_pct,
            "status": b.status,
            "active_since": b.active_since,
            "requires_manual_review": b.requires_manual_review,
        }
        for b in svc.breakers()
    ]


@server.tool(name="risk_live_status", structured_output=False)
async def risk_live_status() -> str:
    """Kill switch, running-session count and breaker restrictions — the operating
    picture. Unknown values are reported as unknown, never guessed."""
    mgr = _deps.session_manager()
    svc = _risk_service(mgr)
    kill_switch: bool | None = None
    pool = _deps.db_pool()
    if pool is not None:
        row: Any = None
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT kill_switch_active FROM risk_config WHERE scope = 'global'"
                )
        except DB_ERRORS:
            logger.warning("Kill switch state unknown: risk_config read failed", exc_info=True)
            row = None
        if row is not None:
            kill_switch = bool(row["kill_switch_active"])
    if svc is not None and svc.kill_switch_active():
        kill_switch = True
    running: int | None = None
    if mgr is not None:
        running = sum(1 for s in mgr.list_sessions() if s.status == "running")
    live = svc is not None and svc.available
    return _deps.json_text(
        {
            "kill_switch_active": kill_switch,
            "kill_switch_state": (
                "unknown" if kill_switch is None else ("active" if kill_switch else "inactive")
            ),
            "running_sessions": running,
            "circuit_breaker_restrictions": (
                _restrictions(svc.restrictions()) if svc is not None and live else None
            ),
            "circuit_breakers_state": "live" if live else "unavailable",
        }
    )


@server.tool(name="risk_status", structured_output=False)
async def risk_status() -> str:
    """Paper-account risk status: drawdown, daily loss, consecutive losses and the four
    circuit-breaker tiers with their restrictions (the REST /api/risk/status read)."""
    svc = _require_risk(_deps.session_manager())
    view = svc.account()
    cfg = view.config
    start = view.day_start_equity
    daily_loss = (
        max(Decimal("0"), start - view.equity)
        if start is not None and view.equity is not None
        else None
    )
    return _deps.json_text(
        {
            "current_drawdown": round(float(view.drawdown) * 100, 2),
            "max_drawdown_limit": float(cfg.max_drawdown_pct) * 100,
            "daily_loss": None if daily_loss is None else round(float(daily_loss), 2),
            "daily_loss_limit": (
                None if start is None else round(float(start * cfg.max_daily_loss_pct), 2)
            ),
            "equity": _f(view.equity),
            "peak_equity": _f(view.peak_equity),
            "day_start_equity": _f(start),
            "consecutive_losses": view.consecutive_losses,
            "max_consecutive_losses": cfg.max_consecutive_losses,
            "circuit_breakers": _breakers(svc),
            "circuit_breakers_state": "live",
            "restrictions": _restrictions(view.restrictions),
        }
    )


@server.tool(name="positions", structured_output=False)
async def positions() -> str:
    """The held positions of every running session (symbol, direction, quantity, entry
    price, unrealized PnL) with the session and strategy that holds them."""
    try:
        mgr = sessions_svc.require_manager(_deps.session_manager())
        held = []
        for s in mgr.list_sessions():
            if s.status != "running":
                continue
            detail = await sessions_svc.get_session_detail(mgr, s.session_id)
            held.append(
                {
                    "session_id": s.session_id,
                    "strategy_id": s.strategy_id,
                    "positions": detail["positions"],
                }
            )
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc
    return _deps.json_text(held)


# ---------------------------------------------------------------------------
# Kill switch: ungated latch, gated release
# ---------------------------------------------------------------------------


@server.tool(name="kill_switch_on", structured_output=False)
async def kill_switch_on(reason: str = "kill switch (mcp)") -> str:
    """Latch the kill switch NOW and flatten + stop every running session (ungated,
    fail-safe — the same path the REST route takes).

    New sessions and opening orders are refused while it is latched. Releasing is the
    gated action: call ``kill_switch_release`` and a human must approve it with a
    one-time code in the Hydra UI.
    """
    try:
        body = await sessions_svc.activate_kill_switch(
            _deps.session_manager(), APPROVAL_ACTOR, reason
        )
    except ServiceError as exc:
        if isinstance(exc, KillSwitchNotPersistedError):
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            audit(
                _deps.MCP_PRINCIPAL,
                "kill_switch_on",
                sessions_stopped=detail.get("sessions_stopped"),
                positions_remaining=detail.get("positions_remaining"),
                persisted=detail.get("persisted"),
            )
        raise _deps.tool_error(exc) from exc
    audit(
        _deps.MCP_PRINCIPAL,
        "kill_switch_on",
        sessions_stopped=body["sessions_stopped"],
        positions_remaining=body["positions_remaining"],
        persisted=body["persisted"],
    )
    return _deps.json_text(body)


class _ReleaseReason(BaseModel):
    """The gated release's ask: a reason a human can read before deciding."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)


async def _release_executor(deps: Any, params: dict[str, Any], actor: str) -> dict[str, Any]:
    """Re-validate the switch, then release as the MCP service (seam 5).

    Runs only after a human typed the code, so it must never leak an exception for a
    benign state: an already-released switch answers a clean no-op result.
    """
    mgr = getattr(deps, "session_manager", None)
    if mgr is None:
        raise service_error(
            503, "engine_not_ready", "Session manager not initialized", "unavailable"
        )
    reason = str(params.get("reason") or "kill switch release")
    if not mgr.kill_switch_active():
        return {
            "status": "already_released",
            "active": False,
            "message": "The kill switch was already released; nothing changed",
        }
    return await sessions_svc.release_kill_switch(
        getattr(deps, "db_pool", None), mgr, actor, reason
    )


def _approval_store() -> ApprovalStore:
    """The engine's approval store, wired exactly like ``routes/activity.py`` wires it,
    with this module's release executor registered (idempotent)."""
    state = _deps.engine_state()
    store = getattr(state, "approval_store", None)
    if not isinstance(store, ApprovalStore):

        async def write(event: RiskEventRecord) -> None:
            pool = getattr(state, "db_pool", None)
            if pool is not None:
                await write_approval_event(pool, event)

        store = ApprovalStore(event_writer=write)
        state.approval_store = store
    store.register("kill_switch_release", _release_executor)
    return store


@server.tool(name="kill_switch_release", structured_output=False)
async def kill_switch_release(reason: str) -> str:
    """Ask to release the kill switch. GATED: this does NOT release anything — it
    creates an approval and answers ``approval_required`` with a one-time code.

    A human must type that code on the Hydra UI Activity page; only then does the
    release run, attributed to ``service:mcp``. The code is single-use and expires in
    10 minutes; if it expires, call this tool again for a fresh one.
    """
    try:
        body = _ReleaseReason(reason=reason)
        store = _approval_store()
        approval = await store.create("kill_switch_release", body.reason, {"reason": body.reason})
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc
    except ValidationError as exc:
        raise _deps.invalid_arguments(exc) from exc
    return _deps.json_text(
        {
            "status": "approval_required",
            "approval_id": approval.id,
            "code": approval.code,
            "expires_at": approval.expires_at.isoformat(),
            "note": (
                "Type the code in the Hydra UI Activity page to approve the release. "
                "The code is single-use and expires in 10 minutes."
            ),
        }
    )
