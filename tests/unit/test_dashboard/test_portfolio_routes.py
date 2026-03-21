"""Tests for portfolio API routes."""

from __future__ import annotations

from datetime import UTC
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
        assert isinstance(data, dict)
        assert data["trades"] == []
        assert data["total"] == 0

    def test_update_trade_no_pool_returns_503(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.patch("/api/portfolio/trades/1", json={"notes": "hello"})
        assert resp.status_code == 503

    def test_export_csv_no_pool_returns_503(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.get("/api/portfolio/export/csv")
        assert resp.status_code == 503

    def test_export_csv_invalid_format_returns_422(self, client_no_pool: TestClient) -> None:
        # Invalid format is caught before the pool check
        resp = client_no_pool.get("/api/portfolio/export/csv?format=invalid")
        assert resp.status_code == 422


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
        conn.fetchrow = AsyncMock(return_value={"total_value": 12000.50})
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


# ---------------------------------------------------------------------------
# Trade journal (Feature 1)
# ---------------------------------------------------------------------------


def _make_trade_row(
    trade_id: int = 1,
    symbol: str = "BTCUSDT",
    side: str = "BUY",
    price: float = 67000.0,
    quantity: float = 0.1,
    fee: float = 0.001,
    pnl: float = 150.0,
    strategy_id: str = "strat-1",
    notes: str = "",
    tags: list[str] | None = None,
) -> MagicMock:
    """Return a MagicMock that mimics an asyncpg Record for a trade row."""
    from datetime import datetime

    row = MagicMock()
    row.__getitem__ = lambda self, key: {  # type: ignore[assignment]
        "id": trade_id,
        "symbol": symbol,
        "side": side,
        "price": price,
        "quantity": quantity,
        "fee": fee,
        "pnl": pnl,
        "timestamp": datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC),
        "strategy_id": strategy_id,
        "notes": notes,
        "tags": tags or [],
    }[key]
    return row


class TestTradeJournal:
    """Tests for the enhanced /trades endpoint and PATCH /trades/{id}."""

    def test_trades_pagination_metadata(self) -> None:
        pool, conn = _make_mock_pool()
        # fetchval returns total count; fetch returns rows
        conn.fetchval = AsyncMock(return_value=3)
        conn.fetch = AsyncMock(return_value=[_make_trade_row(i) for i in range(1, 4)])
        client = TestClient(_make_app(pool=pool))

        resp = client.get("/api/portfolio/trades?page=1&limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert "trades" in data
        assert data["total"] == 3
        assert data["page"] == 1
        assert data["limit"] == 10
        assert data["pages"] == 1

    def test_trades_response_includes_new_fields(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetchval = AsyncMock(return_value=1)
        conn.fetch = AsyncMock(
            return_value=[_make_trade_row(notes="entry signal strong", tags=["swing", "btc"])]
        )
        client = TestClient(_make_app(pool=pool))

        resp = client.get("/api/portfolio/trades")
        assert resp.status_code == 200
        trade = resp.json()["trades"][0]
        assert trade["notes"] == "entry signal strong"
        assert trade["tags"] == ["swing", "btc"]
        assert "strategy_id" in trade

    def test_trades_db_exception_returns_empty_response(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetchval = AsyncMock(side_effect=RuntimeError("DB down"))
        client = TestClient(_make_app(pool=pool))

        resp = client.get("/api/portfolio/trades")
        assert resp.status_code == 200
        data = resp.json()
        assert data["trades"] == []
        assert data["total"] == 0

    def test_trades_page_clamped_to_minimum(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetchval = AsyncMock(return_value=0)
        conn.fetch = AsyncMock(return_value=[])
        client = TestClient(_make_app(pool=pool))

        # page=0 should be clamped to 1 without error
        resp = client.get("/api/portfolio/trades?page=0")
        assert resp.status_code == 200
        assert resp.json()["page"] == 1

    def test_update_trade_notes_and_tags(self) -> None:
        pool, conn = _make_mock_pool()
        updated_row = _make_trade_row(notes="updated note", tags=["momentum"])
        conn.fetchrow = AsyncMock(return_value=updated_row)
        client = TestClient(_make_app(pool=pool))

        resp = client.patch(
            "/api/portfolio/trades/1",
            json={"notes": "updated note", "tags": ["momentum"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["notes"] == "updated note"
        assert data["tags"] == ["momentum"]
        assert data["id"] == 1

    def test_update_trade_not_found_returns_404(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetchrow = AsyncMock(return_value=None)
        client = TestClient(_make_app(pool=pool))

        resp = client.patch("/api/portfolio/trades/999", json={"notes": "ghost"})
        assert resp.status_code == 404

    def test_update_trade_no_fields_returns_422(self) -> None:
        pool, _ = _make_mock_pool()
        client = TestClient(_make_app(pool=pool))

        resp = client.patch("/api/portfolio/trades/1", json={})
        assert resp.status_code == 422

    def test_update_trade_db_exception_returns_500(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetchrow = AsyncMock(side_effect=RuntimeError("DB error"))
        client = TestClient(_make_app(pool=pool))

        resp = client.patch("/api/portfolio/trades/1", json={"notes": "fail"})
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# CSV export (Feature 2)
# ---------------------------------------------------------------------------


class TestCSVExport:
    """Tests for GET /api/portfolio/export/csv."""

    def test_generic_csv_headers(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(return_value=[_make_trade_row()])
        client = TestClient(_make_app(pool=pool))

        resp = client.get("/api/portfolio/export/csv?format=generic")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        assert 'filename="hydra_trades_generic.csv"' in resp.headers["content-disposition"]
        lines = resp.text.strip().splitlines()
        assert lines[0] == "Date,Symbol,Side,Quantity,Price,Fee,PnL,Strategy"
        assert len(lines) == 2  # header + 1 trade row

    def test_koinly_csv_headers(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(return_value=[])
        client = TestClient(_make_app(pool=pool))

        resp = client.get("/api/portfolio/export/csv?format=koinly")
        assert resp.status_code == 200
        lines = resp.text.strip().splitlines()
        assert "Sent Amount" in lines[0]
        assert "TxHash" in lines[0]

    def test_turbotax_csv_headers(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(return_value=[])
        client = TestClient(_make_app(pool=pool))

        resp = client.get("/api/portfolio/export/csv?format=turbotax")
        assert resp.status_code == 200
        lines = resp.text.strip().splitlines()
        assert "Description of Property" in lines[0]
        assert "Gain or Loss" in lines[0]

    def test_invalid_format_returns_422(self) -> None:
        pool, _ = _make_mock_pool()
        client = TestClient(_make_app(pool=pool))

        resp = client.get("/api/portfolio/export/csv?format=cointracking")
        assert resp.status_code == 422

    def test_empty_db_returns_header_only(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(return_value=[])
        client = TestClient(_make_app(pool=pool))

        resp = client.get("/api/portfolio/export/csv")
        assert resp.status_code == 200
        lines = [ln for ln in resp.text.strip().splitlines() if ln]
        assert len(lines) == 1  # header row only

    def test_csv_export_db_exception_returns_500(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(side_effect=RuntimeError("DB down"))
        client = TestClient(_make_app(pool=pool))

        resp = client.get("/api/portfolio/export/csv")
        assert resp.status_code == 500

    def test_koinly_buy_row_populates_correctly(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(
            return_value=[_make_trade_row(side="BUY", price=67000.0, quantity=0.1)]
        )
        client = TestClient(_make_app(pool=pool))

        resp = client.get("/api/portfolio/export/csv?format=koinly")
        assert resp.status_code == 200
        lines = resp.text.strip().splitlines()
        assert len(lines) == 2
        row_cols = lines[1].split(",")
        # For a BUY: Sent Currency should be quote (USDT), Received Currency should be base (BTC)
        assert row_cols[2] == "USDT"  # Sent Currency
        assert row_cols[4] == "BTC"  # Received Currency
