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

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from hydra.dashboard.routes.strategies import router
from hydra.services.strategies import _row_to_strategy, _status_from_enabled

_INVALID_INDICATOR_RULES: dict[str, Any] = {
    "entry_long": {
        "operator": "AND",
        "conditions": [
            {
                "indicator": "rsii",
                "params": {"period": 14},
                "comparator": "less_than",
                "value": 30,
            }
        ],
    },
    "exit_long": None,
    "entry_short": None,
    "exit_short": None,
}
_INVALID_PARAM_RULES: dict[str, Any] = {
    "entry_long": {
        "operator": "AND",
        "conditions": [
            {
                "indicator": "rsi",
                "params": {"period": "abc"},
                "comparator": "less_than",
                "value": 30,
            }
        ],
    },
    "exit_long": None,
    "entry_short": None,
    "exit_short": None,
}

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
            "name": "Mean Reversion",
            "enabled": True,
            "total_pnl": 1000.25,
            "win_rate": 72.36,
            "total_trades": 50,
        }
        result = _row_to_strategy(row)
        assert result["id"] == "s1"
        assert result["name"] == "Mean Reversion"
        assert "Bollinger" in result["description"]
        assert result["status"] == "Active"
        assert result["enabled"] is True
        assert result["performance"]["total_pnl"] == 1000.25
        assert result["performance"]["win_rate"] == 72.4
        assert result["performance"]["total_trades"] == 50
        assert "sharpe_ratio" not in result["performance"]
        assert "max_drawdown" not in result["performance"]

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
    def test_empty_config_dir_lists_nothing(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.get("/api/strategies")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_non_yaml_strategy_without_db_is_503_not_404(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.get("/api/strategies/strat-1")
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "database_unavailable"
        assert resp.json()["detail"]["state"] == "unavailable"

    def test_unknown_strategy_with_db_is_404(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetchrow = AsyncMock(return_value=None)
        resp = TestClient(_make_app(pool=pool)).get("/api/strategies/strat-1")
        assert resp.status_code == 404
        assert resp.json()["detail"] == {
            "code": "strategy_not_found",
            "message": "Strategy strat-1 not found",
            "state": "not_found",
        }

    def test_toggle_placeholder_is_404(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.post("/api/strategies/strat-1/toggle")
        assert resp.status_code == 404

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

        with patch("hydra.services.strategies._CONFIG_DIR", tmp_path):
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
        with patch("hydra.services.strategies._CONFIG_DIR", tmp_path):
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

    def test_placeholder_performance_is_404(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.get("/api/strategies/strat-1/performance")
        assert resp.status_code == 404

    def test_performance_without_database_is_503(
        self, client_no_pool: TestClient, strategies_dir: Path
    ) -> None:
        (strategies_dir / "s.yaml").write_text("id: s1\nname: S\nstrategy_class: x.Y\n")
        resp = client_no_pool.get("/api/strategies/s1/performance")
        assert resp.status_code == 503
        assert resp.json()["detail"]["state"] == "unavailable"

    def test_list_without_database_marks_performance_unavailable(
        self, client_no_pool: TestClient, strategies_dir: Path
    ) -> None:
        (strategies_dir / "s.yaml").write_text("id: s1\nname: S\nstrategy_class: x.Y\n")
        resp = client_no_pool.get("/api/strategies")
        assert resp.status_code == 200
        (entry,) = resp.json()
        assert entry["id"] == "s1"
        assert entry["performance"] is None
        assert "database is not available" in entry["performance_error"]

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

        with patch("hydra.services.strategies._CONFIG_DIR", tmp_path):
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
        assert data["ml_overlay"] is None
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

    def test_get_strategy_returns_ml_overlay(self, tmp_path: Path) -> None:
        yaml_content = (
            "id: ml_strat_1\n"
            "name: ML Overlay Strategy\n"
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
            "  description: Strategy with ML overlay\n"
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
            "  risk_per_trade_pct: 1.0\n"
            "  max_position_pct: 10.0\n"
            "ml_overlay:\n"
            "  model_name: lstm_btc\n"
            "  confidence_threshold: 0.7\n"
        )
        (tmp_path / "ml_strat.yaml").write_text(yaml_content)

        with patch("hydra.services.strategies._CONFIG_DIR", tmp_path):
            client = TestClient(_make_app(pool=None))
            resp = client.get("/api/strategies/ml_strat_1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "ml_strat_1"
        assert data["ml_overlay"] == {"model_name": "lstm_btc", "confidence_threshold": 0.7}

    def test_save_strategy_with_ml_overlay(self, tmp_path: Path) -> None:
        with patch("hydra.services.strategies._CONFIG_DIR", tmp_path):
            client = TestClient(_make_app(pool=None))
            resp = client.post(
                "/api/strategies/save",
                json={
                    "name": "ML Test Strategy",
                    "description": "Test with ML overlay",
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
                    "ml_overlay": {"model_name": "lstm_btc", "confidence_threshold": 0.7},
                },
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "ML Test Strategy"
        # Verify YAML contains ml_overlay
        yaml_files = list(tmp_path.glob("*.yaml"))
        assert len(yaml_files) == 1
        written = yaml.safe_load(yaml_files[0].read_text())
        assert written["ml_overlay"] == {"model_name": "lstm_btc", "confidence_threshold": 0.7}

    def test_update_non_rule_based_returns_400(self, tmp_path: Path) -> None:
        yaml_content = (
            "id: lstm_strat\n"
            "name: LSTM Strategy\n"
            "strategy_class: hydra.ml.lstm.LSTMStrategy\n"
            "enabled: true\n"
        )
        (tmp_path / "lstm.yaml").write_text(yaml_content)

        with patch("hydra.services.strategies._CONFIG_DIR", tmp_path):
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

        with patch("hydra.services.strategies._CONFIG_DIR", tmp_path):
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

    def test_import_rejects_wrong_extension_without_reading_upload(
        self, client_no_pool: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reads: list[str | None] = []
        real_read = UploadFile.read

        async def spy_read(self: UploadFile, size: int = -1) -> bytes:
            reads.append(self.filename)
            return await real_read(self, size)

        monkeypatch.setattr(UploadFile, "read", spy_read)
        with patch("hydra.services.strategies._CONFIG_DIR", tmp_path):
            rejected = client_no_pool.post(
                "/api/strategies/import", files={"file": ("x.txt", b"a" * 2_000_000)}
            )
            accepted = client_no_pool.post(
                "/api/strategies/import",
                files={
                    "file": (
                        "ok.yaml",
                        yaml.safe_dump(
                            {
                                "name": "Up",
                                "strategy_class": (
                                    "hydra.strategy.builtin.rule_based.RuleBasedStrategy"
                                ),
                            }
                        ).encode(),
                    )
                },
            )
        assert rejected.status_code == 422
        assert rejected.json() == {"detail": "File must be .yaml or .yml"}
        assert accepted.status_code == 201
        assert reads == ["ok.yaml"]
        assert [f.name for f in tmp_path.glob("*.yaml")] == [f"{accepted.json()['id']}.yaml"]

    def test_import_with_disallowed_class_is_422_and_writes_nothing(
        self, client_no_pool: TestClient, tmp_path: Path
    ) -> None:
        upload = yaml.safe_dump({"name": "Bad", "strategy_class": "antigravity.Foo"}).encode()
        with patch("hydra.services.strategies._CONFIG_DIR", tmp_path):
            resp = client_no_pool.post(
                "/api/strategies/import", files={"file": ("bad.yaml", upload)}
            )
        assert resp.status_code == 422
        assert list(tmp_path.glob("*.yaml")) == []
        assert "antigravity" not in sys.modules

    def test_import_without_strategy_class_is_422(
        self, client_no_pool: TestClient, tmp_path: Path
    ) -> None:
        upload = yaml.safe_dump({"name": "No Class"}).encode()
        with patch("hydra.services.strategies._CONFIG_DIR", tmp_path):
            resp = client_no_pool.post(
                "/api/strategies/import", files={"file": ("no_class.yaml", upload)}
            )
        assert resp.status_code == 422
        assert list(tmp_path.glob("*.yaml")) == []

    @pytest.mark.parametrize(
        "rules",
        [_INVALID_INDICATOR_RULES, _INVALID_PARAM_RULES],
        ids=["bad_indicator", "bad_param"],
    )
    def test_save_with_invalid_rules_is_422_and_writes_nothing(
        self, tmp_path: Path, rules: dict[str, Any]
    ) -> None:
        with patch("hydra.services.strategies._CONFIG_DIR", tmp_path):
            client = TestClient(_make_app(pool=None))
            resp = client.post(
                "/api/strategies/save",
                json={
                    "name": "Bad Rules",
                    "description": "",
                    "exchange_id": "binance",
                    "symbol": "BTCUSDT",
                    "rules": rules,
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
        assert resp.status_code == 422
        assert list(tmp_path.glob("*.yaml")) == []

    def test_put_with_a_valid_body_is_200(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "valid.yaml"
        yaml_file.write_text(
            yaml.safe_dump(
                {
                    "id": "valid_1",
                    "name": "Valid",
                    "strategy_class": "hydra.strategy.builtin.rule_based.RuleBasedStrategy",
                    "enabled": False,
                    "symbols": ["BTCUSDT"],
                    "exchange": {"exchange_id": "binance", "market_type": "SPOT"},
                    "timeframes": {"primary": "1h"},
                    "parameters": {
                        "required_history": 50,
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
                    },
                }
            )
        )
        with patch("hydra.services.strategies._CONFIG_DIR", tmp_path):
            client = TestClient(_make_app(pool=None))
            resp = client.put(
                "/api/strategies/valid_1",
                json={
                    "name": "Still Valid",
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
                                    "value": 25,
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

    def test_reload_with_no_running_session_is_404(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.post("/api/strategies/nope/reload")
        assert resp.status_code == 404


class _FakeSession:
    def __init__(self, strategy_id: str, status: str, session_id: str, config_hash: str) -> None:
        self.strategy_id = strategy_id
        self.status = status
        self.session_id = session_id
        self.config_hash = config_hash


class _FakeManager:
    def __init__(self, sessions: list[_FakeSession]) -> None:
        self._sessions = sessions

    def list_sessions(self) -> list[_FakeSession]:
        return self._sessions


class TestGetStrategyRunningSessions:
    def test_running_sessions_and_stale_flag(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "drift.yaml"
        yaml_file.write_text(
            "id: drift_1\nname: Drift\n"
            "strategy_class: hydra.strategy.builtin.rule_based.RuleBasedStrategy\n"
            "enabled: true\n"
            "symbols: [BTCUSDT]\n"
            "parameters:\n  rsi_period: 21\n"
        )
        from hydra.strategy.config import StrategyConfig, config_hash
        from hydra.strategy.engine import validate_strategy_config

        file_hash = config_hash(validate_strategy_config(yaml.safe_load(yaml_file.read_text())))
        stale_hash = config_hash(
            StrategyConfig(
                id="drift_1",
                name="Drift",
                strategy_class="hydra.strategy.builtin.rule_based.RuleBasedStrategy",
                symbols=["BTCUSDT"],
                parameters={"rsi_period": 14},
            )
        )
        manager = _FakeManager(
            [
                _FakeSession("drift_1", "running", "s-stale", stale_hash),
                _FakeSession("drift_1", "running", "s-fresh", file_hash),
                _FakeSession("drift_1", "stopped", "s-stopped", stale_hash),
                _FakeSession("other", "running", "s-other", stale_hash),
            ]
        )

        app = FastAPI()
        app.include_router(router)
        app.state.db_pool = None
        app.state.session_manager = manager
        with patch("hydra.services.strategies._CONFIG_DIR", tmp_path):
            resp = TestClient(app).get("/api/strategies/drift_1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["config_hash"] == file_hash
        by_id = {s["session_id"]: s for s in data["running_sessions"]}
        assert set(by_id) == {"s-stale", "s-fresh"}
        assert by_id["s-stale"] == {
            "session_id": "s-stale",
            "config_hash": stale_hash,
            "stale": True,
        }
        assert by_id["s-fresh"]["stale"] is False
        assert data["file_changed"] is True

    def test_no_session_manager_gives_empty_running_sessions(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "solo.yaml"
        yaml_file.write_text(
            "id: solo_1\nname: Solo\n"
            "strategy_class: hydra.strategy.builtin.rule_based.RuleBasedStrategy\n"
        )
        with patch("hydra.services.strategies._CONFIG_DIR", tmp_path):
            client = TestClient(_make_app(pool=None))
            resp = client.get("/api/strategies/solo_1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running_sessions"] == []
        assert data["file_changed"] is False


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

        with patch("hydra.services.strategies._CONFIG_DIR", tmp_path):
            client = TestClient(_make_app(pool=pool))
            resp = client.get("/api/strategies")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == "db-1"
        assert data[0]["name"] == "LSTM Momentum"
        assert data[0]["editable"] is True
        assert data[0]["performance"]["total_pnl"] == 500.0

    def test_list_strategies_db_error_keeps_yaml_and_nulls_performance(
        self, strategies_dir: Path
    ) -> None:
        (strategies_dir / "s.yaml").write_text("id: s1\nname: S\nstrategy_class: x.Y\n")
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(side_effect=OSError("connection refused"))

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/strategies")
        assert resp.status_code == 200
        (entry,) = resp.json()
        assert (entry["id"], entry["name"]) == ("s1", "S")
        assert entry["performance"] is None
        assert entry["performance_error"] == "Performance unavailable: database error"

    def test_get_strategy_db_error_keeps_builder_config(self, strategies_dir: Path) -> None:
        (strategies_dir / "r.yaml").write_text(
            "id: r1\nname: R\n"
            "strategy_class: hydra.strategy.builtin.rule_based.RuleBasedStrategy\n"
            "timeframes:\n  primary: 4h\n"
        )
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(side_effect=OSError("connection refused"))

        resp = TestClient(_make_app(pool=pool)).get("/api/strategies/r1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["timeframes"]["primary"] == "4h"
        assert data["performance"] is None
        assert data["performance_error"] == "Performance unavailable: database error"

    def test_performance_without_trades_is_zero_with_unknown_ratios(
        self, strategies_dir: Path
    ) -> None:
        (strategies_dir / "s.yaml").write_text("id: s1\nname: S\nstrategy_class: x.Y\n")
        pool, conn = _make_mock_pool()
        conn.fetchrow = AsyncMock(return_value={"total_pnl": 0, "total_trades": 0, "win_rate": 0})
        resp = TestClient(_make_app(pool=pool)).get("/api/strategies/s1/performance")
        assert resp.status_code == 200
        assert resp.json() == {
            "total_pnl": 0.0,
            "win_rate": 0.0,
            "total_trades": 0,
            "sharpe_ratio": None,
            "max_drawdown": None,
        }

    def test_performance_db_error_is_503(self, strategies_dir: Path) -> None:
        (strategies_dir / "s.yaml").write_text("id: s1\nname: S\nstrategy_class: x.Y\n")
        pool, conn = _make_mock_pool()
        conn.fetchrow = AsyncMock(side_effect=OSError("connection refused"))

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/strategies/s1/performance")
        assert resp.status_code == 503

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

    def test_get_strategy_db_error_is_503(self) -> None:
        """A DB error is reported as unavailable, not answered with a placeholder."""
        pool, conn = _make_mock_pool()
        conn.fetchrow = AsyncMock(side_effect=OSError("connection refused"))

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/strategies/strat-1")
        assert resp.status_code == 503
        assert resp.json()["detail"]["state"] == "unavailable"

    def test_toggle_placeholder_with_pool_is_404(self) -> None:
        pool, _conn = _make_mock_pool()

        client = TestClient(_make_app(pool=pool))
        resp = client.post("/api/strategies/strat-1/toggle")
        assert resp.status_code == 404

    def test_toggle_strategy_not_found(self) -> None:
        pool, _conn = _make_mock_pool()

        client = TestClient(_make_app(pool=pool))
        resp = client.post("/api/strategies/nonexistent/toggle")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# #123 tail: backtest share no longer swallows DB errors
# ---------------------------------------------------------------------------


def _share_app(pool: object | None) -> FastAPI:
    from hydra.dashboard.routes.backtest import router as backtest_router

    test_app = FastAPI()
    test_app.include_router(backtest_router)
    test_app.state.db_pool = pool
    return test_app


class TestBacktestShare:
    def test_share_without_db_is_503_and_issues_no_token(self) -> None:
        from hydra.services.backtest import _SHARED_TOKENS

        before = dict(_SHARED_TOKENS)
        resp = TestClient(_share_app(None)).post("/api/backtest/results/bt-x/share")
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "database_unavailable"
        assert before == _SHARED_TOKENS

    def test_share_db_error_is_503_not_a_token(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetchval = AsyncMock(side_effect=OSError("connection refused"))
        resp = TestClient(_share_app(pool)).post("/api/backtest/results/bt-x/share")
        assert resp.status_code == 503
        assert "token" not in resp.json()

    def test_share_unknown_result_is_404_envelope(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetchval = AsyncMock(return_value=None)
        resp = TestClient(_share_app(pool)).post("/api/backtest/results/bt-x/share")
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "result_not_found"

    def test_share_existing_result_returns_a_token(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetchval = AsyncMock(return_value=1)
        resp = TestClient(_share_app(pool)).post("/api/backtest/results/bt-x/share")
        assert resp.status_code == 200
        token = resp.json()["token"]
        assert resp.json()["share_url"] == f"/api/backtest/shared/{token}"
