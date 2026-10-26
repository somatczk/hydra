"""Activity + approval routes: the pinned seam (MCP-D consumes), browser and service principals.

Requests go through the real app on this test's event loop (httpx ASGITransport): the
approval store's asyncio lock must never be touched from another loop, which a sync
TestClient's portal would do. The browser principal is auth_mode 'dev' (no bearer); the
service principal sends the bearer token.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from hydra.core.config import HydraConfig
from hydra.dashboard.api import app
from hydra.dashboard.routes.activity import get_approval_store

from .conftest import TEST_API_TOKEN

T0 = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
CSRF = {"X-Hydra-Request": "1"}
PENDING_KEYS = {"id", "actor", "action", "reason", "params", "requested_at", "expires_at", "status"}

Bind = Callable[..., HydraConfig]


@pytest.fixture(autouse=True)
def cfg(bind_api_config: Bind) -> HydraConfig:
    return bind_api_config(HYDRA_API__AUTH_MODE="dev", HYDRA_API_TOKEN=TEST_API_TOKEN)


@pytest.fixture(autouse=True)
def fresh_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app.state, "approval_store", None, raising=False)


@pytest.fixture()
async def browser() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t", headers=CSRF
    ) as client:
        yield client


@pytest.fixture()
async def service() -> AsyncIterator[httpx.AsyncClient]:
    headers = {"Authorization": f"Bearer {TEST_API_TOKEN}", **CSRF}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t", headers=headers
    ) as client:
        yield client


class Exec:
    """Registered in-test executor; records every (deps, params, actor) call."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, dict[str, Any], str]] = []

    async def __call__(self, deps: Any, params: dict[str, Any], actor: str) -> Any:
        self.calls.append((deps, dict(params), actor))
        return {"released": True}


def _event_row(i: int, actor: str) -> dict[str, Any]:
    return {
        "id": i,
        "ts": T0 - timedelta(minutes=i),
        "scope": "paper",
        "kind": f"event_{i}",
        "tier": None,
        "actor": actor,
        "reason": "r",
        "details": f'{{"n": {i}}}',
    }


@pytest.fixture()
def pool(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[_event_row(1, "service:mcp"), _event_row(2, "user:soma")])
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    fake_pool = MagicMock()
    fake_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    fake_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(app.state, "db_pool", fake_pool)
    return fake_pool, conn


async def make_approval(action: str = "kill_switch_release") -> Any:
    return await get_approval_store(app).create(
        action, "claude asks to release the kill switch", {"reason": "calm"}
    )


def wrong_code(code: str) -> str:
    return next(c for c in ("000000", "111111", "222222") if c != code)


def approve_url(approval_id: str) -> str:
    return f"/api/activity/approvals/{approval_id}/approve"


def deny_url(approval_id: str) -> str:
    return f"/api/activity/approvals/{approval_id}/deny"


class TestTrail:
    async def test_events_come_newest_first_from_sql(
        self, browser: httpx.AsyncClient, pool: tuple[MagicMock, MagicMock]
    ) -> None:
        _fake, conn = pool
        resp = await browser.get("/api/activity?limit=10")
        assert resp.status_code == 200
        rows = resp.json()
        assert [r["id"] for r in rows] == [1, 2]
        assert rows[0]["details"] == {"n": 1}
        query, *args = conn.fetch.await_args.args
        assert "ORDER BY ts DESC, id DESC" in query
        assert args == ["paper", 10]

    async def test_actor_filter_is_inside_the_sql_never_after_the_limit(
        self, browser: httpx.AsyncClient, pool: tuple[MagicMock, MagicMock]
    ) -> None:
        _fake, conn = pool
        resp = await browser.get("/api/activity?actor=service:mcp&limit=500")
        assert resp.status_code == 200
        query, *args = conn.fetch.await_args.args
        assert "AND actor = $2" in query
        assert "LIMIT $3" in query
        assert args == ["paper", "service:mcp", 500]

    async def test_empty_actor_means_unfiltered(
        self, browser: httpx.AsyncClient, pool: tuple[MagicMock, MagicMock]
    ) -> None:
        _fake, conn = pool
        resp = await browser.get("/api/activity?actor=&limit=25")
        assert resp.status_code == 200
        _query, *args = conn.fetch.await_args.args
        assert args == ["paper", 25]

    async def test_limit_over_500_is_refused(
        self, browser: httpx.AsyncClient, pool: tuple[MagicMock, MagicMock]
    ) -> None:
        assert (await browser.get("/api/activity?limit=501")).status_code == 422

    async def test_without_db_is_503(self, browser: httpx.AsyncClient) -> None:
        resp = await browser.get("/api/activity")
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "database_unavailable"


