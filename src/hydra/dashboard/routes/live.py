"""The live-gate API: the armed-state read, the arm ceremony and the ungated disarm.

Model (c): ``live_armed`` is a persisted ``system_config`` row turned ON only through
the code-gated ``live_arming`` approval — no REST path writes it directly — and OFF
ungated (disarm is toward safety, audited). The four create routes here issue the
challenge and answer the pinned seam-#9 shape (``{status: 'approval_required',
approval_id, code, expires_at, params, note}``); the human then approves through the
existing ``POST /api/activity/approvals/{id}/approve`` with that code, and the executor
in :mod:`hydra.services.live_gate` runs — re-validating everything at execution time.
Only a ``user``/``dev`` principal can request a live action (the UI's L4 dialogs).

Disarm is honest (R10): the response names every running live session — they keep
running under their start's authorization until stopped or the kill switch fires —
and every pending live-arming approval that can still re-arm on its code. The armed
row is read with a raising read on the disarm path: an unreadable row is 503, never
a silent ``already_disarmed``.

The perps gate (P4-E) rides the same shapes beside the spot gate: ``/perps/status``,
``/perps/arm`` and ``/perps/disarm`` are the separate code-gated ceremony for
leveraged USD⌂-M trading — its own ``perps_armed``/``perps_capital`` rows, its own
``live_perps_arming`` executor, and its own fail-closed futures-permission re-fetch.
The session-start and order create routes check whichever gate the target's venue
answers to; arming spot never arms perps.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from hydra.dashboard.routes._db import (
    ServiceErrorRoute,
    api_error,
    db_errors_to_503,
    request_pool,
    request_trader,
)
from hydra.dashboard.security import audit, current_principal
from hydra.services.live_gate import (
    LIVE_SESSION_NOTE,
    approval_response,
    create_live_approval,
    is_trade_capable,
    live_approval_store,
    positively_disarmed,
    read_live_capital,
    read_live_state,
    require_armed,
    running_live_sessions,
    write_armed_transition,
)
from hydra.services.perps_gate import (
    PERPS_SESSION_NOTE,
    is_perp_strategy,
    is_perp_venue,
    positively_perps_disarmed,
    read_perps_capital,
    read_perps_state,
    require_perps_gate,
    running_live_perps_sessions,
    write_perps_armed_transition,
)
from hydra.services.sessions import QuickTradeRequest

router = APIRouter(prefix="/api/live", tags=["live"], route_class=ServiceErrorRoute)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class LiveStatusResponse(BaseModel):
    """One source for the armed state; ``/api/system/config`` does not duplicate it."""

    live_armed: bool
    armed_by: str | None = None
    armed_at: str | None = None
    live_capital: float | None = None
    running_live_sessions: list[str] = Field(default_factory=list)
    running_live_sessions_count: int = 0


class DisarmResponse(LiveStatusResponse):
    status: str
    message: str
    pending_live_approvals: list[str] = Field(default_factory=list)


class ApprovalRequiredResponse(BaseModel):
    """The pinned seam-#9 create-route shape, identical for all four live actions."""

    status: str
    approval_id: str
    code: str
    expires_at: str
    params: dict[str, Any]
    note: str


class ArmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    live_capital: float = Field(gt=0, allow_inf_nan=False)
    reason: str | None = Field(None, min_length=3, max_length=500)


class DisarmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)


class LiveSessionStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: str = Field(min_length=1)
    paper_capital: float | None = Field(None, gt=0, allow_inf_nan=False)
    reason: str = Field(min_length=3, max_length=500)


class LiveOrderRequest(QuickTradeRequest):
    """The quick-trade fields plus the review reason; the executor revalidates the
    order model from the approval's params."""

    reason: str = Field(min_length=3, max_length=500)


class CredentialDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exchange_id: str = Field(min_length=1)
    reason: str = Field(min_length=3, max_length=500)


class PerpsStatusResponse(BaseModel):
    """The perps gate's state; one source, mirroring the spot ``/api/live/status``."""

    perps_armed: bool
    armed_by: str | None = None
    armed_at: str | None = None
    perps_capital: float | None = None
    running_live_perps_sessions: list[str] = Field(default_factory=list)
    running_live_perps_sessions_count: int = 0


class PerpsDisarmResponse(PerpsStatusResponse):
    status: str
    message: str
    pending_perps_approvals: list[str] = Field(default_factory=list)


class PerpsArmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    perps_capital: float = Field(gt=0, allow_inf_nan=False)
    reason: str | None = Field(None, min_length=3, max_length=500)


class PerpsDisarmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _actor(request: Request) -> str:
    principal = getattr(request.state, "principal", None)
    name = getattr(principal, "name", None)
    kind = getattr(principal, "kind", None)
    if name:
        return f"{kind}:{name}" if kind else str(name)
    return "anonymous"


def _requester(request: Request) -> str:
    """The principal as an actor string, after refusing principals that cannot ask."""
    principal = current_principal(request)
    if principal.kind not in ("user", "dev"):
        raise api_error(
            403,
            "principal_not_allowed",
            "Only a signed-in person can request live actions; service principals cannot",
            "forbidden",
        )
    return f"{principal.kind}:{principal.name}"


