"""Tests for the strategy builder endpoints (consolidated under /api/strategies)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hydra.dashboard.routes.strategies import router
from tests.fixtures.universe import seeded_universe_pool


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
# GET /api/strategies/indicators
# ---------------------------------------------------------------------------


class TestListIndicators:
    """Tests for the indicators listing endpoint."""

    def test_returns_200(self, client: TestClient) -> None:
        """GET /api/strategies/indicators returns 200."""
        response = client.get("/api/strategies/indicators")
        assert response.status_code == 200

    def test_returns_list(self, client: TestClient) -> None:
        """Response is a JSON list."""
        response = client.get("/api/strategies/indicators")
        data = response.json()
        assert isinstance(data, list)
        assert len(data) > 0

    def test_indicator_structure(self, client: TestClient) -> None:
        """Each indicator has name, category, description, params."""
        response = client.get("/api/strategies/indicators")
        data = response.json()
        for indicator in data:
            assert "name" in indicator
            assert "category" in indicator
            assert "description" in indicator
            assert "params" in indicator
            assert isinstance(indicator["params"], list)

    def test_param_structure(self, client: TestClient) -> None:
        """Each param has name and type fields."""
        response = client.get("/api/strategies/indicators")
        data = response.json()
        for indicator in data:
            for param in indicator["params"]:
                assert "name" in param
                assert "type" in param

    def test_rsi_in_list(self, client: TestClient) -> None:
        """RSI should be in the indicator list."""
        response = client.get("/api/strategies/indicators")
        data = response.json()
        names = [ind["name"] for ind in data]
        assert "rsi" in names

    def test_macd_in_list(self, client: TestClient) -> None:
        """MACD should be in the indicator list."""
        response = client.get("/api/strategies/indicators")
        data = response.json()
        names = [ind["name"] for ind in data]
        assert "macd" in names


# ---------------------------------------------------------------------------
# GET /api/strategies/comparators
# ---------------------------------------------------------------------------


class TestListComparators:
    """Tests for the comparators listing endpoint."""

    def test_returns_200(self, client: TestClient) -> None:
        """GET /api/strategies/comparators returns 200."""
        response = client.get("/api/strategies/comparators")
        assert response.status_code == 200

    def test_returns_all_comparators(self, client: TestClient) -> None:
        """Should return all 5 comparator types."""
        response = client.get("/api/strategies/comparators")
        data = response.json()
        assert len(data) == 5

    def test_comparator_structure(self, client: TestClient) -> None:
        """Each comparator has value, label, description."""
        response = client.get("/api/strategies/comparators")
        data = response.json()
        for comp in data:
            assert "value" in comp
            assert "label" in comp
            assert "description" in comp

    def test_comparator_values(self, client: TestClient) -> None:
        """All expected comparator values are present."""
        response = client.get("/api/strategies/comparators")
        data = response.json()
        values = {c["value"] for c in data}
        expected = {"less_than", "greater_than", "crosses_above", "crosses_below", "equals"}
        assert values == expected


# ---------------------------------------------------------------------------
# POST /api/strategies/save
# ---------------------------------------------------------------------------


class TestSaveStrategy:
    """Tests for the strategy save endpoint."""

    @pytest.fixture()
    def client(self, app: FastAPI) -> TestClient:
        """The builder app with the stored universe row every save now reads."""
        app.state.db_pool = seeded_universe_pool()
        return TestClient(app)

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
        """POST /api/strategies/save with valid data returns 201."""
        with patch("hydra.services.strategies._CONFIG_DIR", tmp_path):
            response = client.post("/api/strategies/save", json=self._valid_save_request())
        assert response.status_code == 201

    def test_save_creates_yaml_file(self, client: TestClient, tmp_path: Path) -> None:
        """Save endpoint creates a YAML file in the config directory."""
        with patch("hydra.services.strategies._CONFIG_DIR", tmp_path):
            response = client.post("/api/strategies/save", json=self._valid_save_request())
        assert response.status_code == 201
        yaml_files = list(tmp_path.glob("*.yaml"))
        assert len(yaml_files) == 1

    def test_save_response_structure(self, client: TestClient, tmp_path: Path) -> None:
        """Response has id, name, config_path, message fields."""
        with patch("hydra.services.strategies._CONFIG_DIR", tmp_path):
            response = client.post("/api/strategies/save", json=self._valid_save_request())
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
        with patch("hydra.services.strategies._CONFIG_DIR", tmp_path):
            response = client.post("/api/strategies/save", json=request)
        assert response.status_code == 422

    def test_save_empty_name_returns_422(self, client: TestClient) -> None:
        """Empty strategy name returns 422."""
        request = self._valid_save_request()
        request["name"] = ""
        response = client.post("/api/strategies/save", json=request)
        assert response.status_code == 422

    def test_save_invalid_comparator_returns_422(self, client: TestClient, tmp_path: Path) -> None:
        """Invalid comparator value returns 422."""
        request = self._valid_save_request()
        request["rules"]["entry_long"]["conditions"][0]["comparator"] = "not_a_comparator"
        with patch("hydra.services.strategies._CONFIG_DIR", tmp_path):
            response = client.post("/api/strategies/save", json=request)
        assert response.status_code == 422

    def test_save_invalid_timeframe_returns_422(self, client: TestClient, tmp_path: Path) -> None:
        """Invalid timeframe returns 422."""
        request = self._valid_save_request()
        request["timeframes"]["primary"] = "2h"
        with patch("hydra.services.strategies._CONFIG_DIR", tmp_path):
            response = client.post("/api/strategies/save", json=request)
        assert response.status_code == 422

    def test_saved_yaml_is_valid(self, client: TestClient, tmp_path: Path) -> None:
        """The saved YAML file should be loadable and contain the strategy config."""
        with patch("hydra.services.strategies._CONFIG_DIR", tmp_path):
            response = client.post("/api/strategies/save", json=self._valid_save_request())
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
# GET /api/strategies/{id} — the builder read the dialog round-trips through
# ---------------------------------------------------------------------------


class TestGetStrategyDetailPerps:
    """P4-F: the stored perp leverage passes through the detail read, so an edited
    binanceusdm strategy never silently downgrades to 1x."""

    @pytest.fixture()
    def client(self, app: FastAPI) -> TestClient:
        """The builder app with the stored universe row every save reads."""
        app.state.db_pool = seeded_universe_pool()
        return TestClient(app)

    def test_detail_passes_the_saved_perps_leverage_through(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        with patch("hydra.services.strategies._CONFIG_DIR", tmp_path):
            request = TestSaveStrategy()._valid_save_request()
            request["exchange_id"] = "binanceusdm"
            request["leverage"] = 2
            saved = client.post("/api/strategies/save", json=request).json()
            assert saved["id"]

            detail = client.get(f"/api/strategies/{saved['id']}").json()
        assert detail["exchange_id"] == "binanceusdm"
        assert detail["leverage"] == 2

    def test_detail_keeps_a_spot_strategy_at_1x(self, client: TestClient, tmp_path: Path) -> None:
        with patch("hydra.services.strategies._CONFIG_DIR", tmp_path):
            request = TestSaveStrategy()._valid_save_request()
            saved = client.post("/api/strategies/save", json=request).json()

            detail = client.get(f"/api/strategies/{saved['id']}").json()
        assert detail["exchange_id"] == "binance"
        assert detail["leverage"] == 1


# ---------------------------------------------------------------------------
# POST /api/strategies/preview
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

    def test_preview_without_database_is_503_not_sample_data(self, client: TestClient) -> None:
        """Preview needs stored bars; with no DB it says so instead of using a random walk."""
        response = client.post("/api/strategies/preview", json=self._valid_preview_request())
        assert response.status_code == 503
        assert response.json()["detail"]["state"] == "unavailable"

    @pytest.mark.parametrize("timeframe", ["2h", "30m"])
    def test_preview_invalid_timeframe_returns_422(
        self, client: TestClient, timeframe: str
    ) -> None:
        """Invalid timeframe in preview returns 422."""
        request = self._valid_preview_request()
        request["timeframe"] = timeframe
        response = client.post("/api/strategies/preview", json=request)
        assert response.status_code == 422

    def test_preview_unknown_exchange_returns_422(self, client: TestClient) -> None:
        request = self._valid_preview_request()
        request["exchange_id"] = "mtgox"
        response = client.post("/api/strategies/preview", json=request)
        assert response.status_code == 422

    def test_preview_on_a_venue_backtests_refuse_is_422(self, client: TestClient) -> None:
        request = self._valid_preview_request()
        request["exchange_id"] = "kraken"
        response = client.post("/api/strategies/preview", json=request)
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "unsupported_venue"

    def test_preview_invalid_comparator_returns_422(self, client: TestClient) -> None:
        """Invalid comparator in preview returns 422."""
        request = self._valid_preview_request()
        request["rules"]["entry_long"]["conditions"][0]["comparator"] = "invalid"
        response = client.post("/api/strategies/preview", json=request)
        assert response.status_code == 422
