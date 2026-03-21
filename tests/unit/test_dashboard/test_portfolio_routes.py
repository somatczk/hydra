"""Tests for portfolio API routes."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hydra.dashboard.routes.portfolio import router


def _make_app(pool: Any = None) -> FastAPI:
    app = FastAPI()
    app.state.db_pool = pool
    app.include_router(router)
    return app


def _make_mock_pool() -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    pool._closed = False
    return pool, conn


@pytest.fixture()
def client_no_pool() -> TestClient:
    return TestClient(_make_app(pool=None))


# ---------------------------------------------------------------------------
# Fallback (no pool)
# ---------------------------------------------------------------------------


class TestPortfolioFallback:
    def test_summary_returns_defaults(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.get("/api/portfolio/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_value" in data
        assert "unrealized_pnl" in data
        assert "realized_pnl" in data
        assert "total_fees" in data
        assert "change_pct" in data
        assert isinstance(data["total_value"], float)

    def test_positions_returns_empty_list(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.get("/api/portfolio/positions")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 0

    def test_equity_curve_returns_empty(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.get("/api/portfolio/equity-curve")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 0

    def test_daily_pnl_returns_empty(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.get("/api/portfolio/daily-pnl")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 0

    def test_monthly_returns_empty(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.get("/api/portfolio/monthly-returns")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 0

    def test_attribution_returns_empty(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.get("/api/portfolio/attribution")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 0

    def test_trades_returns_empty(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.get("/api/portfolio/trades")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 0


# ---------------------------------------------------------------------------
# DB path (mock pool)
# ---------------------------------------------------------------------------


class TestPortfolioWithPool:
    """Endpoints should query the DB and return transformed rows."""

    def test_summary_from_db(self) -> None:
        pool, conn = _make_mock_pool()
        # No source param: fetchval called 4 times:
        # realized_pnl, fees, daily_pnl, unrealized_pnl
        # Then fetchrow for snapshot (total_value), then fetch for equity curve
        conn.fetchval = AsyncMock(side_effect=[1500.00, 85.50, 42.00, 200.00])
        conn.fetchrow = AsyncMock(
            return_value={"total_value": 12000.50}
        )
        conn.fetch = AsyncMock(
            return_value=[
                {"total_value": 10000.00},
                {"total_value": 12000.50},
            ]
        )
        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/portfolio/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_value"] == 12000.50
        assert data["total_fees"] == 85.50
        assert data["daily_pnl"] == 42.00
        assert data["max_drawdown_pct"] == 0.0  # monotonically increasing curve

    def test_positions_from_db(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "strategy_id": "strat-1",
                    "exchange_id": "binance",
                    "symbol": "BTCUSDT",
                    "direction": "LONG",
                    "quantity": 0.15,
                    "avg_entry_price": 67420.0,
                    "unrealized_pnl": 100.0,
                    "realized_pnl": 0.0,
                }
            ]
        )
        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/portfolio/positions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["pair"] == "BTC/USDT"

    def test_positions_db_exception_returns_empty(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(side_effect=RuntimeError("DB down"))
        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/portfolio/positions")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 0

    def test_equity_curve_db_exception_returns_empty(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(side_effect=RuntimeError("DB down"))
        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/portfolio/equity-curve")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 0