# ---------------------------------------------------------------------------
# The armed state
# ---------------------------------------------------------------------------


@router.get("/status", response_model=LiveStatusResponse)
async def live_status(request: Request) -> dict[str, Any]:
    """The armed state, the ``live_capital`` baseline and the running live sessions."""
    pool = request_pool(request)
    state = await read_live_state(pool)
    running = await running_live_sessions(request_trader(request))
    return {
        "live_armed": state["armed"],
        "armed_by": state["armed_by"],
        "armed_at": state["armed_at"],
        "live_capital": await read_live_capital(pool),
        "running_live_sessions": running,
        "running_live_sessions_count": len(running),
    }


@router.post("/arm", response_model=ApprovalRequiredResponse)
async def arm(body: ArmRequest, request: Request) -> dict[str, Any]:
    """Request the live arming ceremony: the ``live_capital`` baseline is written as
    part of arming (one audited transition, R2) once the code is typed.

    The executor re-fetches the Binance key permissions live (R8) and refuses unless
    the keys are mainnet, trade-enabled and withdrawal-disabled.
    """
    actor = _requester(request)
    reason = body.reason or "arm live trading"
    params = {"live_capital": body.live_capital, "reason": reason}
    store = live_approval_store(request.app.state)
    approval = await create_live_approval(
        store, action="live_arming", reason=reason, params=params, requested_by=actor
    )
    return approval_response(approval)


@router.post("/disarm", response_model=DisarmResponse)
async def disarm(body: DisarmRequest, request: Request) -> dict[str, Any]:
    """Disarm directly — ungated toward safety, audited — and report honestly.

    Running live sessions are named and keep running under their start's
    authorization (R10); stopping a session and the kill switch stay ungated.
    The armed row is read with a raising read: an unreadable row is 503, never a
    silent ``already_disarmed``. Pending live-arming approvals are named too — a
    surviving arm code can still re-arm after this disarm.
    """
    pool = request_pool(request)
    status = "already_disarmed"
    if not await positively_disarmed(pool):
        await write_armed_transition(pool, armed=False, actor=_actor(request), reason=body.reason)
        status = "disarmed"
    running = await running_live_sessions(request_trader(request))
    pending = [
        a.id
        for a in await live_approval_store(request.app.state).list_pending()
        if a.action == "live_arming"
    ]
    audit(
        getattr(request.state, "principal", None),
        "live_disarmed",
        reason=body.reason,
        running_live_sessions=len(running),
        pending_live_approvals=len(pending),
    )
    message = (
        f"Live trading is disarmed. {len(running)} live session(s) keep running under "
        f"their start's authorization. {LIVE_SESSION_NOTE}"
    )
    if pending:
        message += (
            f" {len(pending)} pending live-arming approval(s) requested before this "
            "disarm can still re-arm on their code; deny them in the Activity review."
        )
    return {
        "status": status,
        "live_armed": False,
        "armed_by": None,
        "armed_at": None,
        "live_capital": await read_live_capital(pool),
        "running_live_sessions": running,
        "running_live_sessions_count": len(running),
        "pending_live_approvals": pending,
        "message": message,
    }


# ---------------------------------------------------------------------------
# The perps gate (P4-E): the separate code-gated ceremony for leveraged trading
# ---------------------------------------------------------------------------


@router.get("/perps/status", response_model=PerpsStatusResponse)
async def perps_status(request: Request) -> dict[str, Any]:
    """The perps armed state, the ``perps_capital`` baseline and the running live perps
    sessions — a separate row from the spot gate's; arming spot never arms perps."""
    pool = request_pool(request)
    state = await read_perps_state(pool)
    running = await running_live_perps_sessions(request_trader(request))
    return {
        "perps_armed": state["armed"],
        "armed_by": state["armed_by"],
        "armed_at": state["armed_at"],
        "perps_capital": await read_perps_capital(pool),
        "running_live_perps_sessions": running,
        "running_live_perps_sessions_count": len(running),
    }


@router.post("/perps/arm", response_model=ApprovalRequiredResponse)
async def perps_arm(body: PerpsArmRequest, request: Request) -> dict[str, Any]:
    """Request the perps arming ceremony: the ``perps_capital`` baseline is written as
    part of arming (one audited transition) once the code is typed.

    The executor re-fetches the connected key's futures permission live and refuses
    unless ``enableFutures`` reads true with withdrawals disabled — any failure refuses
    the arm (fail-closed).
    """
    actor = _requester(request)
    reason = body.reason or "arm perps trading"
    params = {"perps_capital": body.perps_capital, "reason": reason}
    store = live_approval_store(request.app.state)
    approval = await create_live_approval(
        store, action="live_perps_arming", reason=reason, params=params, requested_by=actor
    )
    return approval_response(approval)


