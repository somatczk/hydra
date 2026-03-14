"""Tests for portfolio dashboard routes with DB and fallback paths.

Tests cover:
- Each endpoint returning correct format when pool is None (fallback path)
- Each endpoint returning correct format with a mock pool (DB path)
- ``/api/portfolio/trades`` returning TradeRecord list
- ``_format_pair`` symbol formatting helper
- Aggregation logic for daily PnL, monthly returns, attribution
- DB exception fallback behaviour
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hydra.dashboard.routes.portfolio import (
    _format_pair,
    router,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_app(pool: object | None = None) -> FastAPI:
    """Create a minimal FastAPI app with the portfolio router and optional pool."""
    test_app = FastAPI()
    test_app.include_router(router)
    test_app.state.db_pool = pool
    return test_app


@pytest.fixture()
def client_no_pool() -> TestClient:
    """Client with no DB pool -- exercises the fallback path."""
    return TestClient(_make_app(pool=None))


def _make_mock_pool() -> MagicMock:
    """Build a mock asyncpg pool with ``acquire`` returning an async context manager."""
    pool = MagicMock()
    conn = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx
    return pool, conn


# ---------------------------------------------------------------------------
# _format_pair helper
# ---------------------------------------------------------------------------


class TestFormatPair:
    def test_btcusdt(self) -> None:
        assert _format_pair("BTCUSDT") == "BTC/USDT"

    def test_ethusdt(self) -> None:
        assert _format_pair("ETHUSDT") == "ETH/USDT"

    def test_solusdt(self) -> None:
        assert _format_pair("SOLUSDT") == "SOL/USDT"

    def test_dogeusdt(self) -> None:
        assert _format_pair("DOGEUSDT") == "DOGE/USDT"

    def test_avaxusdt(self) -> None:
        assert _format_pair("AVAXUSDT") == "AVAX/USDT"

    def test_linkbtc(self) -> None:
        assert _format_pair("LINKBTC") == "LINK/BTC"

    def test_unknown_symbol_short(self) -> None:
        """Symbols shorter than 4 chars are returned as-is."""
        assert _format_pair("BTC") == "BTC"

    def test_unknown_symbol_fallback(self) -> None:
        """Unknown base currencies use the 3-char split fallback."""
        assert _format_pair("AABCDE") == "AAB/CDE"

    def test_empty_string(self) -> None:
        assert _format_pair("") == ""


# ---------------------------------------------------------------------------
# Fallback path (pool is None)
# ---------------------------------------------------------------------------


class TestPortfolioFallback:
    """All endpoints should return valid placeholder data when pool is None."""

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

    def test_positions_returns_list(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.get("/api/portfolio/positions")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0
        pos = data[0]
        assert "id" in pos
        assert "pair" in pos
        assert "side" in pos

    def test_equity_curve_returns_points(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.get("/api/portfolio/equity-curve")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0
        assert "timestamp" in data[0]
        assert "value" in data[0]

    def test_daily_pnl_returns_list(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.get("/api/portfolio/daily-pnl")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0
        assert "date" in data[0]
        assert "pnl" in data[0]

    def test_monthly_returns_list(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.get("/api/portfolio/monthly-returns")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0
        assert "month" in data[0]
        assert "return_pct" in data[0]

    def test_attribution_returns_list(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.get("/api/portfolio/attribution")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0
        assert "strategy" in data[0]
        assert "pnl" in data[0]
        assert "pct_of_total" in data[0]

    def test_trades_returns_list(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.get("/api/portfolio/trades")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0
        trade = data[0]
        assert "id" in trade
        assert "pair" in trade
        assert "side" in trade
        assert "price" in trade
        assert "size" in trade
        assert "fee" in trade
        assert "pnl" in trade
        assert "timestamp" in trade


# ---------------------------------------------------------------------------
# DB path (mock pool)
# ---------------------------------------------------------------------------


class TestPortfolioWithPool:
    """Endpoints should query the DB and return transformed rows."""

    def test_summary_from_db(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetchrow = AsyncMock(
            return_value={
                "total_value": 50000.0,
                "unrealized_pnl": 123.45,
                "realized_pnl": 678.90,
            }
        )
        conn.fetchval = AsyncMock(return_value=42.50)

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/portfolio/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_value"] == 50000.0
        assert data["unrealized_pnl"] == 123.45
        assert data["realized_pnl"] == 678.9
        assert data["total_fees"] == 42.5
        # change_pct = unrealized_pnl / total_value * 100
        expected_pct = round(123.45 / 50000.0 * 100, 2)
        assert data["change_pct"] == expected_pct

    def test_summary_db_returns_none_snapshot(self) -> None:
        """When the snapshot query returns None, fallback summary is returned."""
        pool, conn = _make_mock_pool()
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetchval = AsyncMock(return_value=0)

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/portfolio/summary")
        assert resp.status_code == 200
        data = resp.json()
        # Should return the default PortfolioSummary values
        assert "total_value" in data

    def test_summary_db_exception_falls_back(self) -> None:
        """DB errors should return default placeholder summary."""
        pool, conn = _make_mock_pool()
        conn.fetchrow = AsyncMock(side_effect=RuntimeError("DB down"))

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/portfolio/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_value" in data

    def test_positions_from_db(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "strategy_id": "s1",
                    "exchange_id": "binance",
                    "symbol": "BTCUSDT",
                    "direction": "LONG",
                    "quantity": 0.5,
                    "avg_entry_price": 60000.0,
                    "unrealized_pnl": 500.0,
                    "realized_pnl": 100.0,
                },
                {
                    "id": 2,
                    "strategy_id": "s2",
                    "exchange_id": "bybit",
                    "symbol": "ETHUSDT",
                    "direction": "SHORT",
                    "quantity": 10.0,
                    "avg_entry_price": 3000.0,
                    "unrealized_pnl": -50.0,
                    "realized_pnl": 0.0,
                },
            ]
        )

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/portfolio/positions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

        btc = data[0]
        assert btc["id"] == "pos-1"
        assert btc["pair"] == "BTC/USDT"
        assert btc["exchange"] == "binance"
        assert btc["side"] == "Long"
        assert btc["size"] == 0.5
        assert btc["entry_price"] == 60000.0
        # current_price == entry_price (no live feed)
        assert btc["current_price"] == 60000.0
        assert btc["unrealized_pnl"] == 500.0
        # pnl_pct = 500 / (60000 * 0.5) * 100 = 1.67
        assert btc["pnl_pct"] == round(500.0 / (60000.0 * 0.5) * 100, 2)

        eth = data[1]
        assert eth["side"] == "Short"
        assert eth["pair"] == "ETH/USDT"

    def test_positions_db_exception_falls_back(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(side_effect=RuntimeError("DB down"))

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/portfolio/positions")
        assert resp.status_code == 200
        data = resp.json()
        # Falls back to _POSITIONS placeholder
        assert isinstance(data, list)
        assert len(data) > 0

    def test_equity_curve_from_db(self) -> None:
        pool, conn = _make_mock_pool()
        ts1 = datetime(2026, 3, 1, tzinfo=UTC)
        ts2 = datetime(2026, 3, 2, tzinfo=UTC)
        conn.fetch = AsyncMock(
            return_value=[
                {"timestamp": ts1, "total_value": 10000.0},
                {"timestamp": ts2, "total_value": 10500.0},
            ]
        )

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/portfolio/equity-curve")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["timestamp"] == ts1.isoformat()
        assert data[0]["value"] == 10000.0
        assert data[1]["value"] == 10500.0

    def test_equity_curve_db_exception_falls_back(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(side_effect=RuntimeError("DB down"))

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/portfolio/equity-curve")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0

    def test_daily_pnl_from_db(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(
            return_value=[
                {"date": datetime(2026, 3, 8, tzinfo=UTC), "pnl": 120.555},
                {"date": datetime(2026, 3, 9, tzinfo=UTC), "pnl": -45.0},
            ]
        )

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/portfolio/daily-pnl")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["date"] == "2026-03-08"
        assert data[0]["pnl"] == 120.56  # rounded to 2 decimals
        assert data[1]["pnl"] == -45.0

    def test_daily_pnl_db_exception_falls_back(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(side_effect=RuntimeError("DB down"))

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/portfolio/daily-pnl")
        assert resp.status_code == 200

    def test_monthly_returns_from_db(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(
            return_value=[
                {"month": "2026-01", "first_val": 10000.0, "last_val": 10500.0},
                {"month": "2026-02", "first_val": 10500.0, "last_val": 11000.0},
            ]
        )

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/portfolio/monthly-returns")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        # return_pct = (last_val - first_val) / first_val * 100
        assert data[0]["month"] == "2026-01"
        assert data[0]["return_pct"] == round((10500 - 10000) / 10000 * 100, 2)
        assert data[1]["return_pct"] == round((11000 - 10500) / 10500 * 100, 2)

    def test_monthly_returns_zero_first_val(self) -> None:
        """When first_val is 0, return_pct should be 0.0 (no division by zero)."""
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(
            return_value=[
                {"month": "2026-01", "first_val": 0.0, "last_val": 100.0},
            ]
        )

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/portfolio/monthly-returns")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["return_pct"] == 0.0

    def test_monthly_returns_db_exception_falls_back(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(side_effect=RuntimeError("DB down"))

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/portfolio/monthly-returns")
        assert resp.status_code == 200

    def test_attribution_from_db(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(
            return_value=[
                {"name": "StratA", "pnl": 600.0},
                {"name": "StratB", "pnl": 400.0},
            ]
        )

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/portfolio/attribution")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        total = 600.0 + 400.0
        assert data[0]["strategy"] == "StratA"
        assert data[0]["pnl"] == 600.0
        assert data[0]["pct_of_total"] == round(600.0 / total * 100, 1)
        assert data[1]["pct_of_total"] == round(400.0 / total * 100, 1)

    def test_attribution_zero_total_pnl(self) -> None:
        """When total PnL is zero, pct_of_total should be 0.0."""
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(
            return_value=[
                {"name": "StratA", "pnl": 0.0},
            ]
        )

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/portfolio/attribution")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["pct_of_total"] == 0.0

    def test_attribution_db_exception_falls_back(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(side_effect=RuntimeError("DB down"))

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/portfolio/attribution")
        assert resp.status_code == 200

    def test_trades_from_db(self) -> None:
        pool, conn = _make_mock_pool()
        ts = datetime(2026, 3, 14, 12, 0, 0, tzinfo=UTC)
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "id": 42,
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "price": 67000.0,
                    "quantity": 0.1,
                    "fee": 0.00012345,
                    "pnl": 50.678,
                    "timestamp": ts,
                },
            ]
        )

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/portfolio/trades")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        trade = data[0]
        assert trade["id"] == 42
        assert trade["pair"] == "BTC/USDT"
        assert trade["side"] == "BUY"
        assert trade["price"] == 67000.0
        assert trade["size"] == 0.1
        assert trade["fee"] == 0.00012345
        assert trade["pnl"] == 50.68
        assert trade["timestamp"] == ts.isoformat()

    def test_trades_db_exception_falls_back(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(side_effect=RuntimeError("DB down"))

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/portfolio/trades")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_positions_zero_entry_price_no_division_error(self) -> None:
        """When entry_price * quantity is zero, pnl_pct should be 0.0."""
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "id": 99,
                    "strategy_id": "s1",
                    "exchange_id": "binance",
                    "symbol": "BTCUSDT",
                    "direction": "LONG",
                    "quantity": 0.0,
                    "avg_entry_price": 0.0,
                    "unrealized_pnl": 0.0,
                    "realized_pnl": 0.0,
                },
            ]
        )

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/portfolio/positions")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["pnl_pct"] == 0.0

    def test_summary_zero_total_value_no_division_error(self) -> None:
        """When total_value is zero, change_pct should be 0.0."""
        pool, conn = _make_mock_pool()
        conn.fetchrow = AsyncMock(
            return_value={
                "total_value": 0.0,
                "unrealized_pnl": 0.0,
                "realized_pnl": 0.0,
            }
        )
        conn.fetchval = AsyncMock(return_value=0.0)

        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/portfolio/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["change_pct"] == 0.0
