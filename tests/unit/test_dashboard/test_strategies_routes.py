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

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
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
        assert len(data) >= 1
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

    def test_update_strategy_config(self, tmp_path: Path) -> None:
        yaml_content = (
            "id: test_strat_001\n"
            "name: Old Name\n"
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
        yaml_file = tmp_path / "test_strat.yaml"
        yaml_file.write_text(yaml_content)

        with patch("hydra.dashboard.routes.strategies._CONFIG_DIR", tmp_path):
            client = TestClient(_make_app(pool=None))
            resp = client.put(
                "/api/strategies/test_strat_001",
                json={
                    "name": "Updated Name",
                    "description": "Updated description",
                    "exchange_id": "binance",
                    "symbol": "ETHUSDT",
                    "rules": {
                        "entry_long": {
                            "operator": "AND",
                            "conditions": [
                                {
                                    "indicator": "rsi",
                                    "params": {"period": 14},
                                    "comparator": "less_than",
                                    "value": 25,
                                }
                            ],
                        },
                        "exit_long": None,
                        "entry_short": None,
                        "exit_short": None,
                    },
                    "timeframes": {"primary": "4h"},
                    "risk": {
                        "stop_loss_method": "atr",
                        "stop_loss_value": 2.0,
                        "take_profit_method": "atr",
                        "take_profit_value": 3.0,
                        "sizing_method": "fixed_fractional",
                        "sizing_params": {"risk_per_trade_pct": 1.0, "max_position_pct": 10.0},
                    },
                    "enable_immediately": False,
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "test_strat_001"
        assert data["name"] == "Updated Name"
        assert "config_path" in data
        # Verify YAML was written to disk
        written = yaml.safe_load(yaml_file.read_text())
        assert written["id"] == "test_strat_001"
        assert written["name"] == "Updated Name"
        assert written["symbols"] == ["ETHUSDT"]
        assert written["timeframes"]["primary"] == "4h"

    def test_update_unknown_strategy_returns_404(self, tmp_path: Path) -> None:
        with patch("hydra.dashboard.routes.strategies._CONFIG_DIR", tmp_path):
            client = TestClient(_make_app(pool=None))
            resp = client.put(
                "/api/strategies/nonexistent",
                json={
                    "name": "Anything",
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
                        "sizing_params": {"risk_per_trade_pct": 1.0, "max_position_pct": 10.0},
                    },
                    "enable_immediately": False,
                },
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

    def test_get_strategy_returns_structured_data(self, tmp_path: Path) -> None:
        yaml_content = (
            "id: rule_strat_1\n"
            "name: RSI Strategy\n"
            "strategy_class: hydra.strategy.builtin.rule_based.RuleBasedStrategy\n"
            "enabled: true\n"
            "symbols:\n"
            "  - ETHUSDT\n"
            "exchange:\n"
            "  exchange_id: bybit\n"
            "  market_type: SPOT\n"
            "timeframes:\n"
            "  primary: 4h\n"
            "  confirmation: 1d\n"
            "parameters:\n"
            "  description: My RSI strategy\n"
            "  required_history: 50\n"
            "  rules:\n"
            "    entry_long:\n"
            "      operator: AND\n"
            "      conditions:\n"
            "        - indicator: rsi\n"
            "          params: {period: 14}\n"
            "          comparator: less_than\n"
            "          value: 30\n"
            "    exit_long: null\n"
            "    entry_short: null\n"
            "    exit_short: null\n"
            "position_sizing:\n"
            "  method: fixed_fractional\n"
            "  risk_per_trade_pct: 2.0\n"
            "  max_position_pct: 15.0\n"
        )
        (tmp_path / "rule_strat.yaml").write_text(yaml_content)

        with patch("hydra.dashboard.routes.strategies._CONFIG_DIR", tmp_path):
            client = TestClient(_make_app(pool=None))
            resp = client.get("/api/strategies/rule_strat_1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "rule_strat_1"
        assert data["name"] == "RSI Strategy"
        assert data["description"] == "My RSI strategy"
        assert data["exchange_id"] == "bybit"
        assert data["symbol"] == "ETHUSDT"
        assert data["editable"] is True
        # Check rules
        assert data["rules"]["entry_long"]["operator"] == "AND"
        assert len(data["rules"]["entry_long"]["conditions"]) == 1
        assert data["rules"]["entry_long"]["conditions"][0]["indicator"] == "rsi"
        # Check timeframes
        assert data["timeframes"]["primary"] == "4h"
        assert data["timeframes"]["confirmation"] == "1d"
        # Check risk
        assert data["risk"]["sizing_method"] == "fixed_fractional"
        assert data["risk"]["sizing_params"]["risk_per_trade_pct"] == 2.0
        assert data["risk"]["sizing_params"]["max_position_pct"] == 15.0

    def test_update_non_rule_based_returns_400(self, tmp_path: Path) -> None:
        yaml_content = (
            "id: lstm_strat\n"
            "name: LSTM Strategy\n"
            "strategy_class: hydra.ml.lstm.LSTMStrategy\n"
            "enabled: true\n"
        )
        (tmp_path / "lstm.yaml").write_text(yaml_content)

        with patch("hydra.dashboard.routes.strategies._CONFIG_DIR", tmp_path):
            client = TestClient(_make_app(pool=None))
            resp = client.put(
                "/api/strategies/lstm_strat",
                json={
                    "name": "Updated",
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
                        "sizing_params": {"risk_per_trade_pct": 1.0, "max_position_pct": 10.0},
                    },
                    "enable_immediately": False,
                },
            )
        assert resp.status_code == 400

    def test_update_preserves_strategy_id(self, tmp_path: Path) -> None:
        yaml_content = (
            "id: original_id_123\n"
            "name: Original Name\n"
            "strategy_class: hydra.strategy.builtin.rule_based.RuleBasedStrategy\n"
            "enabled: true\n"
            "symbols:\n"
            "  - BTCUSDT\n"
            "exchange:\n"
            "  exchange_id: binance\n"
            "  market_type: SPOT\n"
            "timeframes:\n"
            "  primary: 1h\n"
            "parameters:\n"
            "  required_history: 75\n"
            "  rules:\n"
            "    entry_long:\n"
            "      operator: AND\n"
            "      conditions:\n"
            "        - indicator: rsi\n"
            "          params: {period: 14}\n"
            "          comparator: less_than\n"
            "          value: 30\n"
        )
        yaml_file = tmp_path / "original.yaml"
        yaml_file.write_text(yaml_content)

        with patch("hydra.dashboard.routes.strategies._CONFIG_DIR", tmp_path):
            client = TestClient(_make_app(pool=None))
            resp = client.put(
                "/api/strategies/original_id_123",
                json={
                    "name": "Completely Different Name",
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
                        "sizing_params": {"risk_per_trade_pct": 1.0, "max_position_pct": 10.0},
                    },
                    "enable_immediately": False,
                },
            )
        assert resp.status_code == 200
        assert resp.json()["id"] == "original_id_123"
        # Read back YAML and verify ID preserved
        written = yaml.safe_load(yaml_file.read_text())
        assert written["id"] == "original_id_123"
        assert written["name"] == "Completely Different Name"
        # Verify required_history preserved from original
        assert written["parameters"]["required_history"] == 75


# ---------------------------------------------------------------------------
# DB path (mock pool)
# ---------------------------------------------------------------------------


class TestStrategiesWithPool:
    def test_list_strategies_from_db(self, tmp_path: Path) -> None:
        # Create a YAML strategy file as source of truth
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(
            "id: db-1\n"
            "name: LSTM Momentum\n"
            "strategy_class: hydra.strategy.builtin.rule_based.RuleBasedStrategy\n"
            "enabled: true\n"
        )

        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "strategy_id": "db-1",
                    "total_pnl": 500.0,
                    "total_trades": 10,
                    "win_rate": 60.0,
                },
            ]
        )

        with patch("hydra.dashboard.routes.strategies._CONFIG_DIR", tmp_path):
            client = TestClient(_make_app(pool=pool))
            resp = client.get("/api/strategies")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == "db-1"
        assert data[0]["name"] == "LSTM Momentum"
        assert data[0]["editable"] is True
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
        assert len(data) >= 1

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
