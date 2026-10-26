"""ApprovalStore: codes, single-use, attempts, expiry, dedup, cap, executors, lifecycle events."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from structlog.testing import capture_logs

from hydra.core.logging import get_logger
from hydra.risk.state import RiskEventRecord
from hydra.services import approvals as approvals_module
from hydra.services.approvals import (
    APPROVAL_ACTOR,
    CODE_TTL,
    MAX_CODE_ATTEMPTS,
    MAX_PENDING,
    ApprovalStore,
)
from hydra.services.errors import ServiceError

T0 = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: int) -> None:
        self.now += timedelta(**kwargs)


class Recorder:
    """Injected event writer: what the store would append to risk_events."""

    def __init__(self) -> None:
        self.events: list[RiskEventRecord] = []

    async def __call__(self, event: RiskEventRecord) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]


class Exec:
    """Registered in-test executor; records every (deps, params, actor) call."""

    def __init__(self, result: Any = None) -> None:
        self.calls: list[tuple[Any, dict[str, Any], str]] = []
        self.result = result

    async def __call__(self, deps: Any, params: dict[str, Any], actor: str) -> Any:
        self.calls.append((deps, dict(params), actor))
        return self.result


def make_store(recorder: Recorder, clock: FakeClock) -> ApprovalStore:
    return ApprovalStore(event_writer=recorder, clock=clock)


def wrong_code(code: str) -> str:
    return next(c for c in ("000000", "111111", "222222") if c != code)


async def _created(store: ApprovalStore, action: str = "kill_switch_release") -> Any:
    return await store.create(action, "claude asks to release the kill switch", {"reason": "calm"})


class TestCodesAndIds:
    async def test_code_is_six_digits_and_id_is_uuid4(self) -> None:
        store = make_store(Recorder(), FakeClock())
        for _ in range(5):
            approval = await _created(store, action=f"action_{uuid.uuid4().hex[:8]}")
            assert re.fullmatch(r"[0-9]{6}", approval.code)
            assert uuid.UUID(approval.id).version == 4

    async def test_expiry_is_ten_minutes_after_the_request(self) -> None:
        clock = FakeClock()
        store = make_store(Recorder(), clock)
        approval = await _created(store)
        assert approval.expires_at - approval.requested_at == CODE_TTL


class TestApprove:
    async def test_right_code_runs_the_executor_once_with_double_attribution(self) -> None:
        store = make_store(Recorder(), FakeClock())
        executor = Exec(result={"released": True})
        store.register("kill_switch_release", executor)
        approval = await _created(store)

        outcome = await store.approve(
            approval.id, approval.code, approved_by="user:soma", deps="deps-token"
        )

        assert outcome.executed is True
        assert outcome.result == {"released": True}
        assert executor.calls == [("deps-token", {"reason": "calm"}, APPROVAL_ACTOR)]
        assert outcome.approval.status == "approved"
        assert outcome.approval.approved_by == "user:soma"

        # single-use: the same code is already decided and nothing runs twice
        with pytest.raises(ServiceError) as exc:
            await store.approve(approval.id, approval.code, approved_by="user:soma")
        assert (exc.value.status_code, exc.value.detail["code"]) == (409, "already_decided")
        assert len(executor.calls) == 1

    async def test_wrong_codes_burn_attempts_then_invalidate(self) -> None:
        recorder = Recorder()
        store = make_store(recorder, FakeClock())
        executor = Exec()
        store.register("kill_switch_release", executor)
        approval = await _created(store)

        for burned in range(1, MAX_CODE_ATTEMPTS):
            with pytest.raises(ServiceError) as exc:
                await store.approve(approval.id, wrong_code(approval.code), approved_by="user:soma")
            assert (exc.value.status_code, exc.value.detail["code"]) == (422, "code_mismatch")
            assert exc.value.detail["attempts_left"] == MAX_CODE_ATTEMPTS - burned
        assert approval.status == "pending"

        with pytest.raises(ServiceError) as exc:  # the 5th wrong code invalidates
            await store.approve(approval.id, wrong_code(approval.code), approved_by="user:soma")
        assert (exc.value.status_code, exc.value.detail["attempts_left"]) == (422, 0)
        assert approval.status == "failed"

        with pytest.raises(ServiceError) as exc:  # the right code no longer works
            await store.approve(approval.id, approval.code, approved_by="user:soma")
        assert (exc.value.status_code, exc.value.detail["code"]) == (410, "approval_failed")
        assert executor.calls == []
        failed = [e for e in recorder.events if e.kind == "approval_failed"]
        assert len(failed) == 1
        assert failed[0].actor == "user:soma"
        assert failed[0].details["attempts"] == MAX_CODE_ATTEMPTS

    async def test_unregistered_executor_refuses_without_consuming_the_code(self) -> None:
        store = make_store(Recorder(), FakeClock())
        approval = await _created(store, action="no_executor_action")

        with pytest.raises(ServiceError) as exc:
            await store.approve(approval.id, approval.code, approved_by="user:soma")
        assert (exc.value.status_code, exc.value.detail["code"]) == (409, "executor_not_registered")
        assert (approval.status, approval.attempts) == ("pending", 0)

        store.register("no_executor_action", Exec())
        outcome = await store.approve(approval.id, approval.code, approved_by="user:soma")
        assert outcome.executed is True


class TestDeny:
    async def test_deny_needs_no_code_and_blocks_a_later_approve(self) -> None:
        recorder = Recorder()
        store = make_store(recorder, FakeClock())
        executor = Exec()
        store.register("kill_switch_release", executor)
        approval = await _created(store)

        outcome = await store.deny(approval.id, decided_by="user:soma", reason="not now")

        assert outcome.approval.status == "denied"
        assert outcome.approval.approved_by == "user:soma"
        with pytest.raises(ServiceError) as exc:
            await store.approve(approval.id, approval.code, approved_by="user:soma")
        assert (exc.value.status_code, exc.value.detail["code"]) == (409, "already_decided")
        assert executor.calls == []

        denied = [e for e in recorder.events if e.kind == "approval_denied"]
        assert len(denied) == 1
        assert denied[0].actor == "user:soma"
        assert denied[0].details["reason"] == "not now"


class TestExpiry:
    async def test_expired_approvals_report_expired_and_change_nothing(self) -> None:
        recorder = Recorder()
        clock = FakeClock()
        store = make_store(recorder, clock)
        executor = Exec()
        store.register("kill_switch_release", executor)
        approval = await _created(store)

        clock.advance(minutes=10)  # exactly the TTL: expired
        with pytest.raises(ServiceError) as exc:
            await store.approve(approval.id, approval.code, approved_by="user:soma")
        assert (exc.value.status_code, exc.value.detail["code"]) == (410, "approval_expired")
        with pytest.raises(ServiceError):
            await store.deny(approval.id, decided_by="user:soma")
        assert executor.calls == []
        assert (await store.get(approval.id)).status == "expired"
        assert recorder.kinds() == ["approval_requested", "approval_expired"]

    async def test_expiry_is_swept_lazily_and_listed_approvals_go(self) -> None:
        recorder = Recorder()
        clock = FakeClock()
        store = make_store(recorder, clock)
        approval = await _created(store)
        assert [a.id for a in await store.list_pending()] == [approval.id]

        clock.advance(minutes=9, seconds=59)  # one second before the TTL: still listed
        assert [a.id for a in await store.list_pending()] == [approval.id]
        clock.advance(seconds=1)
        assert await store.list_pending() == []
        assert recorder.kinds() == ["approval_requested", "approval_expired"]


class TestDedupAndCap:
    async def test_second_create_returns_the_pending_approval_and_writes_no_event(self) -> None:
        recorder = Recorder()
        store = make_store(recorder, FakeClock())
        first = await _created(store)
        second = await store.create("kill_switch_release", "claude asks again", {"reason": "hurry"})
        assert second is first
        assert (second.code, second.expires_at) == (first.code, first.expires_at)
        assert recorder.kinds() == ["approval_requested"]

        await store.deny(first.id, decided_by="user:soma")  # decided: a new create is fresh
        third = await _created(store)
        assert third.id != first.id
        assert third.status == "pending"
        assert recorder.kinds() == [
            "approval_requested",
            "approval_denied",
            "approval_requested",
        ]

    async def test_cap_sweeps_expired_first_then_refuses(self) -> None:
        recorder = Recorder()
        clock = FakeClock()
        store = make_store(recorder, clock)
        for i in range(MAX_PENDING):
            await store.create(f"action_{i}", "r")

        with pytest.raises(ServiceError) as exc:
            await store.create("one_too_many", "r")
        assert (exc.value.status_code, exc.value.detail["code"]) == (409, "approvals_full")
        assert len(recorder.events) == MAX_PENDING

        clock.advance(minutes=10)  # the sweep frees the store first
        approval = await store.create("after_expiry", "r")
        assert approval.status == "pending"
        assert recorder.kinds().count("approval_expired") == MAX_PENDING


class TestEventsAndAudit:
    async def test_lifecycle_actors_scopes_and_audit_lines(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = Recorder()
        store = make_store(recorder, FakeClock())
        executor = Exec()
        store.register("kill_switch_release", executor)
        monkeypatch.setattr(
            approvals_module, "audit_logger", get_logger("hydra.audit")
        )  # a cached logger would bypass capture_logs

        with capture_logs() as logs:
            approval = await _created(store)
            await store.approve(approval.id, approval.code, approved_by="user:soma")

        assert recorder.kinds() == ["approval_requested", "approval_approved"]
        requested, decided = recorder.events
        assert requested.actor == APPROVAL_ACTOR
        assert requested.scope == "paper"
        assert decided.actor == "user:soma"
        assert decided.details["approval_id"] == approval.id
        assert decided.details["action"] == "kill_switch_release"
        audit = [log for log in logs if log.get("event") == "audit"]
        assert [(log["action"], log["actor"]) for log in audit] == [
            ("approval_requested", APPROVAL_ACTOR),
            ("approval_approved", "user:soma"),
        ]

    async def test_failing_event_writer_never_blocks_a_transition(self) -> None:
        async def boom(_event: RiskEventRecord) -> None:
            raise RuntimeError("risk_events write failed")

        store = ApprovalStore(event_writer=boom, clock=FakeClock())
        approval = await _created(store)
        await store.deny(approval.id, decided_by="user:soma")
        assert approval.status == "denied"

    async def test_unknown_approval_is_404_everywhere(self) -> None:
        store = make_store(Recorder(), FakeClock())
        for call in (
            store.get("nope"),
            store.approve("nope", "123456", approved_by="user:soma"),
            store.deny("nope", decided_by="user:soma"),
        ):
            with pytest.raises(ServiceError) as exc:
                await call
            assert (exc.value.status_code, exc.value.detail["code"]) == (404, "approval_not_found")
