"""The live-gate API: the armed-state read, the arm ceremony and the ungated disarm.

Model (c): ``live_armed`` is a persisted ``system_config`` row turned ON only through
the code-gated ``live_arming`` approval — no REST path writes it directly — and OFF
ungated (disarm is toward safety, audited). The four create routes here issue the
challenge and answer the pinned seam-#9 shape (``{status: 'approval_required',
approval_id, code, expires_at, params, note}``); the human then approves through the
existing ``POST /api/activity/approvals/{id}/approve`` with that code, and the executor
in :mod:`hydra.services.live_gate` runs — re-validating everything at execution time.
Only a ``user``/``dev`` principal can request a live action (the UI's L4 dialogs).

Disarm is honest (R10): the response names every running live session, and they keep
running under their start's authorization — stopping a session and the kill switch stay
ungated.
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
    read_live_capital,
    read_live_state,
    require_armed,
    running_live_sessions,
    write_armed_transition,
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


async def _session(request: Request, session_id: str) -> dict[str, Any]:
    """The listed session by id, or the 404; the live check is the caller's."""
    sessions = await request_trader(request).list_sessions()
    target = next((s for s in sessions if s.get("session_id") == session_id), None)
    if target is None:
        raise api_error(404, "session_not_found", f"Session {session_id} not found", "not_found")
    return target


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
    """
    pool = request_pool(request)
    state = await read_live_state(pool)
    status = "already_disarmed"
    if state["armed"]:
        await write_armed_transition(pool, armed=False, actor=_actor(request), reason=body.reason)
        status = "disarmed"
    running = await running_live_sessions(request_trader(request))
    audit(
        getattr(request.state, "principal", None),
        "live_disarmed",
        reason=body.reason,
        running_live_sessions=len(running),
    )
    return {
        "status": status,
        "live_armed": False,
        "armed_by": None,
        "armed_at": None,
        "live_capital": await read_live_capital(pool),
        "running_live_sessions": running,
        "running_live_sessions_count": len(running),
        "message": (
            f"Live trading is disarmed. {len(running)} live session(s) keep running under "
            f"their start's authorization. {LIVE_SESSION_NOTE}"
        ),
    }


# ---------------------------------------------------------------------------
# The per-action code gates
# ---------------------------------------------------------------------------


@router.post("/session-start", response_model=ApprovalRequiredResponse)
async def session_start(body: LiveSessionStartRequest, request: Request) -> dict[str, Any]:
    """Request a live session start: its own one-time code on top of the armed gate."""
    actor = _requester(request)
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
    """Request one manual live order on a running live session (per-order code)."""
    actor = _requester(request)
    await require_armed(request_pool(request))
    target = await _session(request, body.session_id)
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
    directly through ``DELETE /api/system/exchanges/{id}/connect``)."""
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
    if not is_trade_capable(row["validated_permissions"]):
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