class TestPendingApprovals:
    async def test_list_has_exactly_the_pinned_shape_oldest_first(
        self, browser: httpx.AsyncClient
    ) -> None:
        first = await make_approval()
        second = await make_approval(action="restart_worker")
        resp = await browser.get("/api/activity/approvals")
        assert resp.status_code == 200
        rows = resp.json()
        assert [r["id"] for r in rows] == [first.id, second.id]
        assert set(rows[0].keys()) == PENDING_KEYS
        assert rows[0]["actor"] == "service:mcp"
        assert rows[0]["action"] == "kill_switch_release"
        assert rows[0]["params"] == {"reason": "calm"}
        assert rows[0]["status"] == "pending"

    async def test_detail_carries_attempts_left_and_404s_unknown(
        self, browser: httpx.AsyncClient
    ) -> None:
        approval = await make_approval()
        resp = await browser.get(f"/api/activity/approvals/{approval.id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["attempts_left"] == 5
        assert "code" not in body

        await browser.post(approve_url(approval.id), json={"code": wrong_code(approval.code)})
        body = (await browser.get(f"/api/activity/approvals/{approval.id}")).json()
        assert body["attempts_left"] == 4

        assert (await browser.get("/api/activity/approvals/nope")).status_code == 404


class TestApprove:
    async def test_wrong_code_is_422_code_mismatch_with_attempts_left(
        self, browser: httpx.AsyncClient
    ) -> None:
        approval = await make_approval()
        resp = await browser.post(
            approve_url(approval.id), json={"code": wrong_code(approval.code)}
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["code"] == "code_mismatch"
        assert detail["attempts_left"] == 4

    async def test_malformed_code_is_422_and_burns_no_attempt(
        self, browser: httpx.AsyncClient
    ) -> None:
        approval = await make_approval()
        resp = await browser.post(approve_url(approval.id), json={"code": "12ab56"})
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "validation_error"
        body = (await browser.get(f"/api/activity/approvals/{approval.id}")).json()
        assert body["attempts_left"] == 5

        resp = await browser.post(approve_url(approval.id), json={"code": "123456", "extra": 1})
        assert resp.status_code == 422  # extra=forbid

    async def test_service_principal_cannot_approve(
        self, service: httpx.AsyncClient, browser: httpx.AsyncClient
    ) -> None:
        approval = await make_approval()
        resp = await service.post(approve_url(approval.id), json={"code": approval.code})
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "principal_not_allowed"
        body = (await browser.get(f"/api/activity/approvals/{approval.id}")).json()
        assert (body["status"], body["attempts_left"]) == ("pending", 5)

    async def test_right_code_executes_once_with_double_attribution(
        self, browser: httpx.AsyncClient, pool: tuple[MagicMock, MagicMock]
    ) -> None:
        _fake, conn = pool
        executor = Exec()
        get_approval_store(app).register("kill_switch_release", executor)
        approval = await make_approval()

        resp = await browser.post(approve_url(approval.id), json={"code": approval.code})
        assert resp.status_code == 200
        assert resp.json() == {
            "executed": True,
            "result": {"released": True},
            "actor": "service:mcp",
            "approved_by": "dev:dev",
        }
        assert executor.calls == [(app.state, {"reason": "calm"}, "service:mcp")]
        written_kinds = [call.args[2] for call in conn.execute.await_args_list]
        written_actors = [call.args[4] for call in conn.execute.await_args_list]
        assert written_kinds == ["approval_requested", "approval_approved"]
        assert written_actors == ["service:mcp", "dev:dev"]

        again = await browser.post(approve_url(approval.id), json={"code": approval.code})
        assert again.status_code == 409
        assert again.json()["detail"]["code"] == "already_decided"
        assert len(executor.calls) == 1

    async def test_unknown_approval_is_404(self, browser: httpx.AsyncClient) -> None:
        resp = await browser.post(approve_url("nope"), json={"code": "123456"})
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "approval_not_found"


class TestDeny:
    async def test_deny_without_code_decides_and_blocks_approve(
        self, browser: httpx.AsyncClient
    ) -> None:
        approval = await make_approval()
        resp = await browser.post(deny_url(approval.id), json={})
        assert resp.status_code == 200
        assert resp.json() == {"id": approval.id, "status": "denied"}

        again = await browser.post(approve_url(approval.id), json={"code": approval.code})
        assert again.status_code == 409
        assert again.json()["detail"]["code"] == "already_decided"

    async def test_deny_with_reason_decides(self, browser: httpx.AsyncClient) -> None:
        approval = await make_approval()
        resp = await browser.post(deny_url(approval.id), json={"reason": "not now"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "denied"

    async def test_deny_with_no_body_at_all_decides(self, browser: httpx.AsyncClient) -> None:
        approval = await make_approval()
        resp = await browser.post(deny_url(approval.id))
        assert resp.status_code == 200
        assert resp.json() == {"id": approval.id, "status": "denied"}

    async def test_service_principal_cannot_deny(self, service: httpx.AsyncClient) -> None:
        approval = await make_approval()
        resp = await service.post(deny_url(approval.id), json={})
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "principal_not_allowed"

    async def test_deny_unknown_approval_is_404(self, browser: httpx.AsyncClient) -> None:
        assert (await browser.post(deny_url("nope"), json={})).status_code == 404
