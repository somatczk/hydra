"""Risk config routes: validation, spot-only contract, and reload into the running RiskService."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hydra.core.types import Side
from hydra.dashboard.api import app
from hydra.dashboard.routes.risk import router
from hydra.execution.paper_trading import PaperTradingExecutor
from hydra.risk.pretrade import OPEN
from hydra.risk.service import RiskService
from hydra.risk.state import InMemoryRiskStateStore, LoadedConfig, risk_config_from_row
from hydra.services.trader import LocalTrader
from tests.unit.test_risk.harness import T0, Clock, order, rules_cache

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Risk config endpoints
# ---------------------------------------------------------------------------


class TestGetRiskConfig:
    def test_get_risk_config_without_db_is_503(self, client: TestClient) -> None:
        app.state.db_pool = None
        resp = client.get("/api/risk/config")
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert detail["state"] == "unavailable"
        assert detail["code"] == "database_unavailable"


class TestUpdateRiskConfig:
    def test_update_risk_config_requires_db(self, client: TestClient) -> None:
        """Without DB pool, PUT returns 503."""
        app.state.db_pool = None
        resp = client.put(
            "/api/risk/config",
            json={"max_position_pct": 0.20},
        )
        assert resp.status_code == 503

    def test_update_risk_config_empty_body(self, client: TestClient) -> None:
        """Empty update body should return 400."""
        app.state.db_pool = None
        resp = client.put("/api/risk/config", json={})
        assert resp.status_code in (400, 503)  # 503 if no DB, 400 if DB but empty


class TestRiskConfigValidation:
    def test_rejects_out_of_range_values(self, client: TestClient) -> None:
        resp = client.put(
            "/api/risk/config",
            json={"max_drawdown_pct": 2.0},  # > 1.0, invalid
        )
        assert resp.status_code == 422  # Pydantic validation error

    def test_rejects_negative_values(self, client: TestClient) -> None:
        resp = client.put(
            "/api/risk/config",
            json={"max_position_pct": -0.5},
        )
        assert resp.status_code == 422


class TestLiveRiskStatus:
    def test_live_risk_status_without_db_reports_unknown(self, client: TestClient) -> None:
        app.state.db_pool = None
        resp = client.get("/api/risk/live-status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["kill_switch_active"] is None
        assert data["kill_switch_state"] == "unknown"


class TestExistingRiskEndpoints:
    """Without a DB the risk endpoints report unavailable instead of defaults."""

    @pytest.mark.parametrize("path", ["/api/risk/status", "/api/risk/var"])
    def test_without_db_is_503(self, client: TestClient, path: str) -> None:
        app.state.db_pool = None
        resp = client.get(path)
        assert resp.status_code == 503
        assert resp.json()["detail"]["state"] == "unavailable"

    def test_circuit_breakers_unavailable(self, client: TestClient) -> None:
        resp = client.get("/api/risk/circuit-breakers")
        assert resp.status_code == 503
        assert resp.json()["detail"]["state"] == "unavailable"


class TestSpotOnlyConfigContract:
    @pytest.mark.parametrize(
        "body",
        [{"leverage": 5}, {"margin_mode": "cross"}, {"max_drawdown_pct": 0}],
    )
    def test_rejected_fields(self, client: TestClient, body: dict[str, object]) -> None:
        resp = client.put("/api/risk/config", json=body)
        assert resp.status_code == 422

    def test_strategy_override_put_is_501(self, client: TestClient) -> None:
        resp = client.put("/api/risk/config/strat-x", json={"max_position_pct": 0.2})
        assert resp.status_code == 501


class _RowStore(InMemoryRiskStateStore):
    """Reads limits from the same row the route's UPDATE writes."""

    def __init__(self, row: dict[str, Any]) -> None:
        super().__init__(paper_capital=Decimal("10000"))
        self.row = row

    async def load_config(self) -> LoadedConfig:
        return LoadedConfig(risk_config_from_row(self.row), False, self.paper_capital)


class _Mgr:
    def __init__(self, svc: RiskService) -> None:
        self.risk = svc

    def list_sessions(self) -> list[Any]:
        return []

    async def reload_risk_config(self) -> None:
        return None


async def test_put_reload_propagates_to_risk_service() -> None:
    row: dict[str, Any] = {
        "scope": "global",
        "max_position_pct": Decimal("0.10"),
        "max_risk_per_trade": Decimal("0.02"),
        "max_portfolio_heat": Decimal("0.30"),
        "max_daily_loss_pct": Decimal("0.03"),
        "max_drawdown_pct": Decimal("0.15"),
        "max_concurrent_positions": 10,
        "max_consecutive_losses": 5,
        "kill_switch_active": False,
    }

    async def execute(query: str, *args: Any) -> None:
        names = re.findall(r"(\w+) = \$\d", query.split("WHERE")[0])
        row.update({n: v for n, v in zip(names, args, strict=False)})

    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=lambda *a: dict(row))
    conn.execute = AsyncMock(side_effect=execute)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    clock = Clock()
    svc = RiskService(_RowStore(row), clock=clock, market_rules=rules_cache(clock))
    await svc.load()
    await svc.recovery_complete()
    ex = PaperTradingExecutor(initial_balances={"USDT": Decimal("10000")})
    await svc.register_session(
        "s1", ex, allocated=Decimal("10000"), symbols=["BTCUSDT"], exchange_id="binance"
    )
    await svc.on_mark("binance", "BTCUSDT", Decimal("60000"), T0)
    probe = order(Side.BUY, "0.01")
    assert (await svc.check(probe, "s1", OPEN)).approved is True

    test_app = FastAPI()
    test_app.state.db_pool = pool
    test_app.state.session_manager = _Mgr(svc)
    test_app.state.trader = LocalTrader(pool, _Mgr(svc))
    test_app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=test_app), base_url="http://t"
    ) as c:
        resp = await c.put(
            "/api/risk/config", json={"max_position_pct": 0.05, "max_consecutive_losses": 3}
        )
    assert resp.status_code == 200
    assert resp.json()["max_consecutive_losses"] == 3
    assert svc.config.max_consecutive_losses == 3
    decision = await svc.check(probe, "s1", OPEN)
    assert (decision.approved, decision.check) == (False, "position_size")
