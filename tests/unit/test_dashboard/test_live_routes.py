"""The live-gate routes: the armed-state read, the four code-gated create routes, disarm.

Requests go through the real app (the security middleware, the error envelope, the
activity approve leg) exactly like ``test_activity_routes.py``: the browser principal
is auth_mode ``dev`` (no bearer, ``X-Hydra-Request``), the service principal sends the
bearer token. The DB boundary is an in-memory fake answering the statements the live
gate issues; the exchange boundary is a fake ccxt ``binance`` class behind the real
keyed client.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, ClassVar

import ccxt
import ccxt.async_support
import httpx
import pytest
from cryptography.fernet import Fernet

from hydra.core.config import HydraConfig
from hydra.core.encryption import encrypt
from hydra.dashboard.api import app

from .conftest import TEST_API_TOKEN

CSRF = {"X-Hydra-Request": "1"}
ARMED_ROW = json.dumps({"armed": True, "by": "user:soma", "at": "2026-10-28T00:00:00+00:00"})
NOTE = (
    "Type the code in the Hydra UI Activity page to approve. The code is single-use "
    "and expires in 10 minutes."
)

Bind = Callable[..., HydraConfig]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Db:
    """Answers the statements the live gate and the approval store issue."""

    def __init__(
        self,
        *,
        config: dict[str, str] | None = None,
        credentials: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.config = dict(config or {})
        self.credentials = dict(credentials or {})
        self.events: list[dict[str, Any]] = []

    def value(self, key: str) -> Any:
        return json.loads(self.config[key])

    def acquire(self) -> _Db:
        return self

    async def __aenter__(self) -> _Db:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        yield

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if sql == "SELECT value FROM system_config WHERE key = $1":
            value = self.config.get(args[0])
            return None if value is None else {"value": value}
        if sql.startswith("SELECT encrypted_key"):
            row = self.credentials.get(args[0])
            return None if row is None else dict(row)
        if sql == "SELECT 1 FROM exchange_credentials WHERE exchange_id = $1":
            return None if args[0] not in self.credentials else {"one": 1}
        if sql == "SELECT validated_permissions FROM exchange_credentials WHERE exchange_id = $1":
            row = self.credentials.get(args[0])
            return (
                None if row is None else {"validated_permissions": row.get("validated_permissions")}
            )
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if sql == "SELECT value FROM system_config WHERE key = $1":
            return self.config.get(args[0])
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def execute(self, sql: str, *args: Any) -> str:
        if sql.startswith("INSERT INTO system_config"):
            self.config[args[0]] = str(args[1])
            return "INSERT 0 1"
        if sql.startswith("INSERT INTO risk_events"):
            if sql.startswith(
                "INSERT INTO risk_events (scope, kind, tier, actor, reason, "
                "details) VALUES ('paper'"
            ):
                kind, actor, reason, details = args
            else:
                _, kind, _, actor, reason, details = args
            self.events.append(
                {"kind": kind, "actor": actor, "reason": reason, "details": json.loads(details)}
            )
            return "INSERT 0 1"
        if sql == "DELETE FROM exchange_credentials WHERE exchange_id = $1":
            self.credentials.pop(args[0], None)
            return "DELETE 1"
        raise AssertionError(f"unexpected execute: {sql}")


class _Backend:
    def __init__(self, sessions: list[dict[str, Any]] | None = None) -> None:
        self.sessions = sessions or []
        self.kill_switch = False

    async def kill_switch_active(self) -> bool:
        return self.kill_switch

    async def list_sessions(self) -> list[dict[str, Any]]:
        return [dict(s) for s in self.sessions]


class _BrokenDb:
    """A pool whose acquisition fails: the disarm path's transient DB outage."""

    def acquire(self) -> _BrokenDb:
        return self

    async def __aenter__(self) -> _BrokenDb:
        raise OSError("db gone")

    async def __aexit__(self, *exc: object) -> None:
        return None


RESTRICTIONS = {
    "ipRestrict": False,
    "enableWithdrawals": False,
    "enableSpotAndMarginTrading": True,
}


class FakeBinance:
    error: ClassVar[BaseException | None] = None
    restrictions: ClassVar[dict[str, Any]] = dict(RESTRICTIONS)
    instances: ClassVar[list[FakeBinance]] = []

    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts
        self.closed = False
        FakeBinance.instances.append(self)

    async def close(self) -> None:
        self.closed = True


