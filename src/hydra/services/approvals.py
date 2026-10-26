"""The D6 approval state machine: gated MCP actions need a human's one-time code.

One in-memory :class:`ApprovalStore` per engine process holds the pending approvals and is
deliberately NOT persisted: a restart loses them (the 10-minute expiry makes that a
shrug -- the MCP client retries the request and gets a fresh code). Every lifecycle
transition (``requested`` / ``approved`` / ``denied`` / ``expired`` / ``failed``) is logged
on ``hydra.audit`` and, when the caller injects an event writer, appended to ``risk_events``
as a best-effort ``approval_*`` row (scope ``paper``); a failing write never blocks the
transition. An executor that raises marks the approval ``failed`` and answers
``approval_execution_failed`` (the approval is not left pretending to be approved), and
because dedup applies only while one is pending the action can simply be requested again.
Decided approvals are dropped from memory ``DECIDED_RETENTION`` after their decision.

The one-time code is 6 numeric digits from ``secrets`` (20 bits), single-use, compared with
``hmac.compare_digest``, invalidating the approval after 5 wrong attempts and expiring after
10 minutes. That is safe only because of the attempt cap and the short TTL; both are
load-bearing and tested. Approve/deny with the wrong code or from a service principal never
execute anything: executors run only after the code matches, and the REST routes refuse
service principals before the store is consulted.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from hydra.core.logging import get_logger
from hydra.risk.state import PgRiskStateStore, RiskEventRecord
from hydra.services.errors import ServiceError, service_error

logger = logging.getLogger(__name__)
audit_logger = get_logger("hydra.audit")

CODE_TTL = timedelta(minutes=10)
CODE_LENGTH = 6
CODE_PATTERN = r"^[0-9]{6}$"
MAX_CODE_ATTEMPTS = 5
MAX_PENDING = 20
DECIDED_RETENTION = timedelta(hours=1)
APPROVAL_ACTOR = "service:mcp"

ApprovalStatus = Literal["pending", "approved", "denied", "expired", "failed"]
Clock = Callable[[], datetime]
ApprovalExecutor = Callable[[Any, dict[str, Any], str], Awaitable[Any]]
"""Registered per action: ``await executor(deps, params, actor) -> result``.

``deps`` is opaque engine context (the dashboard routes pass ``app.state``); ``params`` is a
copy of the approval's params; ``actor`` is ``APPROVAL_ACTOR``, so the executed action is
always attributed to the service that asked, while ``approved_by`` names the human who said
yes. Executors must re-validate their target state and stay idempotent: the approval is
already recorded as approved when they run, and their result is returned to the approving
caller verbatim. An executor that raises (any exception, ``ServiceError`` included) marks
the approval ``failed`` — the decision is answered with ``approval_execution_failed`` and
a fresh request can be made.
"""
ApprovalEventWriter = Callable[[RiskEventRecord], Awaitable[None]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def write_approval_event(pool: Any, event: RiskEventRecord) -> None:
    """Append one ``approval_*`` row to ``risk_events``; best-effort, never raises."""
    try:
        await PgRiskStateStore(pool).append_event(event)
    except Exception:
        logger.warning(
            "Approval %s row for %s not written (best-effort)",
            event.kind,
            event.details.get("approval_id"),
            exc_info=True,
        )


@dataclass(slots=True)
class Approval:
    """One gated action in the machine; the creator hands out the code, reads never do."""

    id: str
    action: str
    reason: str
    params: dict[str, Any]
    code: str
    status: ApprovalStatus
    requested_at: datetime
    expires_at: datetime
    attempts: int = 0
    approved_by: str | None = None
    decided_at: datetime | None = None
    error: str | None = None

    @property
    def attempts_left(self) -> int:
        return max(0, MAX_CODE_ATTEMPTS - self.attempts)


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    """What a decision did: the approval plus, for an approval, the executor's result."""

    approval: Approval
    result: Any = None
    executed: bool = False


