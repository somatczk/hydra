"""Live-gate tools: ``live_session_start`` and ``live_order`` (the code-gated actions).

Both mirror ``kill_switch_release`` exactly: the tool creates an approval in the
engine's ``ApprovalStore`` and answers ``approval_required`` with the one-time code —
the code flows to Claude, a human types it in the Hydra UI Activity page, and only then
does the executor run, attributed to ``service:mcp`` (approved by the human). Each tool
fail-fasts the armed gate so Claude learns the state before a code is issued; the
executor re-validates everything again at execution time.

There is deliberately no ``live_arm``/``live_disarm`` tool (arming is the operator's
Settings ceremony, ``POST /api/live/arm``; disarm is ungated) and no dedicated live
status tool — the armed state rides ``risk_live_status``'s ``live_armed`` field.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hydra.dashboard.mcp import server
from hydra.dashboard.mcp.tools import _deps
from hydra.services.approvals import APPROVAL_ACTOR
from hydra.services.errors import ServiceError, service_error
from hydra.services.live_gate import (
    create_live_approval,
    live_approval_store,
    require_armed,
)
from hydra.services.perps_gate import is_perp_strategy, is_perp_venue, require_perps_gate
from hydra.services.sessions import QuickTradeRequest


class _Reason(BaseModel):
    """A live-action reason a human can read: the same bounds as the kill switch."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)


class _SessionStart(_Reason):
    strategy_id: str = Field(min_length=1)


@server.tool(name="live_session_start", structured_output=False)
async def live_session_start(strategy_id: str, reason: str) -> str:
    """Ask to start a LIVE session for a strategy. GATED: this starts nothing — it
    creates an approval and answers ``approval_required`` with a one-time code.

    A human must type that code on the Hydra UI Activity page; only then does the live
    session start, attributed to ``service:mcp``. The platform must be armed first
    (the operator's Settings ceremony); the code is single-use and expires in
    10 minutes. Strategy orders inside the started session then flow under this
    start's standing authorization.
    """
    try:
        body = _SessionStart(strategy_id=strategy_id, reason=reason)
        if is_perp_strategy(body.strategy_id):
            await require_perps_gate(_deps.db_pool())  # the separate perps ceremony (P4-E)
        else:
            await require_armed(_deps.db_pool())
        params: dict[str, str] = {"strategy_id": body.strategy_id, "reason": body.reason}
        store = live_approval_store(_deps.engine_state())
        approval = await create_live_approval(
            store,
            action="live_session_start",
            reason=body.reason,
            params=dict(params),
            requested_by=APPROVAL_ACTOR,
        )
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
                "Type the code in the Hydra UI Activity page to approve the live session "
                "start. The code is single-use and expires in 10 minutes."
            ),
        }
    )


@server.tool(name="live_order", structured_output=False)
async def live_order(
    session_id: str,
    side: str,
    quantity: str,
    symbol: str = "BTCUSDT",
    order_type: str = "MARKET",
    price: str | None = None,
    take_profit: str | None = None,
    stop_loss: str | None = None,
    trailing_stop_pct: str | None = None,
    reason: str = "live manual order",
) -> str:
    """Ask to place ONE manual LIVE order on a running live session. GATED: this
    places nothing — it creates an approval and answers ``approval_required`` with a
    one-time code a human must type on the Hydra UI Activity page.

    ``quantity``/``price``/``take_profit``/``stop_loss``/``trailing_stop_pct`` are
    decimal strings. SL/TP (or a trailing stop) are placed reduce-only when the entry
    fills; the order is refused by the same pre-trade checks the quick-trade route
    applies. Strategy orders inside the session do not go through this tool (they
    flow under the session start's standing authorization).
    """
    try:
        order = QuickTradeRequest(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            take_profit=take_profit,
            stop_loss=stop_loss,
            trailing_stop_pct=trailing_stop_pct,
            session_id=session_id,
        )
        _reason = _Reason(reason=reason).reason
        sessions = await _deps.trader().list_sessions()
        target = next((s for s in sessions if s.get("session_id") == session_id), None)
        if is_perp_venue(None if target is None else target.get("exchange_id")):
            await require_perps_gate(_deps.db_pool())  # the separate perps ceremony (P4-E)
        else:
            await require_armed(_deps.db_pool())
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
        store = live_approval_store(_deps.engine_state())
        approval = await create_live_approval(
            store,
            action="live_order",
            reason=_reason,
            params={**order.model_dump(mode="json"), "reason": _reason},
            requested_by=APPROVAL_ACTOR,
        )
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
                "Type the code in the Hydra UI Activity page to approve the live order. "
                "The code is single-use and expires in 10 minutes."
            ),
        }
    )
