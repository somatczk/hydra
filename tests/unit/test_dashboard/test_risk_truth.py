"""Risk routes serve the engine's RiskService state or say they cannot: no placeholders."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hydra.core import metrics
from hydra.dashboard.routes import risk
from hydra.dashboard.routes._db import install_error_handlers
from hydra.dashboard.routes.risk import router
from hydra.risk.pretrade import RiskConfig
from hydra.risk.state import RiskStateUnavailableError
from hydra.services.trader import LocalTrader
from tests.unit.test_risk.harness import Account, make_account

_GLOBAL_ROW: dict[str, Any] = {
    "scope": "global",
    "max_position_pct": 0.10,
    "max_risk_per_trade": 0.02,
    "max_portfolio_heat": 0.30,
    "max_daily_loss_pct": 0.05,
    "max_drawdown_pct": 0.20,
    "max_concurrent_positions": 10,
    "max_consecutive_losses": 5,
    "kill_switch_active": False,
}


class _Mgr:
    def __init__(self, svc: Any = None) -> None:
        self.risk = svc
        self.reloads = 0

    def list_sessions(self) -> list[Any]:
        return []

    async def reload_risk_config(self) -> None:
        self.reloads += 1


def _make_app(pool: Any = None, session_manager: Any = None) -> FastAPI:
    app = FastAPI()
    app.state.db_pool = pool
    app.state.session_manager = session_manager
    app.state.trader = LocalTrader(pool, session_manager)
    install_error_handlers(app)
    app.include_router(router)
    return app


def _make_pool() -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool, conn


def _refusing_pool() -> MagicMock:
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(side_effect=ConnectionError("refused"))
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


@pytest.fixture(autouse=True)
def _fresh_metrics() -> Iterator[None]:
    metrics._reset_metrics()
    yield
    metrics._reset_metrics()


async def _tier2_account() -> Account:
    """Peak 10000, equity 9480 (5.2% drawdown): tier 2 at the 15% max drawdown."""
    acct = await make_account(RiskConfig(max_daily_loss_pct=Decimal("0.5")))
    ex = await acct.add_session("s1", "10000")
    await acct.mark("60000")
    await ex.create_order("BTCUSDT", "BUY", "MARKET", Decimal("0.1"))
    await acct.mark("54800")
    assert acct.svc.account().tier == 2
    return acct


@pytest.fixture()
async def tier2() -> Account:
    return await _tier2_account()


@pytest.fixture()
async def client(tier2: Account) -> AsyncIterator[httpx.AsyncClient]:
    pool, conn = _make_pool()
    conn.fetchrow = AsyncMock(return_value=_GLOBAL_ROW)
    conn.fetch = AsyncMock(return_value=[{"daily_pnl": -50.0}] * 3)
    app = _make_app(pool=pool, session_manager=_Mgr(tier2.svc))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


class TestCircuitBreakers:
    async def test_live_tiers_from_engine(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/api/risk/circuit-breakers")
        assert resp.status_code == 200
        rows = resp.json()
        assert [r["status"] for r in rows] == ["Tripped", "Tripped", "Normal", "Normal"]
        assert [r["threshold_pct"] for r in rows] == [3.0, 5.0, 10.0, 15.0]
        assert {r["current_value"] for r in rows} == {5.2}
        assert rows[1]["active_since"] is not None
        assert rows[2]["active_since"] is None

    def test_without_engine_is_503(self) -> None:
        for mgr in (None, MagicMock()):
            resp = TestClient(_make_app(session_manager=mgr)).get("/api/risk/circuit-breakers")
            assert resp.status_code == 503
            detail = resp.json()["detail"]
            assert (detail["code"], detail["state"]) == ("risk_service_unavailable", "unavailable")
            assert "Normal" not in resp.text

    def test_no_hardcoded_breaker_list_left(self) -> None:
        assert not hasattr(risk, "_CIRCUIT_BREAKERS")


class TestReset:
    async def test_reset_wrong_tier_is_409(self, client: httpx.AsyncClient) -> None:
        resp = await client.post(
            "/api/risk/circuit-breakers/3/reset", json={"reason": "check", "confirm_tier": 3}
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "tier_not_active"
        assert resp.json()["detail"]["active_tier"] == 2

    @pytest.mark.parametrize(
        ("tier", "body", "code"),
        [
            (9, {"reason": "typo", "confirm_tier": 9}, "invalid_tier"),
            (2, {"reason": "mismatch", "confirm_tier": 3}, "confirm_mismatch"),
            (2, {"reason": "x", "confirm_tier": 2}, "validation_error"),
            (2, {"reason": "ok ok", "confirm_tier": 2, "force": True}, "validation_error"),
        ],
    )
    async def test_reset_validation_is_422(
        self, client: httpx.AsyncClient, tier2: Account, tier: int, body: dict[str, Any], code: str
    ) -> None:
        resp = await client.post(f"/api/risk/circuit-breakers/{tier}/reset", json=body)
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == code
        assert tier2.svc.account().tier == 2

    async def test_reset_audit_failure_is_503_and_changes_nothing(
        self, client: httpx.AsyncClient, tier2: Account
    ) -> None:
        tier2.store.append_event = AsyncMock(side_effect=OSError("audit down"))
        resp = await client.post(
            "/api/risk/circuit-breakers/2/reset", json={"reason": "reviewed", "confirm_tier": 2}
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "audit_unavailable"
        assert tier2.svc.account().tier == 2

    async def test_reset_active_tier(self, client: httpx.AsyncClient, tier2: Account) -> None:
        resp = await client.post(
            "/api/risk/circuit-breakers/2/reset", json={"reason": "reviewed", "confirm_tier": 2}
        )
        assert resp.status_code == 200
        assert [r["status"] for r in resp.json()] == ["Normal"] * 4
        event = tier2.store.events[-1]
        assert (event.kind, event.tier, event.reason, event.actor) == (
            "cb_reset",
            2,
            "reviewed",
            "anonymous",
        )
        assert tier2.store.state.peak_equity == Decimal("9480.0")

    async def test_reset_not_persisted_is_503_and_rolled_back(
        self, client: httpx.AsyncClient, tier2: Account
    ) -> None:
        tier2.store.save_state = AsyncMock(side_effect=OSError("db down"))
        resp = await client.post(
            "/api/risk/circuit-breakers/2/reset", json={"reason": "reviewed", "confirm_tier": 2}
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "reset_not_persisted"
        assert (tier2.svc.account().tier, tier2.store.state.cb.tier) == (2, 2)
        assert [e.kind for e in tier2.store.events[-2:]] == ["cb_reset", "cb_reset_failed"]


class TestPaperAccountReset:
    async def test_reset_rebases_ledger_and_keeps_tier(
        self, client: httpx.AsyncClient, tier2: Account
    ) -> None:
        resp = await client.post(
            "/api/risk/paper-account/reset", json={"reason": "fresh start", "confirm": "RESET"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert (body["equity"], body["peak_equity"], body["current_drawdown"]) == (
            9480.0,
            9480.0,
            0.0,
        )
        assert body["restrictions"]["active_tier"] == 2
        event = tier2.store.events[-1]
        assert (event.kind, event.actor, event.reason) == (
            "paper_reset",
            "anonymous",
            "fresh start",
        )
        assert event.details["old_peak"] == Decimal("10000")

    @pytest.mark.parametrize(
        "body",
        [{"reason": "fresh start", "confirm": "reset"}, {"reason": "fresh start"}],
    )
    async def test_needs_typed_confirmation(
        self, client: httpx.AsyncClient, tier2: Account, body: dict[str, Any]
    ) -> None:
        resp = await client.post("/api/risk/paper-account/reset", json=body)
        assert resp.status_code == 422
        assert "paper_reset" not in [e.kind for e in tier2.store.events]

    async def test_audit_failure_is_503_and_changes_nothing(
        self, client: httpx.AsyncClient, tier2: Account
    ) -> None:
        tier2.store.append_event = AsyncMock(side_effect=RiskStateUnavailableError("down"))
        resp = await client.post(
            "/api/risk/paper-account/reset", json={"reason": "fresh start", "confirm": "RESET"}
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "paper_reset_unavailable"
        assert tier2.store.state.peak_equity == Decimal("10000")


class TestRiskStatus:
    async def test_status_is_live_engine_state(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/api/risk/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["circuit_breakers_state"] == "live"
        assert data["current_drawdown"] == 5.2
        assert data["max_drawdown_limit"] == 15.0
        assert data["equity"] == 9480.0
        assert data["peak_equity"] == 10000.0
        assert data["daily_loss"] == 520.0
        assert data["daily_loss_limit"] == 5000.0
        assert [b["status"] for b in data["circuit_breakers"]][:2] == ["Tripped", "Tripped"]
        assert data["restrictions"]["can_open_new_trades"] is False
        assert data["restrictions"]["size_multiplier"] == 0.5

    async def test_live_source_is_503(self, client: httpx.AsyncClient) -> None:
        for path in ("/api/risk/status?source=live", "/api/risk/var?source=live"):
            resp = await client.get(path)
            assert resp.status_code == 503
            assert resp.json()["detail"]["code"] == "live_disabled"

    def test_status_without_engine_is_503(self) -> None:
        pool, _ = _make_pool()
        resp = TestClient(_make_app(pool=pool)).get("/api/risk/status")
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "risk_service_unavailable"

    async def test_state_unavailable_is_503(self, tier2: Account) -> None:
        tier2.store.save_state = AsyncMock(side_effect=OSError("db down"))
        await tier2.mark("54700")
        app = _make_app(session_manager=_Mgr(tier2.svc))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            resp = await c.get("/api/risk/status")
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "risk_state_unavailable"


class TestEvents:
    async def test_events_from_audit_table(self) -> None:
        pool, conn = _make_pool()
        ts = datetime(2026, 10, 20, 12, tzinfo=UTC)
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "id": 7,
                    "ts": ts,
                    "scope": "paper",
                    "kind": "cb_escalate",
                    "tier": 3,
                    "actor": "risk_engine",
                    "reason": "drawdown 0.1050 >= 0.10",
                    "details": '{"previous_tier": 2}',
                }
            ]
        )
        app = _make_app(pool=pool)
        resp = TestClient(app).get("/api/risk/events?limit=10")
        assert resp.status_code == 200
        assert resp.json()[0]["kind"] == "cb_escalate"
        assert resp.json()[0]["details"] == {"previous_tier": 2}
        assert conn.fetch.await_args.args[1:] == ("paper", 10)

    def test_events_db_down_is_503(self) -> None:
        resp = TestClient(_make_app(pool=_refusing_pool())).get("/api/risk/events")
        assert resp.status_code == 503


class TestLiveStatus:
    def test_kill_switch_unknown_without_pool(self) -> None:
        data = TestClient(_make_app(pool=None)).get("/api/risk/live-status").json()
        assert data["kill_switch_active"] is None
        assert data["kill_switch_state"] == "unknown"
        assert data["running_sessions"] is None
        assert data["circuit_breakers_state"] == "unavailable"
        assert data["circuit_breaker_restrictions"] is None

    def test_kill_switch_unknown_when_db_down(self) -> None:
        data = TestClient(_make_app(pool=_refusing_pool())).get("/api/risk/live-status").json()
        assert data["kill_switch_state"] == "unknown"

    @pytest.mark.parametrize(("flag", "state"), [(True, "active"), (False, "inactive")])
    def test_kill_switch_from_row(self, flag: bool, state: str) -> None:
        pool, conn = _make_pool()
        conn.fetchrow = AsyncMock(return_value={**_GLOBAL_ROW, "kill_switch_active": flag})
        mgr = MagicMock()
        mgr.list_sessions.return_value = [
            MagicMock(status="running"),
            MagicMock(status="stopped"),
            MagicMock(status="running"),
        ]
        data = (
            TestClient(_make_app(pool=pool, session_manager=mgr))
            .get("/api/risk/live-status")
            .json()
        )
        assert data["kill_switch_active"] is flag
        assert data["kill_switch_state"] == state
        assert data["running_sessions"] == 2

    async def test_restrictions_and_latch_from_engine(self, tier2: Account) -> None:
        await tier2.svc.activate_kill_switch("ops", "drill")
        pool, conn = _make_pool()
        conn.fetchrow = AsyncMock(return_value=_GLOBAL_ROW)
        app = _make_app(pool=pool, session_manager=_Mgr(tier2.svc))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            data = (await c.get("/api/risk/live-status")).json()
        assert data["kill_switch_state"] == "active"
        assert data["circuit_breakers_state"] == "live"
        restr = data["circuit_breaker_restrictions"]
        assert (restr["active_tier"], restr["can_open_new_trades"]) == (2, False)


class TestRiskConfig:
    def test_risk_config_get_503_without_db(self) -> None:
        resp = TestClient(_make_app(pool=None)).get("/api/risk/config")
        assert resp.status_code == 503
        assert resp.json()["detail"]["state"] == "unavailable"

    def test_risk_config_missing_global_row_is_503(self) -> None:
        pool, conn = _make_pool()
        conn.fetchrow = AsyncMock(return_value=None)
        resp = TestClient(_make_app(pool=pool)).get("/api/risk/config")
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "risk_config_missing"

    def test_risk_config_from_row(self) -> None:
        pool, conn = _make_pool()
        conn.fetchrow = AsyncMock(return_value={**_GLOBAL_ROW, "kill_switch_active": True})
        data = TestClient(_make_app(pool=pool)).get("/api/risk/config").json()
        assert data["max_drawdown_pct"] == 0.20
        assert data["max_consecutive_losses"] == 5
        assert data["kill_switch_active"] is True
        assert "leverage" not in data and "margin_mode" not in data

    def test_strategy_config_returns_enforced_global_limits(self) -> None:
        pool, conn = _make_pool()
        conn.fetchrow = AsyncMock(return_value=_GLOBAL_ROW)
        resp = TestClient(_make_app(pool=pool)).get("/api/risk/config/strat-x")
        assert resp.status_code == 200
        assert resp.json()["scope"] == "global"
        assert conn.fetchrow.await_args.args[1] == "global"

    def test_strategy_override_put_is_501(self) -> None:
        pool, conn = _make_pool()
        conn.execute = AsyncMock()
        resp = TestClient(_make_app(pool=pool)).put(
            "/api/risk/config/strat-x", json={"max_position_pct": 0.2}
        )
        assert resp.status_code == 501
        assert resp.json()["detail"]["state"] == "not_implemented"
        conn.execute.assert_not_awaited()

    def test_put_risk_config_missing_scope_404(self) -> None:
        pool, conn = _make_pool()
        conn.fetchrow = AsyncMock(return_value=None)
        conn.execute = AsyncMock()
        resp = TestClient(_make_app(pool=pool)).put(
            "/api/risk/config", json={"max_position_pct": 0.2}
        )
        assert resp.status_code == 404
        conn.execute.assert_not_awaited()

    def test_put_risk_config_db_error_is_503(self) -> None:
        resp = TestClient(_make_app(pool=_refusing_pool())).put(
            "/api/risk/config", json={"max_position_pct": 0.2}
        )
        assert resp.status_code == 503

    def test_put_writes_max_consecutive_losses(self) -> None:
        pool, conn = _make_pool()
        updated = {**_GLOBAL_ROW, "max_consecutive_losses": 3}
        conn.fetchrow = AsyncMock(side_effect=[_GLOBAL_ROW, updated])
        conn.execute = AsyncMock()
        resp = TestClient(_make_app(pool=pool)).put(
            "/api/risk/config", json={"max_consecutive_losses": 3}
        )
        assert resp.status_code == 200
        assert resp.json()["max_consecutive_losses"] == 3
        args = conn.execute.await_args.args
        assert "max_consecutive_losses = $7" in args[0]
        assert (args[7], args[-1]) == (3, "global")

    def test_put_empty_body_is_400_invalid(self) -> None:
        pool, _ = _make_pool()
        resp = TestClient(_make_app(pool=pool)).put("/api/risk/config", json={})
        assert resp.status_code == 400
        assert resp.json()["detail"]["state"] == "invalid"


class TestVar:
    async def test_var_portfolio_value_is_account_equity(self, client: httpx.AsyncClient) -> None:
        data = (await client.get("/api/risk/var")).json()
        assert data["portfolio_value"] == 9480.0
        assert data["sample_days"] == 3
        assert data["var_95"] == 0.0

    async def test_var_exact_percentile(self, tier2: Account) -> None:
        pool, conn = _make_pool()
        pnls = [-300.0, -200.0, -100.0] + [10.0 * i for i in range(17)]
        conn.fetch = AsyncMock(return_value=[{"daily_pnl": v} for v in pnls])
        app = _make_app(pool=pool, session_manager=_Mgr(tier2.svc))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            data = (await c.get("/api/risk/var")).json()
        assert data["sample_days"] == 20
        assert (data["var_95"], data["var_99"], data["cvar_95"]) == (200.0, 300.0, 250.0)

    def test_var_without_engine_is_503(self) -> None:
        pool, _ = _make_pool()
        resp = TestClient(_make_app(pool=pool)).get("/api/risk/var")
        assert resp.status_code == 503