class ApprovalStore:
    """Pending approvals, their one-time codes and the action executors (all in memory).

    ``create`` dedupes per action (a second request while one is pending returns the same
    approval, code and expiry, and writes no new event) and caps the store at
    ``MAX_PENDING`` pending approvals, sweeping expired ones first and then refusing.
    Every transition runs under one ``asyncio.Lock``; the executor itself runs only after
    the lock is released (the atomic status flip before it settles exactly-once), so a slow
    action never stalls deny, the pending list or other decisions. Expiry is swept lazily
    at the top of every operation — decided approvals (approved/denied/expired/failed) are
    dropped from memory ``DECIDED_RETENTION`` after their decision — so nothing schedules
    work on a timer. Lost on restart by design.
    """

    def __init__(
        self,
        *,
        event_writer: ApprovalEventWriter | None = None,
        clock: Clock = _utcnow,
    ) -> None:
        self._approvals: dict[str, Approval] = {}
        self._executors: dict[str, ApprovalExecutor] = {}
        self._event_writer = event_writer
        self._clock = clock
        self._lock = asyncio.Lock()

    def register(self, action: str, executor: ApprovalExecutor) -> None:
        """Map ``action`` to its executor; the registry starts empty (B2 fills it)."""
        self._executors[action] = executor

    async def create(
        self, action: str, reason: str, params: dict[str, Any] | None = None
    ) -> Approval:
        """Request a gated action; dedupes while one is pending for the same action."""
        async with self._lock:
            await self._sweep()
            for approval in self._approvals.values():
                if approval.action == action and approval.status == "pending":
                    return approval
            if sum(a.status == "pending" for a in self._approvals.values()) >= MAX_PENDING:
                raise service_error(
                    409,
                    "approvals_full",
                    f"Too many pending approvals (at most {MAX_PENDING}); "
                    "deny some or wait for them to expire",
                    "invalid",
                )
            now = self._clock()
            approval = Approval(
                id=str(uuid.uuid4()),
                action=action,
                reason=reason,
                params=dict(params or {}),
                code=f"{secrets.randbelow(10**CODE_LENGTH):06d}",
                status="pending",
                requested_at=now,
                expires_at=now + CODE_TTL,
            )
            self._approvals[approval.id] = approval
            await self._emit("requested", approval, actor=APPROVAL_ACTOR)
            return approval

    async def get(self, approval_id: str) -> Approval:
        """One approval by id, sweeping lazily (a stale pending one comes back ``expired``)."""
        async with self._lock:
            await self._sweep()
            approval = self._approvals.get(approval_id)
            if approval is None:
                raise self._not_found(approval_id)
            return approval

    async def list_pending(self) -> list[Approval]:
        """Pending approvals, oldest first (the closest to expiring is the most urgent)."""
        async with self._lock:
            await self._sweep()
            return [
                a
                for a in sorted(self._approvals.values(), key=lambda a: a.requested_at)
                if a.status == "pending"
            ]

    async def approve(
        self, approval_id: str, code: str, *, approved_by: str, deps: Any = None
    ) -> ApprovalOutcome:
        """Check the one-time code, record the approval, run the action's executor once."""
        async with self._lock:
            approval = await self._decidable(approval_id)
            if not hmac.compare_digest(code, approval.code):
                approval.attempts += 1
                if approval.attempts >= MAX_CODE_ATTEMPTS:
                    approval.status = "failed"
                    approval.decided_at = self._clock()
                    await self._emit("failed", approval, actor=approved_by)
                raise service_error(
                    422,
                    "code_mismatch",
                    "That code didn't match. Nothing changed.",
                    "invalid",
                    attempts_left=approval.attempts_left,
                )
            executor = self._executors.get(approval.action)
            if executor is None:
                raise service_error(
                    409,
                    "executor_not_registered",
                    f"No executor is registered for {approval.action!r}; the approval "
                    "stays pending and the code was not used",
                    "invalid",
                    action=approval.action,
                )
            approval.status = "approved"
            approval.decided_at = self._clock()
            approval.approved_by = approved_by
            await self._emit("approved", approval, actor=approved_by)
        # the flip above already settled exactly-once: run the executor unlocked so a slow
        # action cannot stall deny, the pending list or other decisions
        try:
            result = await executor(deps, dict(approval.params), APPROVAL_ACTOR)
        except Exception as exc:
            async with self._lock:
                approval.status = "failed"
                approval.error = str(exc)
                await self._emit("failed", approval, actor=approved_by)
            raise service_error(
                503,
                "approval_execution_failed",
                f"The approved action {approval.action!r} did not run: {exc}",
                "unavailable",
                executed=False,
            ) from exc
        return ApprovalOutcome(approval=approval, result=result, executed=True)

    async def deny(
        self, approval_id: str, *, decided_by: str, reason: str | None = None
    ) -> ApprovalOutcome:
        """Refuse the action without a code; a denied approval can never be approved."""
        async with self._lock:
            approval = await self._decidable(approval_id)
            approval.status = "denied"
            approval.decided_at = self._clock()
            await self._emit("denied", approval, actor=decided_by, reason=reason)
            return ApprovalOutcome(approval=approval)

    async def _decidable(self, approval_id: str) -> Approval:
        """The approval if it can still be decided, else the matching refusal."""
        await self._sweep()
        approval = self._approvals.get(approval_id)
        if approval is None:
            raise self._not_found(approval_id)
        if approval.status == "expired":
            raise service_error(
                410, "approval_expired", "This approval expired; ask for a new one", "invalid"
            )
        if approval.status == "failed":
            raise service_error(
                410,
                "approval_failed",
                "This approval is invalidated (too many wrong codes, or the action "
                "failed to run); ask for a new one",
                "invalid",
            )
        if approval.status in ("approved", "denied"):
            raise service_error(
                409,
                "already_decided",
                f"This approval was already {approval.status}",
                "invalid",
                status=approval.status,
            )
        return approval

    async def _sweep(self) -> None:
        """Sweep pending approvals past their expiry and drop decided ones past retention.

        Decided approvals (approved/denied/expired/failed) are kept for
        ``DECIDED_RETENTION`` after their decision — the decided-elsewhere rendering and
        post-mortems need them — and then silently dropped. The caller holds the lock.
        """
        now = self._clock()
        for approval_id, approval in list(self._approvals.items()):
            if approval.status == "pending":
                if now >= approval.expires_at:
                    approval.status = "expired"
                    approval.decided_at = now
                    await self._emit("expired", approval, actor=APPROVAL_ACTOR)
            elif approval.decided_at is not None and now >= approval.decided_at + DECIDED_RETENTION:
                del self._approvals[approval_id]

    async def _emit(
        self,
        kind: str,
        approval: Approval,
        *,
        actor: str,
        reason: str | None = None,
    ) -> None:
        """One ``hydra.audit`` line plus a best-effort ``risk_events`` row per transition."""
        audit_logger.info(
            "audit",
            action=f"approval_{kind}",
            actor=actor,
            approval_id=approval.id,
            approval_action=approval.action,
        )
        if self._event_writer is None:
            return
        details: dict[str, Any] = {"approval_id": approval.id, "action": approval.action}
        if kind == "failed":
            details["attempts"] = approval.attempts
            if approval.error is not None:
                details["error"] = approval.error
        if reason is not None:
            details["reason"] = reason
        event = RiskEventRecord(
            kind=f"approval_{kind}",
            actor=actor,
            reason=reason if reason is not None else approval.reason,
            details=details,
        )
        try:
            await self._event_writer(event)
        except Exception:
            logger.warning(
                "Approval %s row for %s not written (best-effort)",
                event.kind,
                approval.id,
                exc_info=True,
            )

    @staticmethod
    def _not_found(approval_id: str) -> ServiceError:
        return service_error(404, "approval_not_found", "No such approval", "not_found")
