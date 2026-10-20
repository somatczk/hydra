"""Risk routes report real numbers or say they cannot: no fabricated breakers or defaults."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hydra.dashboard.routes import risk
from hydra.dashboard.routes.risk import router

_GLOBAL_ROW: dict[str, Any] = {
    "scope": "global",
    "max_position_pct": 0.10,
    "max_risk_per_trade": 0.02,
    "max_portfolio_heat": 0.06,
    "max_daily_loss_pct": 0.05,
    "max_drawdown_pct": 0.20,
    "max_concurrent_positions": 10,
    "kill_switch_active": False,
}


def _make_app(pool: Any = None, session_manager: Any = None) -> FastAPI:
    app = FastAPI()
    app.state.db_pool = pool
    app.state.session_manager = session_manager
    app.state.system_config = {"paper_capital": 10000.0}
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


class TestCircuitBreakers:
    def test_circuit_breakers_endpoint_reports_unavailable(self) -> None:
        resp = TestClient(_make_app()).get("/api/risk/circuit-breakers")
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert detail["state"] == "unavailable"
        assert detail["code"] == "circuit_breaker_manager_not_wired"
        assert "Normal" not in resp.text
        assert "Warning" not in resp.text

    def test_reset_is_501_and_mutates_nothing(self) -> None:
        pool, conn = _make_pool()
        conn.fetchrow = AsyncMock(side_effect=[_GLOBAL_ROW, None])
        conn.fetchval = AsyncMock(return_value=0)
        conn.fetch = AsyncMock(return_value=[])
        client = TestClient(_make_app(pool=pool))

        resp = client.post("/api/risk/circuit-breakers/3/reset")
        assert resp.status_code == 501
        assert resp.json()["detail"]["state"] == "not_implemented"

        status = client.get("/api/risk/status").json()
        assert status["circuit_breakers"] is None
        assert status["circuit_breakers_state"] == "unavailable"

    def test_no_hardcoded_breaker_list_left(self) -> None:
        assert not hasattr(risk, "_CIRCUIT_BREAKERS")


class TestRiskStatus:
    def test_status_with_real_numbers_and_null_breakers(self) -> None:
        pool, conn = _make_pool()
        conn.fetchrow = AsyncMock(
            return_value={"max_drawdown_pct": 0.20, "max_daily_loss_pct": 0.05}
        )
        conn.fetchval = AsyncMock(return_value=-120.0)
        conn.fetch = AsyncMock(
            return_value=[
                {"total_value": 10000.0},
                {"total_value": 11000.0},
                {"total_value": 9900.0},
            ]
        )
        resp = TestClient(_make_app(pool=pool)).get("/api/risk/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_drawdown"] == 10.0
        assert data["daily_loss"] == 120.0
        assert data["daily_loss_limit"] == 500.0
        assert data["max_drawdown_limit"] == 20.0
        assert data["circuit_breakers"] is None
        assert data["circuit_breakers_state"] == "unavailable"

    def test_status_db_down_is_503(self) -> None:
        resp = TestClient(_make_app(pool=_refusing_pool())).get("/api/risk/status")
        assert resp.status_code == 503
        assert resp.json()["detail"]["state"] == "unavailable"

    def test_status_no_pool_is_503(self) -> None:
        resp = TestClient(_make_app(pool=None)).get("/api/risk/status")
        assert resp.status_code == 503

    def test_status_missing_global_row_is_503_not_nameerror(self) -> None:
        pool, conn = _make_pool()
        conn.fetchrow = AsyncMock(return_value=None)
        resp = TestClient(_make_app(pool=pool)).get("/api/risk/status")
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert detail["code"] == "risk_config_missing"
        assert detail["state"] == "unavailable"


class TestLiveStatus:
    def test_kill_switch_unknown_without_pool(self) -> None:
        resp = TestClient(_make_app(pool=None)).get("/api/risk/live-status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["kill_switch_active"] is None
        assert data["kill_switch_state"] == "unknown"
        assert data["running_sessions"] is None
        assert data["circuit_breakers_state"] == "unavailable"

    def test_kill_switch_unknown_when_db_down(self) -> None:
        resp = TestClient(_make_app(pool=_refusing_pool())).get("/api/risk/live-status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["kill_switch_active"] is None
        assert data["kill_switch_state"] == "unknown"

    def test_kill_switch_unknown_when_row_missing(self) -> None:
        pool, conn = _make_pool()
        conn.fetchrow = AsyncMock(return_value=None)
        data = TestClient(_make_app(pool=pool)).get("/api/risk/live-status").json()
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


class TestRiskConfig:
    def test_risk_config_get_503_without_db(self) -> None:
        resp = TestClient(_make_app(pool=None)).get("/api/risk/config")
        assert resp.status_code == 503
        assert resp.json()["detail"]["state"] == "unavailable"

    def test_risk_config_get_503_when_db_down(self) -> None:
        resp = TestClient(_make_app(pool=_refusing_pool())).get("/api/risk/config")
        assert resp.status_code == 503

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
        assert data["kill_switch_active"] is True

    def test_strategy_config_falls_back_to_global_row(self) -> None:
        pool, conn = _make_pool()
        conn.fetchrow = AsyncMock(side_effect=[None, _GLOBAL_ROW])
        resp = TestClient(_make_app(pool=pool)).get("/api/risk/config/strat-x")
        assert resp.status_code == 200
        assert resp.json()["scope"] == "global"

    def test_strategy_config_db_error_is_503(self) -> None:
        pool, conn = _make_pool()
        conn.fetchrow = AsyncMock(side_effect=asyncpg.PostgresError("boom"))
        resp = TestClient(_make_app(pool=pool)).get("/api/risk/config/strat-x")
        assert resp.status_code == 503

    def test_put_risk_config_missing_scope_404(self) -> None:
        pool, conn = _make_pool()
        conn.fetchrow = AsyncMock(return_value=None)
        conn.execute = AsyncMock()
        resp = TestClient(_make_app(pool=pool)).put(
            "/api/risk/config", json={"max_position_pct": 0.2}
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["state"] == "not_found"
        conn.execute.assert_not_awaited()

    def test_put_risk_config_db_error_is_503(self) -> None:
        resp = TestClient(_make_app(pool=_refusing_pool())).put(
            "/api/risk/config", json={"max_position_pct": 0.2}
        )
        assert resp.status_code == 503

    def test_put_risk_config_writes_and_reads_back(self) -> None:
        pool, conn = _make_pool()
        updated = {**_GLOBAL_ROW, "max_position_pct": 0.2}
        conn.fetchrow = AsyncMock(side_effect=[_GLOBAL_ROW, updated])
        conn.execute = AsyncMock()
        resp = TestClient(_make_app(pool=pool)).put(
            "/api/risk/config", json={"max_position_pct": 0.2}
        )
        assert resp.status_code == 200
        assert resp.json()["max_position_pct"] == 0.2
        args = conn.execute.await_args.args
        assert args[1] == 0.2
        assert args[-1] == "global"

    def test_put_empty_body_is_400_invalid(self) -> None:
        pool, _ = _make_pool()
        resp = TestClient(_make_app(pool=pool)).put("/api/risk/config", json={})
        assert resp.status_code == 400
        assert resp.json()["detail"]["state"] == "invalid"


class TestVar:
    def test_var_db_error_is_503(self) -> None:
        resp = TestClient(_make_app(pool=_refusing_pool())).get("/api/risk/var")
        assert resp.status_code == 503
        assert resp.json()["detail"]["state"] == "unavailable"

    def test_var_insufficient_history_reports_sample_days(self) -> None:
        pool, conn = _make_pool()
        conn.fetchval = AsyncMock(side_effect=[0, 0])
        conn.fetch = AsyncMock(return_value=[{"daily_pnl": -50.0}] * 3)
        data = TestClient(_make_app(pool=pool)).get("/api/risk/var").json()
        assert data["sample_days"] == 3
        assert data["var_95"] == 0.0
        assert data["portfolio_value"] == 10000.0

    def test_var_exact_percentile(self) -> None:
        pool, conn = _make_pool()
        conn.fetchval = AsyncMock(side_effect=[100.0, 10.0])
        pnls = [-300.0, -200.0, -100.0] + [10.0 * i for i in range(17)]
        conn.fetch = AsyncMock(return_value=[{"daily_pnl": v} for v in pnls])
        data = TestClient(_make_app(pool=pool)).get("/api/risk/var").json()
        # n=20 -> idx_95 = 1 (second worst day), idx_99 = 0 (worst day)
        assert data["sample_days"] == 20
        assert data["var_95"] == 200.0
        assert data["var_99"] == 300.0
        assert data["cvar_95"] == 250.0
        assert data["portfolio_value"] == 10090.0
