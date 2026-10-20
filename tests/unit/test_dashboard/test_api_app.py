"""Tests for the main dashboard FastAPI application module.

Tests cover:
- Health endpoint reflects pool status
- strategy_builder router is included (not legacy builder)
- Lifespan without DATABASE_URL runs with no pool (startup failures: test_lifespan.py)
- All expected route prefixes are registered
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hydra.core.config import HydraConfig
from hydra.dashboard import lifespan
from hydra.dashboard.api import app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    def test_health_returns_200(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "db" in data

    def test_health_degraded_without_pool(self, client: TestClient) -> None:
        """Without a DB pool the health endpoint should report degraded."""
        # Ensure no pool is set
        original = getattr(app.state, "db_pool", None)
        app.state.db_pool = None
        try:
            resp = client.get("/health")
            data = resp.json()
            assert data["status"] == "degraded"
            assert data["db"] == "disconnected"
        finally:
            app.state.db_pool = original

    def test_health_ok_with_open_pool(self, client: TestClient) -> None:
        """With an open pool, the health endpoint should report ok."""
        mock_pool = MagicMock()
        mock_pool._closed = False
        original = getattr(app.state, "db_pool", None)
        app.state.db_pool = mock_pool
        try:
            resp = client.get("/health")
            data = resp.json()
            assert data["status"] == "ok"
            assert data["db"] == "connected"
        finally:
            app.state.db_pool = original

    def test_health_degraded_with_closed_pool(self, client: TestClient) -> None:
        """With a closed pool, the health endpoint should report degraded."""
        mock_pool = MagicMock()
        mock_pool._closed = True
        original = getattr(app.state, "db_pool", None)
        app.state.db_pool = mock_pool
        try:
            resp = client.get("/health")
            data = resp.json()
            assert data["status"] == "degraded"
            assert data["db"] == "disconnected"
        finally:
            app.state.db_pool = original


# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------


class TestRouterRegistration:
    """Verify expected route prefixes are registered in the app."""

    def _route_paths(self) -> set[str]:
        # HTTP routes from the OpenAPI schema (FastAPI 0.146+ keeps included routers nested
        # in app.routes), websockets are registered on the app itself
        return set(app.openapi()["paths"]) | {r.path for r in app.routes if hasattr(r, "path")}

    def test_strategies_routes_present(self) -> None:
        paths = self._route_paths()
        assert any("/api/strategies" in p for p in paths)
        # Builder endpoints are now consolidated under /api/strategies/
        assert any("/api/strategies/indicators" in p for p in paths)
        assert any("/api/strategies/preview" in p for p in paths)
        assert any("/api/strategies/save" in p for p in paths)

    def test_portfolio_routes_present(self) -> None:
        paths = self._route_paths()
        assert any("/api/portfolio" in p for p in paths)

    def test_backtest_routes_present(self) -> None:
        paths = self._route_paths()
        assert any("/api/backtest" in p for p in paths)

    def test_risk_routes_present(self) -> None:
        paths = self._route_paths()
        assert any("/api/risk" in p for p in paths)

    def test_models_routes_present(self) -> None:
        paths = self._route_paths()
        assert any("/api/models" in p for p in paths)

    def test_system_routes_present(self) -> None:
        paths = self._route_paths()
        assert any("/api/system" in p for p in paths)

    def test_health_route_present(self) -> None:
        paths = self._route_paths()
        assert "/health" in paths

    def test_metrics_route_present(self) -> None:
        paths = self._route_paths()
        assert "/metrics" in paths

    def test_websocket_routes_present(self) -> None:
        paths = self._route_paths()
        ws_paths = {"/ws/market", "/ws/trades", "/ws/portfolio", "/ws/signals", "/ws/risk"}
        for ws_path in ws_paths:
            assert ws_path in paths


# ---------------------------------------------------------------------------
# DB pool startup/shutdown
# ---------------------------------------------------------------------------


class TestDbPoolLifecycle:
    async def test_lifespan_without_database_url_has_no_pool(self) -> None:
        """Dev mode: no DATABASE_URL means no pool, no key check, config still loaded."""
        test_app = FastAPI()
        async with lifespan.run(test_app):
            assert test_app.state.db_pool is None
            assert isinstance(test_app.state.config, HydraConfig)
            assert test_app.state.session_manager is not None
            assert test_app.state.exchange_credentials == {}

    async def test_app_starts_with_pool_unset(self) -> None:
        assert app.state.db_pool is None
        assert app.state.exchange_credential_errors == {}

    async def test_close_db_pool_when_pool_exists(self) -> None:
        mock_pool = AsyncMock()
        test_app = FastAPI()
        test_app.state.db_pool = mock_pool
        await lifespan._close_db_pool(test_app)
        mock_pool.close.assert_awaited_once()

    async def test_close_db_pool_when_pool_is_none(self) -> None:
        test_app = FastAPI()
        test_app.state.db_pool = None
        await lifespan._close_db_pool(test_app)


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


class TestCORS:
    def test_cors_allows_localhost_3000(self, client: TestClient) -> None:
        resp = client.options(
            "/health",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"
