"""The activity trail and the pending-approval endpoints (D6, pinned seam with MCP-D).

``GET /api/activity`` serves ``risk_events`` newest first, with the ``?actor=`` filter applied
inside the SQL (never after the LIMIT -- see ``fetch_risk_events``), so the merged trail and
Claude's own trail are both complete. The approval routes expose the in-memory store on
``app.state``: pending approvals for the Needs-you surface, one approval with its remaining
attempts, and the approve (one-time code) / deny decisions. Only a signed-in person can
decide: service principals get 403, so no token holder can approve its own request.

The store is created lazily on first use and writes its lifecycle events to ``risk_events``
through the app's pool; every transition also logs a ``hydra.audit`` line (see
``hydra.services.approvals``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, FastAPI, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from hydra.dashboard.routes._db import ServiceErrorRoute, api_error, db_errors_to_503, require_pool
from hydra.dashboard.security import current_principal
from hydra.risk.state import RiskEventRecord, fetch_risk_events
from hydra.services.approvals import (
    APPROVAL_ACTOR,
    CODE_PATTERN,
    ApprovalStore,
    write_approval_event,
)

router = APIRouter(prefix="/api/activity", tags=["activity"], route_class=ServiceErrorRoute)


def get_approval_store(app: FastAPI) -> ApprovalStore:
    """The engine's one approval store, created on first use (the pool is read per write)."""
    store = getattr(app.state, "approval_store", None)
    if not isinstance(store, ApprovalStore):

        async def write(event: RiskEventRecord) -> None:
            pool = getattr(app.state, "db_pool", None)
            if pool is not None:
                await write_approval_event(pool, event)

        store = ApprovalStore(event_writer=write)
        app.state.approval_store = store
    return store


def _decider(request: Request) -> str:
    """The principal as an actor string, after refusing principals that cannot decide."""
    principal = current_principal(request)
    if principal.kind not in ("user", "dev"):
        raise api_error(
            403,
            "principal_not_allowed",
            "Only a signed-in person can approve or deny; service principals cannot",
            "forbidden",
        )
    return f"{principal.kind}:{principal.name}"


# ---------------------------------------------------------------------------
# Pydantic models (pinned seam 2: MCP-D consumes these shapes exactly)
# ---------------------------------------------------------------------------


class ActivityEvent(BaseModel):
    id: int | None
    ts: datetime | None
    scope: str
    kind: str
    tier: int | None
    actor: str
    reason: str
    details: dict[str, Any]


class PendingApproval(BaseModel):
    id: str
    actor: str
    action: str
    reason: str
    params: dict[str, Any]
    requested_at: datetime
    expires_at: datetime
    status: str


class ApprovalDetail(PendingApproval):
    attempts_left: int
    approved_by: str | None = None
    decided_at: datetime | None = None
    error: str | None = None


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=CODE_PATTERN)


class DenyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(None, max_length=500)


class ApproveResponse(BaseModel):
    executed: bool
    result: Any = None
    actor: str
    approved_by: str


class DenyResponse(BaseModel):
    id: str
    status: str


def _pending_model(approval: Any) -> PendingApproval:
    return PendingApproval(
        id=approval.id,
        actor=APPROVAL_ACTOR,
        action=approval.action,
        reason=approval.reason,
        params=approval.params,
        requested_at=approval.requested_at,
        expires_at=approval.expires_at,
        status=approval.status,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=list[ActivityEvent])
async def get_activity(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    actor: str | None = Query(None),
) -> list[ActivityEvent]:
    """The merged activity trail (risk_events), newest first.

    An empty ``actor`` is treated as absent (the All filter sends ``?actor=``).
    """
    pool = require_pool(request)
    async with db_errors_to_503("reading activity events"):
        events = await fetch_risk_events(pool, limit, actor=actor or None)
    return [
        ActivityEvent(
            id=e.id,
            ts=e.ts,
            scope=e.scope,
            kind=e.kind,
            tier=e.tier,
            actor=e.actor,
            reason=e.reason,
            details=e.details,
        )
        for e in events
    ]


@router.get("/approvals", response_model=list[PendingApproval])
async def list_approvals(request: Request) -> list[PendingApproval]:
    """Pending approvals, oldest first (the closest to expiring is the most urgent)."""
    store = get_approval_store(request.app)
    return [_pending_model(a) for a in await store.list_pending()]


@router.get("/approvals/{approval_id}", response_model=ApprovalDetail)
async def get_approval(request: Request, approval_id: str) -> ApprovalDetail:
    """One approval with its remaining code attempts (never the code itself)."""
    store = get_approval_store(request.app)
    approval = await store.get(approval_id)
    return ApprovalDetail(
        **_pending_model(approval).model_dump(),
        attempts_left=approval.attempts_left if approval.status == "pending" else 0,
        approved_by=approval.approved_by,
        decided_at=approval.decided_at,
        error=approval.error,
    )


@router.post("/approvals/{approval_id}/approve", response_model=ApproveResponse)
async def approve(approval_id: str, body: ApproveRequest, request: Request) -> ApproveResponse:
    """Approve with the one-time code and run the registered executor exactly once."""
    approved_by = _decider(request)
    store = get_approval_store(request.app)
    outcome = await store.approve(
        approval_id, body.code, approved_by=approved_by, deps=request.app.state
    )
    return ApproveResponse(
        executed=outcome.executed,
        result=outcome.result,
        actor=APPROVAL_ACTOR,
        approved_by=approved_by,
    )


@router.post("/approvals/{approval_id}/deny", response_model=DenyResponse)
async def deny(approval_id: str, request: Request, body: DenyRequest | None = None) -> DenyResponse:
    """Deny without a code; the approval stays decided and can never be approved."""
    decided_by = _decider(request)
    store = get_approval_store(request.app)
    reason = None if body is None else body.reason
    outcome = await store.deny(approval_id, decided_by=decided_by, reason=reason)
    return DenyResponse(id=outcome.approval.id, status=outcome.approval.status)
