"""Risk tools (live status, status, positions, kill switch on / gated release).

``risk_live_status`` and ``risk_status`` mirror the REST ``/api/risk`` reads (unknown
values stay unknown); ``positions`` reads every running session's held positions through
the trader backend. ``kill_switch_on`` is ungated (fail-safe: it takes the same
``mgr.stop_all`` path the REST route takes and latches), while ``kill_switch_release``
is the ONE gated tool: it creates an approval in the engine's ``ApprovalStore`` and
answers ``approval_required`` with the one-time code — the release itself runs only when
a human types that code in the Hydra UI Activity page, re-validating the switch state at
execution time inside the backend op (an already-released switch is a clean no-op).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hydra.dashboard.mcp import server
from hydra.dashboard.mcp.tools import _deps
from hydra.dashboard.security import audit
from hydra.risk.state import RiskEventRecord
from hydra.services.approvals import (
    APPROVAL_ACTOR,
    ApprovalStore,
    write_approval_event,
)
from hydra.services.errors import ServiceError
from hydra.services.sessions import KillSwitchNotPersistedError

# ---------------------------------------------------------------------------
# Reads (mirroring routes/risk.py)
# ---------------------------------------------------------------------------


@server.tool(name="risk_live_status", structured_output=False)
async def risk_live_status() -> str:
    """Kill switch, running-session count and breaker restrictions — the operating
    picture. Unknown values are reported as unknown, never guessed."""
    return _deps.json_text(await _deps.trader().live_status())


@server.tool(name="risk_status", structured_output=False)
async def risk_status() -> str:
    """Paper-account risk status: drawdown, daily loss, consecutive losses and the four
    circuit-breaker tiers with their restrictions (the REST /api/risk/status read)."""
    try:
        return _deps.json_text(await _deps.trader().risk_status())
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc


@server.tool(name="positions", structured_output=False)
async def positions() -> str:
    """The held positions of every running session (symbol, direction, quantity, entry
    price, unrealized PnL) with the session and strategy that holds them."""
    try:
        held = await _deps.trader().positions()
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc
    return _deps.json_text(held)


# ---------------------------------------------------------------------------
# Kill switch: ungated latch, gated release
# ---------------------------------------------------------------------------


class _Reason(BaseModel):
    """A kill-switch reason a human can read: the latch and release share the bounds."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)


@server.tool(name="kill_switch_on", structured_output=False)
async def kill_switch_on(reason: str = "kill switch (mcp)") -> str:
    """Latch the kill switch NOW and flatten + stop every running session (ungated,
    fail-safe — the same path the REST route takes).

    New sessions and opening orders are refused while it is latched. Releasing is the
    gated action: call ``kill_switch_release`` and a human must approve it with a
    one-time code in the Hydra UI.
    """
    try:
        reason = _Reason(reason=reason).reason
        body = await _deps.trader().activate_kill_switch(APPROVAL_ACTOR, reason)
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
    except ValidationError as exc:
        raise _deps.invalid_arguments(exc) from exc
    audit(
        _deps.MCP_PRINCIPAL,
        "kill_switch_on",
        sessions_stopped=body["sessions_stopped"],
        positions_remaining=body["positions_remaining"],
        persisted=body["persisted"],
    )
    return _deps.json_text(body)


async def _release_executor(deps: Any, params: dict[str, Any], actor: str) -> dict[str, Any]:
    """Release through the backend (seam 5), re-validating the switch inside the op.

    Runs only after a human typed the code, so it must never leak an exception for a
    benign state: an already-released switch answers a clean no-op result.
    """
    reason = str(params.get("reason") or "kill switch release")
    backend = _deps.trader_from(deps)
    return await backend.release_kill_switch(actor, reason)


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
        body = _Reason(reason=reason)
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
