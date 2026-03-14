"""Tests for risk config API routes: GET/PUT config, per-strategy overrides, validation."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hydra.dashboard.api import app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> TestClient:
    # Ensure session_manager is set (even if None)
    if not hasattr(app.state, "session_manager"):
        app.state.session_manager = None
    return TestClient(app)


# ---------------------------------------------------------------------------
# Risk config endpoints
# ---------------------------------------------------------------------------


class TestGetRiskConfig:
    def test_get_risk_config_returns_200(self, client: TestClient) -> None:
        resp = client.get("/api/risk/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "max_position_pct" in data
        assert "max_risk_per_trade" in data
        assert "max_daily_loss_pct" in data
        assert "max_drawdown_pct" in data
        assert "max_concurrent_positions" in data
        assert "kill_switch_active" in data

    def test_get_risk_config_returns_defaults_without_db(self, client: TestClient) -> None:
        resp = client.get("/api/risk/config")
        data = resp.json()
        # Default values when DB is unavailable
        assert data["max_position_pct"] == 0.10
        assert data["max_drawdown_pct"] == 0.15
        assert data["kill_switch_active"] is False


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
    def test_live_risk_status_returns_200(self, client: TestClient) -> None:
        resp = client.get("/api/risk/live-status")
        assert resp.status_code == 200
        data = resp.json()
        assert "kill_switch_active" in data
        assert "running_sessions" in data


class TestExistingRiskEndpoints:
    """Ensure existing risk endpoints still work after the changes."""

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

    def test_var_estimate(self, client: TestClient) -> None:
        resp = client.get("/api/risk/var")
        assert resp.status_code == 200
        data = resp.json()
        assert "var_95" in data