async def _fake_get_account_api_restrictions(fake: FakeBinance, *args: Any) -> dict[str, Any]:
    if FakeBinance.error is not None:
        raise FakeBinance.error
    return dict(FakeBinance.restrictions)


# ccxt's camelCase implicit API name, bound as an attribute (pep8-naming flags the def)
FakeBinance.privateGetAccountApiRestrictions = _fake_get_account_api_restrictions


TRADE_CAPABLE = json.dumps(
    {"enable_spot_and_margin_trading": True, "enable_withdrawals": False, "ip_restrict": False}
)


def _creds(permissions: str | None = TRADE_CAPABLE, environment: str = "mainnet") -> dict[str, Any]:
    return {
        "encrypted_key": encrypt("key-1"),
        "encrypted_secret": encrypt("secret-1"),
        "encrypted_passphrase": None,
        "validated_environment": environment,
        "validated_permissions": permissions,
    }


def _live_session(session_id: str = "s-live", status: str = "running") -> dict[str, Any]:
    return {"session_id": session_id, "trading_mode": "live", "status": status}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def cfg(bind_api_config: Bind) -> HydraConfig:
    return bind_api_config(HYDRA_API__AUTH_MODE="dev", HYDRA_API_TOKEN=TEST_API_TOKEN)


@pytest.fixture(autouse=True)
def credential_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", Fernet.generate_key().decode())


@pytest.fixture(autouse=True)
def fresh_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app.state, "approval_store", None, raising=False)


@pytest.fixture()
def binance(monkeypatch: pytest.MonkeyPatch) -> type[FakeBinance]:
    FakeBinance.error = None
    FakeBinance.restrictions = dict(RESTRICTIONS)
    FakeBinance.instances = []
    monkeypatch.setattr(ccxt.async_support, "binance", FakeBinance)
    return FakeBinance


@pytest.fixture()
def db(monkeypatch: pytest.MonkeyPatch) -> _Db:
    fake = _Db(credentials={"binance": _creds()})
    monkeypatch.setattr(app.state, "db_pool", fake)
    return fake


@pytest.fixture()
def backend(monkeypatch: pytest.MonkeyPatch) -> _Backend:
    fake = _Backend()
    monkeypatch.setattr(app.state, "trader", fake)
    return fake


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


def _detail(resp: httpx.Response) -> dict[str, Any]:
    return resp.json()["detail"]


async def _approve(browser: httpx.AsyncClient, approval_id: str, code: str) -> httpx.Response:
    return await browser.post(f"/api/activity/approvals/{approval_id}/approve", json={"code": code})


# ---------------------------------------------------------------------------
# The status read and the arm ceremony
# ---------------------------------------------------------------------------


class TestStatus:
    async def test_absent_row_reads_disarmed(
        self, db: _Db, backend: _Backend, browser: httpx.AsyncClient
    ) -> None:
        resp = await browser.get("/api/live/status")
        assert resp.status_code == 200
        assert resp.json() == {
            "live_armed": False,
            "armed_by": None,
            "armed_at": None,
            "live_capital": None,
            "running_live_sessions": [],
            "running_live_sessions_count": 0,
        }

    async def test_armed_state_with_live_sessions(
        self, db: _Db, backend: _Backend, browser: httpx.AsyncClient
    ) -> None:
        db.config["live_armed"] = ARMED_ROW
        db.config["live_capital"] = "500.0"
        backend.sessions = [
            _live_session(),
            {"session_id": "s-paper", "trading_mode": "paper", "status": "running"},
        ]
        body = (await browser.get("/api/live/status")).json()
        assert (body["live_armed"], body["armed_by"], body["live_capital"]) == (
            True,
            "user:soma",
            500.0,
        )
        assert (body["running_live_sessions"], body["running_live_sessions_count"]) == (
            ["s-live"],
            1,
        )


