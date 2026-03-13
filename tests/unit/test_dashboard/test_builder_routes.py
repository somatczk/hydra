"""Tests for the strategy builder FastAPI routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hydra.dashboard.routes.strategy_builder import router


@pytest.fixture()
def app() -> FastAPI:
    """Create a FastAPI app with the builder router."""
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    """Create a test client."""
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /api/builder/indicators
# ---------------------------------------------------------------------------


class TestListIndicators:
    """Tests for the indicators listing endpoint."""

    def test_returns_200(self, client: TestClient) -> None:
        """GET /api/builder/indicators returns 200."""
        response = client.get("/api/builder/indicators")
        assert response.status_code == 200

    def test_returns_list(self, client: TestClient) -> None:
        """Response is a JSON list."""
        response = client.get("/api/builder/indicators")
        data = response.json()
        assert isinstance(data, list)
        assert len(data) > 0

    def test_indicator_structure(self, client: TestClient) -> None:
        """Each indicator has name, category, description, params."""
        response = client.get("/api/builder/indicators")
        data = response.json()
        for indicator in data:
            assert "name" in indicator
            assert "category" in indicator
            assert "description" in indicator
            assert "params" in indicator
            assert isinstance(indicator["params"], list)

    def test_param_structure(self, client: TestClient) -> None:
        """Each param has name and type fields."""
        response = client.get("/api/builder/indicators")
        data = response.json()
        for indicator in data:
            for param in indicator["params"]:
                assert "name" in param
                assert "type" in param

    def test_rsi_in_list(self, client: TestClient) -> None:
        """RSI should be in the indicator list."""
        response = client.get("/api/builder/indicators")
        data = response.json()
        names = [ind["name"] for ind in data]
        assert "rsi" in names

    def test_macd_in_list(self, client: TestClient) -> None:
        """MACD should be in the indicator list."""
        response = client.get("/api/builder/indicators")
        data = response.json()
        names = [ind["name"] for ind in data]
        assert "macd" in names


# ---------------------------------------------------------------------------
# GET /api/builder/comparators
# ---------------------------------------------------------------------------


class TestListComparators:
    """Tests for the comparators listing endpoint."""

    def test_returns_200(self, client: TestClient) -> None:
        """GET /api/builder/comparators returns 200."""
        response = client.get("/api/builder/comparators")
        assert response.status_code == 200

    def test_returns_all_comparators(self, client: TestClient) -> None:
        """Should return all 5 comparator types."""
        response = client.get("/api/builder/comparators")
        data = response.json()
        assert len(data) == 5

    def test_comparator_structure(self, client: TestClient) -> None:
        """Each comparator has value, label, description."""
        response = client.get("/api/builder/comparators")
        data = response.json()
        for comp in data:
            assert "value" in comp
            assert "label" in comp
            assert "description" in comp

    def test_comparator_values(self, client: TestClient) -> None:
        """All expected comparator values are present."""
        response = client.get("/api/builder/comparators")
        data = response.json()
        values = {c["value"] for c in data}
        expected = {"less_than", "greater_than", "crosses_above", "crosses_below", "equals"}
        assert values == expected


# ---------------------------------------------------------------------------
# POST /api/builder/save
# ---------------------------------------------------------------------------


class TestSaveStrategy:
    """Tests for the strategy save endpoint."""

    def _valid_save_request(self) -> dict[str, Any]:
        """Return a valid save request body."""
        return {
            "name": "Test Strategy",
            "description": "A test strategy",
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
                            "value": 30.0,
                        }
                    ],
                },
                "exit_long": {
                    "operator": "AND",
                    "conditions": [
                        {
                            "indicator": "rsi",
                            "params": {"period": 14},
                            "comparator": "greater_than",
                            "value": 70.0,
                        }
                    ],
                },
            },
            "timeframes": {"primary": "1h"},
            "risk": {
                "stop_loss_method": "atr",
                "stop_loss_value": 2.0,
                "take_profit_method": "atr",
                "take_profit_value": 3.0,
                "sizing_method": "fixed_fractional",
                "sizing_params": {"risk_per_trade_pct": 1.0},
            },
        }

    def test_save_returns_201(self, client: TestClient, tmp_path: Path) -> None:
        """POST /api/builder/save with valid data returns 201."""
        with patch("hydra.dashboard.routes.strategy_builder._CONFIG_DIR", tmp_path):
            response = client.post("/api/builder/save", json=self._valid_save_request())
        assert response.status_code == 201

    def test_save_creates_yaml_file(self, client: TestClient, tmp_path: Path) -> None:
        """Save endpoint creates a YAML file in the config directory."""
        with patch("hydra.dashboard.routes.strategy_builder._CONFIG_DIR", tmp_path):
            response = client.post("/api/builder/save", json=self._valid_save_request())
        assert response.status_code == 201
        yaml_files = list(tmp_path.glob("*.yaml"))
        assert len(yaml_files) == 1

    def test_save_response_structure(self, client: TestClient, tmp_path: Path) -> None:
        """Response has id, name, config_path, message fields."""
        with patch("hydra.dashboard.routes.strategy_builder._CONFIG_DIR", tmp_path):
            response = client.post("/api/builder/save", json=self._valid_save_request())
        data = response.json()
        assert "id" in data
        assert "name" in data
        assert "config_path" in data
        assert "message" in data
        assert data["name"] == "Test Strategy"

    def test_save_invalid_exchange_returns_422(self, client: TestClient, tmp_path: Path) -> None:
        """Invalid exchange_id returns 422."""
        request = self._valid_save_request()
        request["exchange_id"] = "invalid_exchange"
        with patch("hydra.dashboard.routes.strategy_builder._CONFIG_DIR", tmp_path):
            response = client.post("/api/builder/save", json=request)
        assert response.status_code == 422

    def test_save_empty_name_returns_422(self, client: TestClient) -> None:
        """Empty strategy name returns 422."""
        request = self._valid_save_request()
        request["name"] = ""
        response = client.post("/api/builder/save", json=request)
        assert response.status_code == 422

    def test_save_invalid_comparator_returns_422(self, client: TestClient, tmp_path: Path) -> None:
        """Invalid comparator value returns 422."""
        request = self._valid_save_request()
        request["rules"]["entry_long"]["conditions"][0]["comparator"] = "not_a_comparator"
        with patch("hydra.dashboard.routes.strategy_builder._CONFIG_DIR", tmp_path):
            response = client.post("/api/builder/save", json=request)
        assert response.status_code == 422

    def test_save_invalid_timeframe_returns_422(self, client: TestClient, tmp_path: Path) -> None:
        """Invalid timeframe returns 422."""
        request = self._valid_save_request()
        request["timeframes"]["primary"] = "2h"
        with patch("hydra.dashboard.routes.strategy_builder._CONFIG_DIR", tmp_path):
            response = client.post("/api/builder/save", json=request)
        assert response.status_code == 422

    def test_saved_yaml_is_valid(self, client: TestClient, tmp_path: Path) -> None:
        """The saved YAML file should be loadable and contain the strategy config."""
        with patch("hydra.dashboard.routes.strategy_builder._CONFIG_DIR", tmp_path):
            response = client.post("/api/builder/save", json=self._valid_save_request())
        assert response.status_code == 201
        yaml_files = list(tmp_path.glob("*.yaml"))
        assert len(yaml_files) == 1

        import yaml

        with yaml_files[0].open() as f:
            config = yaml.safe_load(f)
        assert config["name"] == "Test Strategy"
        assert config["symbols"] == ["BTCUSDT"]
        assert "parameters" in config
        assert "rules" in config["parameters"]


# ---------------------------------------------------------------------------
# POST /api/builder/preview
# ---------------------------------------------------------------------------


class TestPreviewSignals:
    """Tests for the preview endpoint."""

    def _valid_preview_request(self) -> dict[str, Any]:
        """Return a valid preview request body."""
        return {
            "rules": {
                "entry_long": {
                    "operator": "AND",
                    "conditions": [
                        {
                            "indicator": "rsi",
                            "params": {"period": 14},
                            "comparator": "less_than",
                            "value": 30.0,
                        }
                    ],
                },
                "exit_long": {
                    "operator": "AND",
                    "conditions": [
                        {
                            "indicator": "rsi",
                            "params": {"period": 14},
                            "comparator": "greater_than",
                            "value": 70.0,
                        }
                    ],
                },
            },
            "timeframe": "1h",
            "symbol": "BTCUSDT",
            "bars_count": 100,
        }

    def test_preview_returns_200(self, client: TestClient) -> None:
        """POST /api/builder/preview returns 200 with valid data."""
        response = client.post("/api/builder/preview", json=self._valid_preview_request())
        assert response.status_code == 200

    def test_preview_response_structure(self, client: TestClient) -> None:
        """Response has signals and metrics fields."""
        response = client.post("/api/builder/preview", json=self._valid_preview_request())
        data = response.json()
        assert "signals" in data
        assert "metrics" in data
        assert isinstance(data["signals"], list)
        assert "trades" in data["metrics"]
        assert "win_rate" in data["metrics"]
        assert "pnl" in data["metrics"]

    def test_preview_invalid_timeframe_returns_422(self, client: TestClient) -> None:
        """Invalid timeframe in preview returns 422."""
        request = self._valid_preview_request()
        request["timeframe"] = "2h"
        response = client.post("/api/builder/preview", json=request)
        assert response.status_code == 422

    def test_preview_invalid_comparator_returns_422(self, client: TestClient) -> None:
        """Invalid comparator in preview returns 422."""
        request = self._valid_preview_request()
        request["rules"]["entry_long"]["conditions"][0]["comparator"] = "invalid"
        response = client.post("/api/builder/preview", json=request)
        assert response.status_code == 422
