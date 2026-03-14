"""Tests for strategy dashboard routes with DB and fallback paths.

Tests cover:
- List strategies returns correct shape with/without pool
- Toggle updates enabled status with/without pool
- Single strategy lookup returns 404 for unknown ID
- ``_status_from_enabled`` helper
- ``_row_to_strategy`` conversion
- DB exception fallback behaviour
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hydra.dashboard.routes.strategies import (
    _row_to_strategy,
    _status_from_enabled,
    router,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_app(pool: object | None = None) -> FastAPI:
    """Create a minimal FastAPI app with the strategies router and optional pool."""
    test_app = FastAPI()
    test_app.include_router(router)
    test_app.state.db_pool = pool
    return test_app


@pytest.fixture()
def client_no_pool() -> TestClient:
    return TestClient(_make_app(pool=None))


def _make_mock_pool() -> tuple[MagicMock, AsyncMock]:
    pool = MagicMock()
    conn = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx
    return pool, conn


# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------


class TestStatusFromEnabled:
    def test_active_when_enabled(self) -> None:
        assert _status_from_enabled(True) == "Active"

    def test_paused_when_disabled(self) -> None:
        assert _status_from_enabled(False) == "Paused"


class TestRowToStrategy:
    def test_known_strategy_name(self) -> None:
        row = {
            "id": "s1",
            "name": "LSTM Momentum",
            "enabled": True,
            "total_pnl": 1000.25,
            "win_rate": 72.36,
            "total_trades": 50,
        }
        result = _row_to_strategy(row)
        assert result["id"] == "s1"
        assert result["name"] == "LSTM Momentum"
        assert "LSTM" in result["description"]
        assert result["status"] == "Active"
        assert result["enabled"] is True
        assert result["performance"]["total_pnl"] == 1000.25
        assert result["performance"]["win_rate"] == 72.4
        assert result["performance"]["total_trades"] == 50
        assert result["performance"]["sharpe_ratio"] == 0.0
        assert result["performance"]["max_drawdown"] == 0.0

    def test_unknown_strategy_name_uses_default_description(self) -> None:
        row = {
            "id": "s99",
            "name": "MyCustomStrat",
            "enabled": False,
            "total_pnl": 0.0,
            "win_rate": 0.0,
            "total_trades": 0,
        }
        result = _row_to_strategy(row)
        assert result["description"] == "Custom strategy"
        assert result["status"] == "Paused"


# ---------------------------------------------------------------------------
# Fallback (pool=None)
# ---------------------------------------------------------------------------


class TestStrategiesFallback:
    def test_list_returns_all_strategies(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.get("/api/strategies")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 4
        first = data[0]
        assert "id" in first
        assert "name" in first
        assert "description" in first
        assert "status" in first
        assert "performance" in first

    def test_get_known_strategy(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.get("/api/strategies/strat-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "LSTM Momentum"
        assert data["id"] == "strat-1"

    def test_get_unknown_strategy_returns_404(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.get("/api/strategies/does-not-exist")
        assert resp.status_code == 404

    def test_toggle_strategy(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.post("/api/strategies/strat-1/toggle")
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert "enabled" in data
        assert data["id"] == "strat-1"

    def test_toggle_unknown_strategy_returns_404(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.post("/api/strategies/nonexistent/toggle")
        assert resp.status_code == 404

    def test_update_strategy_config(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.put(
            "/api/strategies/strat-1",
            json={"config_yaml": "new:\n  config: true\n"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "new:" in data["config_yaml"]

    def test_update_unknown_strategy_returns_404(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.put(
            "/api/strategies/nonexistent",
            json={"config_yaml": "anything"},
        )
        assert resp.status_code == 404

    def test_performance_endpoint(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.get("/api/strategies/strat-1/performance")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_pnl" in data
        assert "win_rate" in data
        assert "total_trades" in data
        assert "sharpe_ratio" in data
        assert "max_drawdown" in data

    def test_performance_unknown_strategy_returns_404(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.get("/api/strategies/nonexistent/performance")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DB path (mock pool)
# ---------------------------------------------------------------------------


class TestStrategiesWithPool:
    def test_list_strategies_from_db(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "id": "db-1",
                    "name": "LSTM Momentum",
                    "exchange_id": "binance",
                    "enabled": True,
                    "total_pnl": 500.0,
                    "total_trades": 10,
                    "win_rate": 60.0,
                },
            ]
        )

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/strategies")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == "db-1"
        assert data[0]["performance"]["total_pnl"] == 500.0

    def test_list_strategies_db_exception_falls_back(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(side_effect=RuntimeError("DB down"))

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/strategies")
        assert resp.status_code == 200
        data = resp.json()
        # Falls back to placeholder strategies
        assert isinstance(data, list)
        assert len(data) >= 4

    def test_get_strategy_from_db(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetchrow = AsyncMock(
            return_value={
                "id": "db-strat",
                "name": "Mean Reversion",
                "exchange_id": "bybit",
                "enabled": False,
                "total_pnl": 200.0,
                "total_trades": 5,
                "win_rate": 40.0,
            }
        )

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/strategies/db-strat")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "db-strat"
        assert data["status"] == "Paused"

    def test_get_strategy_not_found_in_db(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetchrow = AsyncMock(return_value=None)

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/strategies/nonexistent")
        assert resp.status_code == 404

    def test_get_strategy_db_exception_falls_back(self) -> None:
        """On DB error, falls back to in-memory lookup for known strategies."""
        pool, conn = _make_mock_pool()
        conn.fetchrow = AsyncMock(side_effect=RuntimeError("DB down"))

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/strategies/strat-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "LSTM Momentum"

    def test_toggle_strategy_with_pool_uses_memory(self) -> None:
        """Toggle is always in-memory now (no seed_strategies table)."""
        pool, _conn = _make_mock_pool()

        client = TestClient(_make_app(pool=pool))
        resp = client.post("/api/strategies/strat-1/toggle")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "strat-1"
        assert "enabled" in data

    def test_toggle_strategy_not_found(self) -> None:
        pool, _conn = _make_mock_pool()

        client = TestClient(_make_app(pool=pool))
        resp = client.post("/api/strategies/nonexistent/toggle")
        assert resp.status_code == 404
