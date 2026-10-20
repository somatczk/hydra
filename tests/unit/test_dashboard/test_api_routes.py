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
    def test_health_reports_liveness_with_the_version(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 503
        assert set(resp.json()) == {"status", "version"}


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


class TestStrategies:
    def test_empty_config_dir_lists_no_strategies(self, client: TestClient) -> None:
        resp = client.get("/api/strategies")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_placeholder_strategy_is_gone(self, client: TestClient) -> None:
        resp = client.get("/api/strategies/strat-1")
        assert resp.status_code == 503
        assert resp.json()["detail"]["state"] == "unavailable"

    def test_toggle_placeholder_is_404(self, client: TestClient) -> None:
        resp = client.post("/api/strategies/strat-1/toggle")
        assert resp.status_code == 404

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

    def test_placeholder_performance_is_404(self, client: TestClient) -> None:
        resp = client.get("/api/strategies/strat-1/performance")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------


class TestBacktest:
    def test_unknown_strategy_is_404_not_a_default_rsi_run(self, client: TestClient) -> None:
        resp = client.post(
            "/api/backtest/run",
            json={
                "strategy_id": "strat-1",
                "start_date": "2026-01-01",
                "end_date": "2026-03-01",
                "initial_capital": 10000,
            },
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "strategy_not_found"

    def test_known_strategy_without_database_is_503(
        self, client: TestClient, strategies_dir: Path
    ) -> None:
        (strategies_dir / "rsi.yaml").write_text(
            "id: rsi\n"
            "name: RSI\n"
            "strategy_class: hydra.strategy.builtin.rule_based.RuleBasedStrategy\n"
        )
        resp = client.post(
            "/api/backtest/run",
            json={"strategy_id": "rsi", "start_date": "2026-01-01", "end_date": "2026-03-01"},
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]["state"] == "unavailable"

    def test_backtest_status_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/backtest/status/nonexistent")
        assert resp.status_code == 404

    def test_list_results_without_database_is_503(self, client: TestClient) -> None:
        resp = client.get("/api/backtest/results")
        assert resp.status_code == 503

    def test_uncached_detail_without_database_is_503(self, client: TestClient) -> None:
        resp = client.get("/api/backtest/results/bt-1")
        assert resp.status_code == 503


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

    def test_preview_without_database_is_503(self, client: TestClient) -> None:
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
        assert resp.status_code == 503
        assert resp.json()["detail"]["state"] == "unavailable"

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
    @pytest.mark.parametrize("path", ["/api/system/config", "/api/system/exchanges"])
    def test_reads_without_db_are_503(self, client: TestClient, path: str) -> None:
        resp = client.get(path)
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "database_unavailable"

    def test_update_config_without_db_is_503(self, client: TestClient) -> None:
        resp = client.put("/api/system/config", json={"trading_mode": "live"})
        assert resp.status_code == 503

    def test_system_health(self, client: TestClient) -> None:
        resp = client.get("/api/system/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "overall" in data
        assert "services" in data
