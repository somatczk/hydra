"""Tests for the main dashboard FastAPI application module.

Tests cover:
- Health endpoint reflects pool status
- strategy_builder router is included (not legacy builder)
- DB pool initialises when DATABASE_URL is set
- DB pool is None when DATABASE_URL is not set
- All expected route prefixes are registered
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

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
        return {r.path for r in app.routes if hasattr(r, "path")}

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
    @pytest.mark.asyncio
    async def test_init_db_pool_without_database_url(self) -> None:
        """When DATABASE_URL is not set, db_pool should be None."""
        with patch.dict("os.environ", {}, clear=False):
            # Remove DATABASE_URL if present
            import os

            os.environ.pop("DATABASE_URL", None)

            from hydra.dashboard.api import _init_db_pool

            await _init_db_pool()
            assert app.state.db_pool is None

    @pytest.mark.asyncio
    async def test_init_db_pool_with_database_url(self) -> None:
        """When DATABASE_URL is set, asyncpg.create_pool should be called."""
        mock_pool = AsyncMock()
        with (
            patch.dict("os.environ", {"DATABASE_URL": "postgresql://localhost/test"}),
            patch("asyncpg.create_pool", new_callable=AsyncMock, return_value=mock_pool),
        ):
            from hydra.dashboard.api import _init_db_pool

            await _init_db_pool()
            assert app.state.db_pool is mock_pool

        # Clean up
        app.state.db_pool = None

    @pytest.mark.asyncio
    async def test_init_db_pool_exception_sets_none(self) -> None:
        """When asyncpg.create_pool fails, db_pool should be None."""
        with (
            patch.dict("os.environ", {"DATABASE_URL": "postgresql://localhost/test"}),
            patch(
                "asyncpg.create_pool",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Connection refused"),
            ),
        ):
            from hydra.dashboard.api import _init_db_pool

            await _init_db_pool()
            assert app.state.db_pool is None

    @pytest.mark.asyncio
    async def test_close_db_pool_when_pool_exists(self) -> None:
        """Shutdown should close the pool when it exists."""
        mock_pool = AsyncMock()
        app.state.db_pool = mock_pool
        try:
            from hydra.dashboard.api import _close_db_pool

            await _close_db_pool()
            mock_pool.close.assert_called_once()
        finally:
            app.state.db_pool = None

    @pytest.mark.asyncio
    async def test_close_db_pool_when_pool_is_none(self) -> None:
        """Shutdown should be a no-op when pool is None."""
        app.state.db_pool = None
        from hydra.dashboard.api import _close_db_pool

        # Should not raise
        await _close_db_pool()


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