@router.post("/perps/disarm", response_model=PerpsDisarmResponse)
async def perps_disarm(body: PerpsDisarmRequest, request: Request) -> dict[str, Any]:
    """Disarm perps directly — ungated toward safety, audited — and report honestly.

    Running live perp sessions are named and keep running under their start's
    authorization (the R10 pattern); the spot gate is untouched by this ceremony. The
    armed row is read with a raising read: an unreadable row is 503, never a silent
    ``already_disarmed``. Pending perps-arming approvals are named too — a surviving arm
    code can still re-arm after this disarm.
    """
    pool = request_pool(request)
    status = "already_disarmed"
    if not await positively_perps_disarmed(pool):
        await write_perps_armed_transition(
            pool, armed=False, actor=_actor(request), reason=body.reason
        )
        status = "disarmed"
    running = await running_live_perps_sessions(request_trader(request))
    pending = [
        a.id
        for a in await live_approval_store(request.app.state).list_pending()
        if a.action == "live_perps_arming"
    ]
    audit(
        getattr(request.state, "principal", None),
        "perps_disarmed",
        reason=body.reason,
        running_live_perps_sessions=len(running),
        pending_perps_approvals=len(pending),
    )
    message = (
        f"Perps trading is disarmed. {len(running)} live perp session(s) keep running "
        f"under their start's authorization. {PERPS_SESSION_NOTE}"
    )
    if pending:
        message += (
            f" {len(pending)} pending perps-arming approval(s) requested before this "
            "disarm can still re-arm on their code; deny them in the Activity review."
        )
    return {
        "status": status,
        "perps_armed": False,
        "armed_by": None,
        "armed_at": None,
        "perps_capital": await read_perps_capital(pool),
        "running_live_perps_sessions": running,
        "running_live_perps_sessions_count": len(running),
        "pending_perps_approvals": pending,
        "message": message,
    }


# ---------------------------------------------------------------------------
# The per-action code gates
# ---------------------------------------------------------------------------


@router.post("/session-start", response_model=ApprovalRequiredResponse)
async def session_start(body: LiveSessionStartRequest, request: Request) -> dict[str, Any]:
    """Request a live session start: its own one-time code on top of the armed gate.

    A perps strategy answers to the separate perps ceremony (P4-E), not the spot gate.
    """
    actor = _requester(request)
    if is_perp_strategy(body.strategy_id):
        await require_perps_gate(request_pool(request))
    else:
        await require_armed(request_pool(request))
    params: dict[str, Any] = {"strategy_id": body.strategy_id, "reason": body.reason}
    if body.paper_capital is not None:
        params["paper_capital"] = body.paper_capital
    store = live_approval_store(request.app.state)
    approval = await create_live_approval(
        store,
        action="live_session_start",
        reason=body.reason,
        params=params,
        requested_by=actor,
    )
    return approval_response(approval)


@router.post("/order", response_model=ApprovalRequiredResponse)
async def live_order(body: LiveOrderRequest, request: Request) -> dict[str, Any]:
    """Request one manual live order on a running live session (per-order code). A live
    session on the perps venue answers to the separate perps ceremony (P4-E)."""
    actor = _requester(request)
    target = next(
        (
            s
            for s in await request_trader(request).list_sessions()
            if s.get("session_id") == body.session_id
        ),
        None,
    )
    if is_perp_venue(None if target is None else target.get("exchange_id")):
        await require_perps_gate(request_pool(request))
    else:
        await require_armed(request_pool(request))
    if target is None:
        raise api_error(
            404, "session_not_found", f"Session {body.session_id} not found", "not_found"
        )
    if target.get("trading_mode") != "live" or target.get("status") != "running":
        raise api_error(
            409,
            "not_a_live_session",
            f"Session {body.session_id} is not a running live session",
            "invalid",
        )
    store = live_approval_store(request.app.state)
    approval = await create_live_approval(
        store,
        action="live_order",
        reason=body.reason,
        params=body.model_dump(mode="json"),
        requested_by=actor,
    )
    return approval_response(approval)


@router.post("/credential-delete", response_model=ApprovalRequiredResponse)
async def credential_delete(body: CredentialDeleteRequest, request: Request) -> dict[str, Any]:
    """Request deleting trade-capable stored credentials (read-only keys delete
    directly through ``DELETE /api/system/exchanges/{id}/connect`` while no live
    session runs; under a running live session even null-permission keys need this
    ceremony — the direct path refuses them too)."""
    actor = _requester(request)
    pool = request_pool(request)
    async with db_errors_to_503("reading exchange credentials"), pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT validated_permissions FROM exchange_credentials WHERE exchange_id = $1",
            body.exchange_id,
        )
    if row is None:
        raise api_error(
            404,
            "credentials_not_found",
            f"No credentials stored for {body.exchange_id}",
            "not_found",
        )
    if not is_trade_capable(row["validated_permissions"]) and not await running_live_sessions(
        request_trader(request)
    ):
        raise api_error(
            409,
            "credential_delete_not_gated",
            f"The stored {body.exchange_id} keys cannot trade; delete them directly with "
            f"DELETE /api/system/exchanges/{body.exchange_id}/connect",
            "invalid",
        )
    store = live_approval_store(request.app.state)
    approval = await create_live_approval(
        store,
        action="credential_delete",
        reason=body.reason,
        params={"exchange_id": body.exchange_id, "reason": body.reason},
        requested_by=actor,
    )
    return approval_response(approval)