class TestArm:
    async def test_arm_returns_the_code_and_approve_arms(
        self, db: _Db, backend: _Backend, binance: type[FakeBinance], browser: httpx.AsyncClient
    ) -> None:
        resp = await browser.post("/api/live/arm", json={"live_capital": 500.0})
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"status", "approval_id", "code", "expires_at", "params", "note"}
        assert body["status"] == "approval_required"
        assert re.fullmatch(r"[0-9]{6}", body["code"])
        assert body["params"] == {"live_capital": 500.0, "reason": "arm live trading"}
        assert body["note"] == NOTE
        assert "live_armed" not in db.config  # nothing armed before the code

        approved = await _approve(browser, body["approval_id"], body["code"])
        assert approved.status_code == 200, approved.text
        assert approved.json() == {
            "executed": True,
            "result": {
                "status": "armed",
                "live_armed": True,
                "armed_by": "dev:dev",
                "armed_at": approved.json()["result"]["armed_at"],
                "live_capital": 500.0,
                "message": "Live trading is armed; live session starts and manual live "
                "orders now need their own one-time code",
            },
            "actor": "dev:dev",
            "approved_by": "dev:dev",
        }
        status = (await browser.get("/api/live/status")).json()
        assert (status["live_armed"], status["live_capital"]) == (True, 500.0)
        assert [e["kind"] for e in db.events] == [
            "approval_requested",
            "approval_approved",
            "live_armed",
        ]
        assert all(e["actor"] == "dev:dev" for e in db.events)

    async def test_wrong_codes_fail_the_approval_and_disarm_stays(
        self, db: _Db, backend: _Backend, binance: type[FakeBinance], browser: httpx.AsyncClient
    ) -> None:
        body = (await browser.post("/api/live/arm", json={"live_capital": 500.0})).json()
        wrong = next(c for c in ("000000", "111111", "222222") if c != body["code"])
        for _ in range(5):
            resp = await _approve(browser, body["approval_id"], wrong)
            assert (resp.status_code, _detail(resp)["code"]) == (422, "code_mismatch")
        failed = await _approve(browser, body["approval_id"], body["code"])
        assert (failed.status_code, _detail(failed)["code"]) == (410, "approval_failed")
        assert "live_armed" not in db.config
        assert (await browser.get("/api/live/status")).json()["live_armed"] is False

    async def test_deny_leaves_it_disarmed(
        self, db: _Db, backend: _Backend, binance: type[FakeBinance], browser: httpx.AsyncClient
    ) -> None:
        body = (await browser.post("/api/live/arm", json={"live_capital": 500.0})).json()
        resp = await browser.post(f"/api/activity/approvals/{body['approval_id']}/deny", json={})
        assert resp.status_code == 200
        assert (await browser.get("/api/live/status")).json()["live_armed"] is False

    async def test_a_bad_key_surfaces_the_executors_refusal_unchanged(
        self, db: _Db, backend: _Backend, browser: httpx.AsyncClient
    ) -> None:
        """R22 through the route: the executor's 403 reaches the approving caller."""
        db.credentials = {}  # nothing stored: the arm executor refuses at execution time
        body = (await browser.post("/api/live/arm", json={"live_capital": 500.0})).json()
        resp = await _approve(browser, body["approval_id"], body["code"])
        assert resp.status_code == 403
        assert _detail(resp)["code"] == "live_credentials_missing"
        assert "live_armed" not in db.config
        detail = (await browser.get(f"/api/activity/approvals/{body['approval_id']}")).json()
        assert detail["status"] == "failed"


