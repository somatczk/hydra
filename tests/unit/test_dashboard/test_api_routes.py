"""Tests for the Hydra Dashboard FastAPI routes."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

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
        data = resp.json()
        assert "status" in data
        # Without a DB pool the health endpoint reports degraded status
        assert data["status"] in ("ok", "degraded")
        assert "db" in data


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

    def test_update_strategy_config(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(
            "id: test_strat_api\n"
            "name: Test\n"
            "strategy_class: hydra.strategy.builtin.rule_based.RuleBasedStrategy\n"
            "enabled: false\n"
            "symbols:\n"
            "  - BTCUSDT\n"
            "exchange:\n"
            "  exchange_id: binance\n"
            "  market_type: SPOT\n"
            "timeframes:\n"
            "  primary: 1h\n"
            "parameters:\n"
            "  required_history: 50\n"
            "  rules:\n"
            "    entry_long:\n"
            "      operator: AND\n"
            "      conditions:\n"
            "        - indicator: rsi\n"
            "          params: {period: 14}\n"
            "          comparator: less_than\n"
            "          value: 30\n"
        )
        with patch("hydra.dashboard.routes.strategies._CONFIG_DIR", tmp_path):
            client = TestClient(app)
            resp = client.put(
                "/api/strategies/test_strat_api",
                json={
                    "name": "Updated Test",
                    "description": "",
                    "exchange_id": "binance",
                    "symbol": "BTCUSDT",
                    "rules": {
                        "entry_long": {
                            "operator": "AND",
                            "conditions": [
                                {
                                    "indicator": "rsi",
                                    "params": {"period": 14},
                                    "comparator": "less_than",
                                    "value": 30,
                                }
                            ],
                        },
                        "exit_long": None,
                        "entry_short": None,
                        "exit_short": None,
                    },
                    "timeframes": {"primary": "1h"},
                    "risk": {
                        "stop_loss_method": "atr",
                        "stop_loss_value": 2.0,
                        "take_profit_method": "atr",
                        "take_profit_value": 3.0,
                        "sizing_method": "fixed_fractional",
                        "sizing_params": {
                            "risk_per_trade_pct": 1.0,
                            "max_position_pct": 10.0,
                        },
                    },
                    "enable_immediately": False,
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "test_strat_api"
        assert data["name"] == "Updated Test"
        assert "config_path" in data

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
    def test_list_models_empty(self, client: TestClient) -> None:
        resp = client.get("/api/models")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_get_model_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/models/nonexistent")
        assert resp.status_code == 404

    def test_promote_model_not_found(self, client: TestClient) -> None:
        resp = client.post("/api/models/nonexistent/promote")
        assert resp.status_code == 404

    def test_rollback_model_not_found(self, client: TestClient) -> None:
        resp = client.post("/api/models/nonexistent/rollback")
        assert resp.status_code == 404

    def test_retrain_model_not_found(self, client: TestClient) -> None:
        resp = client.post("/api/models/nonexistent/retrain")
        assert resp.status_code == 404


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
        resp = client.get("/api/strategies/indicators")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0
        assert "name" in data[0]
        assert "params" in data[0]

    def test_preview_strategy(self, client: TestClient) -> None:
        resp = client.post(
            "/api/strategies/preview",
            json={
                "rules": {
                    "entry_long": {
                        "operator": "AND",
                        "conditions": [
                            {"indicator": "rsi", "comparator": "crosses_below", "value": 30},
                        ],
                    },
                    "exit_long": {
                        "operator": "AND",
                        "conditions": [
                            {"indicator": "rsi", "comparator": "crosses_above", "value": 70},
                        ],
                    },
                },
                "timeframe": "1h",
                "symbol": "BTCUSDT",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "signals" in data
        assert "metrics" in data

    def test_save_strategy(self, client: TestClient) -> None:
        resp = client.post(
            "/api/strategies/save",
            json={
                "name": "Test Strategy",
                "description": "A test strategy",
                "rules": {
                    "entry_long": {
                        "operator": "AND",
                        "conditions": [
                            {"indicator": "rsi", "comparator": "crosses_below", "value": 30},
                        ],
                    },
                    "exit_long": {
                        "operator": "AND",
                        "conditions": [
                            {"indicator": "rsi", "comparator": "crosses_above", "value": 70},
                        ],
                    },
                },
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert "config_path" in data


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
