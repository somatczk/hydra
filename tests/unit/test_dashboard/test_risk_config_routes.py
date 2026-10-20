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