class TestDisarm:
    async def test_disarm_is_direct_and_names_the_running_sessions(
        self, db: _Db, backend: _Backend, browser: httpx.AsyncClient
    ) -> None:
        db.config["live_armed"] = ARMED_ROW
        db.config["live_capital"] = "500.0"
        backend.sessions = [_live_session()]
        resp = await browser.post("/api/live/disarm", json={"reason": "stop the exposure"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "disarmed"
        assert body["live_armed"] is False
        assert (body["armed_by"], body["armed_at"]) == (None, None)
        assert (body["running_live_sessions"], body["running_live_sessions_count"]) == (
            ["s-live"],
            1,
        )
        assert "keep running under their start's authorization" in body["message"]
        assert [e["kind"] for e in db.events] == ["live_disarmed"]
        assert (await browser.get("/api/live/status")).json()["live_armed"] is False

    async def test_disarm_when_disarmed_is_an_honest_no_op(
        self, db: _Db, backend: _Backend, browser: httpx.AsyncClient
    ) -> None:
        resp = await browser.post("/api/live/disarm", json={"reason": "nothing armed"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "already_disarmed"
        assert resp.json()["pending_live_approvals"] == []
        assert db.events == []

    async def test_a_broken_read_never_answers_already_disarmed(
        self, db: _Db, backend: _Backend, browser: httpx.AsyncClient
    ) -> None:
        """R31 A2r: an unreadable armed row is 503, never a silent already_disarmed."""
        db.config["live_armed"] = ARMED_ROW
        app.state.db_pool = _BrokenDb()
        resp = await browser.post("/api/live/disarm", json={"reason": "transient outage"})
        assert (resp.status_code, _detail(resp)["code"]) == (503, "database_unavailable")
        app.state.db_pool = db
        assert (await browser.get("/api/live/status")).json()["live_armed"] is True

    async def test_disarm_names_pending_live_arming_approvals(
        self, db: _Db, backend: _Backend, browser: httpx.AsyncClient
    ) -> None:
        db.config["live_armed"] = ARMED_ROW
        arm = (
            await browser.post("/api/live/arm", json={"live_capital": 500.0, "reason": "arm it"})
        ).json()
        resp = await browser.post("/api/live/disarm", json={"reason": "stop the exposure"})
        body = resp.json()
        assert body["status"] == "disarmed"
        assert body["pending_live_approvals"] == [arm["approval_id"]]
        assert "still re-arm" in body["message"]


# ---------------------------------------------------------------------------
# The per-action create routes (pinned seam-#9 shapes)
# ---------------------------------------------------------------------------


class TestCreateRoutes:
    async def test_service_principals_are_refused_on_every_create_route(
        self, db: _Db, backend: _Backend, service: httpx.AsyncClient
    ) -> None:
        db.config["live_armed"] = ARMED_ROW
        backend.sessions = [_live_session()]
        db.credentials = {"binance": _creds()}
        calls = [
            ("/api/live/arm", {"live_capital": 500.0}),
            ("/api/live/session-start", {"strategy_id": "momentum", "reason": "go live"}),
            (
                "/api/live/order",
                {"session_id": "s-live", "side": "buy", "quantity": "0.01", "reason": "one buy"},
            ),
            ("/api/live/credential-delete", {"exchange_id": "binance", "reason": "rotate keys"}),
        ]
        for url, body in calls:
            resp = await service.post(url, json=body)
            assert (resp.status_code, _detail(resp)["code"]) == (
                403,
                "principal_not_allowed",
            ), url
        assert (await service.get("/api/live/status")).status_code == 200

    async def test_session_start_create_requires_armed(
        self, db: _Db, backend: _Backend, browser: httpx.AsyncClient
    ) -> None:
        resp = await browser.post(
            "/api/live/session-start", json={"strategy_id": "momentum", "reason": "go live"}
        )
        assert (resp.status_code, _detail(resp)["code"]) == (409, "live_not_armed")

    async def test_session_start_create_answers_the_pinned_shape(
        self, db: _Db, backend: _Backend, browser: httpx.AsyncClient
    ) -> None:
        db.config["live_armed"] = ARMED_ROW
        resp = await browser.post(
            "/api/live/session-start",
            json={"strategy_id": "momentum", "paper_capital": 250.0, "reason": "go live"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"status", "approval_id", "code", "expires_at", "params", "note"}
        assert body["status"] == "approval_required"
        assert re.fullmatch(r"[0-9]{6}", body["code"])
        assert body["params"] == {
            "strategy_id": "momentum",
            "paper_capital": 250.0,
            "reason": "go live",
        }
        assert body["note"] == NOTE

    async def test_order_create_requires_armed_then_a_live_running_session(
        self, db: _Db, backend: _Backend, browser: httpx.AsyncClient
    ) -> None:
        order = {"session_id": "s-live", "side": "buy", "quantity": "0.01", "reason": "one buy"}
        resp = await browser.post("/api/live/order", json=order)
        assert (resp.status_code, _detail(resp)["code"]) == (409, "live_not_armed")
        db.config["live_armed"] = ARMED_ROW
        backend.sessions = [
            {"session_id": "s-paper", "trading_mode": "paper", "status": "running"},
            _live_session(status="stopped"),
        ]
        resp = await browser.post("/api/live/order", json=order)
        assert (resp.status_code, _detail(resp)["code"]) == (409, "not_a_live_session")
        backend.sessions = []
        resp = await browser.post("/api/live/order", json=order)
        assert (resp.status_code, _detail(resp)["code"]) == (404, "session_not_found")

    async def test_order_create_answers_the_pinned_shape(
        self, db: _Db, backend: _Backend, browser: httpx.AsyncClient
    ) -> None:
        db.config["live_armed"] = ARMED_ROW
        backend.sessions = [_live_session()]
        resp = await browser.post(
            "/api/live/order",
            json={
                "session_id": "s-live",
                "side": "buy",
                "quantity": "0.01",
                "take_profit": "70000",
                "reason": "one buy",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"status", "approval_id", "code", "expires_at", "params", "note"}
        assert body["status"] == "approval_required"
        assert re.fullmatch(r"[0-9]{6}", body["code"])
        assert body["params"] == {
            "symbol": "BTCUSDT",
            "side": "buy",
            "order_type": "MARKET",
            "quantity": "0.01",
            "price": None,
            "take_profit": "70000",
            "stop_loss": None,
            "trailing_stop_pct": None,
            "session_id": "s-live",
            "reason": "one buy",
        }
        assert body["note"] == NOTE

    async def test_credential_delete_create_requires_trade_capable_keys(
        self, db: _Db, backend: _Backend, browser: httpx.AsyncClient
    ) -> None:
        db.credentials = {"binance": _creds(permissions=None)}
        resp = await browser.post(
            "/api/live/credential-delete", json={"exchange_id": "binance", "reason": "rotate keys"}
        )
        assert (resp.status_code, _detail(resp)["code"]) == (409, "credential_delete_not_gated")
        assert "DELETE /api/system/exchanges/binance/connect" in _detail(resp)["message"]
        db.credentials = {}
        resp = await browser.post(
            "/api/live/credential-delete", json={"exchange_id": "binance", "reason": "rotate keys"}
        )
        assert (resp.status_code, _detail(resp)["code"]) == (404, "credentials_not_found")

    async def test_credential_delete_create_routes_null_keys_to_the_ceremony_under_live_sessions(
        self, db: _Db, backend: _Backend, browser: httpx.AsyncClient
    ) -> None:
        """R31 A1: a null-permission key under a running live session gets the ceremony,
        not the 'delete them directly' guidance the direct path now refuses."""
        db.credentials = {"binance": _creds(permissions=None)}
        backend.sessions = [_live_session()]
        resp = await browser.post(
            "/api/live/credential-delete", json={"exchange_id": "binance", "reason": "rotate"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"status", "approval_id", "code", "expires_at", "params", "note"}
        assert body["status"] == "approval_required"
        approved = await _approve(browser, body["approval_id"], body["code"])
        assert (approved.status_code, _detail(approved)["code"]) == (
            409,
            "credential_delete_gated",
        )
        assert _detail(approved)["running_live_sessions"] == ["s-live"]
        assert "binance" in db.credentials

    async def test_credential_delete_create_answers_the_pinned_shape_and_deletes_on_approval(
        self, db: _Db, backend: _Backend, browser: httpx.AsyncClient
    ) -> None:
        resp = await browser.post(
            "/api/live/credential-delete", json={"exchange_id": "binance", "reason": "rotate keys"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"status", "approval_id", "code", "expires_at", "params", "note"}
        assert body["status"] == "approval_required"
        assert body["params"] == {"exchange_id": "binance", "reason": "rotate keys"}
        approved = await _approve(browser, body["approval_id"], body["code"])
        assert approved.status_code == 200, approved.text
        assert approved.json()["executed"] is True
        assert approved.json()["result"]["deleted"] is True
        assert "binance" not in db.credentials

    async def test_a_running_live_session_gates_the_delete_execution(
        self, db: _Db, backend: _Backend, browser: httpx.AsyncClient
    ) -> None:
        backend.sessions = [_live_session()]
        body = (
            await browser.post(
                "/api/live/credential-delete", json={"exchange_id": "binance", "reason": "rotate"}
            )
        ).json()
        resp = await _approve(browser, body["approval_id"], body["code"])
        assert (resp.status_code, _detail(resp)["code"]) == (409, "credential_delete_gated")
        assert _detail(resp)["running_live_sessions"] == ["s-live"]
        assert "binance" in db.credentials

    async def test_different_pending_params_conflict(
        self, db: _Db, backend: _Backend, browser: httpx.AsyncClient
    ) -> None:
        db.config["live_armed"] = ARMED_ROW
        first = (
            await browser.post(
                "/api/live/session-start", json={"strategy_id": "momentum", "reason": "go live"}
            )
        ).json()
        resp = await browser.post(
            "/api/live/session-start", json={"strategy_id": "breakout", "reason": "go live"}
        )
        assert (resp.status_code, _detail(resp)["code"]) == (409, "approval_conflict")
        # the identical retry dedupes into the same pending approval
        again = (
            await browser.post(
                "/api/live/session-start", json={"strategy_id": "momentum", "reason": "go live"}
            )
        ).json()
        assert again["approval_id"] == first["approval_id"]
