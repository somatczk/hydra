"""Tests for the Hydra Dashboard FastAPI routes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hydra.dashboard.api import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_returns_200(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


class TestStrategies:
    def test_list_strategies(self, client: TestClient) -> None:
        resp = client.get("/api/strategies")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0
        assert "name" in data[0]
        assert "performance" in data[0]

    def test_get_strategy_detail(self, client: TestClient) -> None:
        resp = client.get("/api/strategies/strat-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "LSTM Momentum"

    def test_get_strategy_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/strategies/nonexistent")
        assert resp.status_code == 404

    def test_toggle_strategy(self, client: TestClient) -> None:
        resp = client.post("/api/strategies/strat-1/toggle")
        assert resp.status_code == 200
        data = resp.json()
        assert "enabled" in data

    def test_update_strategy_config(self, client: TestClient) -> None:
        resp = client.put(
            "/api/strategies/strat-1",
            json={"config_yaml": "strategy:\n  updated: true\n"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "updated: true" in data["config_yaml"]

    def test_strategy_performance(self, client: TestClient) -> None:
        resp = client.get("/api/strategies/strat-1/performance")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_pnl" in data
        assert "win_rate" in data


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------


class TestPortfolio:
    def test_portfolio_summary(self, client: TestClient) -> None:
        resp = client.get("/api/portfolio/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_value" in data
        assert "unrealized_pnl" in data
        assert "realized_pnl" in data

    def test_portfolio_positions(self, client: TestClient) -> None:
        resp = client.get("/api/portfolio/positions")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_equity_curve(self, client: TestClient) -> None:
        resp = client.get("/api/portfolio/equity-curve")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0
        assert "timestamp" in data[0]
        assert "value" in data[0]

    def test_daily_pnl(self, client: TestClient) -> None:
        resp = client.get("/api/portfolio/daily-pnl")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_monthly_returns(self, client: TestClient) -> None:
        resp = client.get("/api/portfolio/monthly-returns")
        assert resp.status_code == 200

    def test_attribution(self, client: TestClient) -> None:
        resp = client.get("/api/portfolio/attribution")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------


class TestRisk:
    def test_risk_status(self, client: TestClient) -> None:
        resp = client.get("/api/risk/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "current_drawdown" in data
        assert "circuit_breakers" in data

    def test_circuit_breakers(self, client: TestClient) -> None:
        resp = client.get("/api/risk/circuit-breakers")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 4

    def test_reset_circuit_breaker(self, client: TestClient) -> None:
        resp = client.post("/api/risk/circuit-breakers/3/reset")
        assert resp.status_code == 200
        data = resp.json()
        assert data["tier"] == 3
        assert data["status"] == "Normal"

    def test_reset_circuit_breaker_not_found(self, client: TestClient) -> None:
        resp = client.post("/api/risk/circuit-breakers/99/reset")
        assert resp.status_code == 404

    def test_var_estimate(self, client: TestClient) -> None:
        resp = client.get("/api/risk/var")
        assert resp.status_code == 200
        data = resp.json()
        assert "var_95" in data
        assert "var_99" in data


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TestModels:
    def test_list_models(self, client: TestClient) -> None:
        resp = client.get("/api/models")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0
        assert "name" in data[0]
        assert "stage" in data[0]

    def test_get_model_detail(self, client: TestClient) -> None:
        resp = client.get("/api/models/model-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "LSTM Price Predictor"

    def test_promote_model(self, client: TestClient) -> None:
        resp = client.post("/api/models/model-3/promote")
        assert resp.status_code == 200
        data = resp.json()
        assert data["stage"] == "Production"

    def test_rollback_model(self, client: TestClient) -> None:
        resp = client.post("/api/models/model-1/rollback")
        assert resp.status_code == 200
        data = resp.json()
        assert data["stage"] == "Staging"

    def test_retrain_models(self, client: TestClient) -> None:
        resp = client.post("/api/models/retrain")
        assert resp.status_code == 200
        data = resp.json()
        assert "task_id" in data


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------


class TestBacktest:
    def test_run_backtest(self, client: TestClient) -> None:
        resp = client.post(
            "/api/backtest/run",
            json={
                "strategy_id": "strat-1",
                "start_date": "2026-01-01",
                "end_date": "2026-03-01",
                "initial_capital": 10000,
            },
        )
        assert resp.status_code == 202
        data = resp.json()
        assert "task_id" in data
        assert data["status"] == "queued"

    def test_backtest_status(self, client: TestClient) -> None:
        # First start a backtest to get a valid task_id
        run_resp = client.post(
            "/api/backtest/run",
            json={
                "strategy_id": "strat-1",
                "start_date": "2026-01-01",
                "end_date": "2026-03-01",
            },
        )
        task_id = run_resp.json()["task_id"]
        resp = client.get(f"/api/backtest/status/{task_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_id"] == task_id

    def test_backtest_status_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/backtest/status/nonexistent")
        assert resp.status_code == 404

    def test_list_results(self, client: TestClient) -> None:
        resp = client.get("/api/backtest/results")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_get_result_detail(self, client: TestClient) -> None:
        resp = client.get("/api/backtest/results/bt-1")
        assert resp.status_code == 200
        data = resp.json()
        assert "equity_curve" in data
        assert "trades" in data


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


class TestBuilder:
    def test_list_indicators(self, client: TestClient) -> None:
        resp = client.get("/api/builder/indicators")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0
        assert "id" in data[0]
        assert "parameters" in data[0]

    def test_preview_strategy(self, client: TestClient) -> None:
        resp = client.post(
            "/api/builder/preview",
            json={
                "conditions": {
                    "entry": [{"indicator": "rsi", "condition": "crosses_below", "value": 30}],
                    "exit": [{"indicator": "rsi", "condition": "crosses_above", "value": 70}],
                },
                "timeframe": "1h",
                "pair": "BTC/USDT",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "signals" in data
        assert "metrics" in data

    def test_save_strategy(self, client: TestClient) -> None:
        resp = client.post(
            "/api/builder/save",
            json={
                "name": "Test Strategy",
                "description": "A test strategy",
                "conditions": {
                    "entry": [{"indicator": "rsi", "condition": "crosses_below", "value": 30}],
                    "exit": [{"indicator": "rsi", "condition": "crosses_above", "value": 70}],
                },
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "strategy_id" in data
        assert "config_yaml" in data


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------


class TestSystem:
    def test_get_config(self, client: TestClient) -> None:
        resp = client.get("/api/system/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "trading_mode" in data

    def test_update_config(self, client: TestClient) -> None:
        resp = client.put(
            "/api/system/config",
            json={"trading_mode": "testnet"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["trading_mode"] == "testnet"

    def test_exchanges(self, client: TestClient) -> None:
        resp = client.get("/api/system/exchanges")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_system_health(self, client: TestClient) -> None:
        resp = client.get("/api/system/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "overall" in data
        assert "services" in data
