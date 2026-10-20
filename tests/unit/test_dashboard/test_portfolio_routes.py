"""Tests for portfolio API routes."""

from __future__ import annotations

from datetime import UTC
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
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
# Outages are 503, empty data is 200, real bugs are 500
# ---------------------------------------------------------------------------

_GET_ENDPOINTS = [
    "/api/portfolio/summary",
    "/api/portfolio/positions",
    "/api/portfolio/equity-curve",
    "/api/portfolio/daily-pnl",
    "/api/portfolio/monthly-returns",
    "/api/portfolio/attribution",
    "/api/portfolio/trades",
    "/api/portfolio/live-metrics",
]


def _assert_unavailable(resp: Any) -> None:
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["state"] == "unavailable"
    assert detail["code"] == "database_unavailable"
    assert isinstance(detail["message"], str)


def _make_refusing_pool() -> MagicMock:
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(side_effect=ConnectionError("refused"))
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


class TestPortfolioUnavailable:
    @pytest.mark.parametrize("path", _GET_ENDPOINTS)
    def test_no_pool_is_503(self, client_no_pool: TestClient, path: str) -> None:
        _assert_unavailable(client_no_pool.get(path))

    @pytest.mark.parametrize("path", _GET_ENDPOINTS)
    def test_acquire_failure_is_503(self, path: str) -> None:
        client = TestClient(_make_app(pool=_make_refusing_pool()))
        _assert_unavailable(client.get(path))

    @pytest.mark.parametrize("path", _GET_ENDPOINTS)
    def test_query_failure_is_503(self, path: str) -> None:
        pool, conn = _make_mock_pool()
        err = asyncpg.exceptions.ConnectionDoesNotExistError("connection was closed")
        conn.fetch = AsyncMock(side_effect=err)
        conn.fetchval = AsyncMock(side_effect=err)
        conn.fetchrow = AsyncMock(side_effect=err)
        client = TestClient(_make_app(pool=pool))
        _assert_unavailable(client.get(path))

    @pytest.mark.parametrize("path", _GET_ENDPOINTS)
    def test_postgres_error_is_503(self, path: str) -> None:
        pool, conn = _make_mock_pool()
        err = asyncpg.PostgresError("boom")
        conn.fetch = AsyncMock(side_effect=err)
        conn.fetchval = AsyncMock(side_effect=err)
        conn.fetchrow = AsyncMock(side_effect=err)
        client = TestClient(_make_app(pool=pool))
        _assert_unavailable(client.get(path))

    @pytest.mark.parametrize("path", _GET_ENDPOINTS)
    def test_handler_bug_is_500_not_503(self, path: str) -> None:
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(return_value=[{"bogus": 1}])
        conn.fetchval = AsyncMock(return_value=1)
        conn.fetchrow = AsyncMock(return_value={"bogus": 1})
        client = TestClient(_make_app(pool=pool), raise_server_exceptions=False)
        assert client.get(path).status_code == 500

    def test_update_trade_no_pool_returns_503(self, client_no_pool: TestClient) -> None:
        _assert_unavailable(client_no_pool.patch("/api/portfolio/trades/1", json={"notes": "x"}))

    def test_export_csv_no_pool_returns_503(self, client_no_pool: TestClient) -> None:
        _assert_unavailable(client_no_pool.get("/api/portfolio/export/csv"))

    def test_export_csv_invalid_format_returns_422(self, client_no_pool: TestClient) -> None:
        # Invalid format is caught before the pool check
        resp = client_no_pool.get("/api/portfolio/export/csv?format=invalid")
        assert resp.status_code == 422
        assert resp.json()["detail"]["state"] == "invalid"


class TestEmptyDatabase:
    """A working DB with no rows is not an outage."""

    def _client(self) -> TestClient:
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(return_value=[])
        conn.fetchval = AsyncMock(return_value=0)
        conn.fetchrow = AsyncMock(return_value=None)
        return TestClient(_make_app(pool=pool))

    @pytest.mark.parametrize(
        "path",
        [
            "/api/portfolio/positions",
            "/api/portfolio/equity-curve",
            "/api/portfolio/daily-pnl",
            "/api/portfolio/monthly-returns",
            "/api/portfolio/attribution",
        ],
    )
    def test_lists_are_200_empty(self, path: str) -> None:
        resp = self._client().get(path)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_trades_are_200_empty(self) -> None:
        resp = self._client().get("/api/portfolio/trades")
        assert resp.status_code == 200
        assert resp.json() == {"trades": [], "total": 0, "page": 1, "limit": 20, "pages": 0}

    def test_paper_summary_is_the_real_paper_capital(self) -> None:
        resp = self._client().get("/api/portfolio/summary?source=paper")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_value"] == 10000.0
        assert data["realized_pnl"] == 0.0
        assert data["unrealized_pnl"] == 0.0


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
        conn.fetchval = AsyncMock(return_value=68000.0)
        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/portfolio/positions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["pair"] == "BTC/USDT"
        assert data[0]["current_price"] == 68000.0
        assert data[0]["unrealized_pnl"] == 87.0

    def test_positions_missing_ohlcv_table_uses_entry_price(self) -> None:
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
                    "unrealized_pnl": 0.0,
                    "realized_pnl": 0.0,
                }
            ]
        )
        conn.fetchval = AsyncMock(side_effect=asyncpg.UndefinedTableError("no ts.ohlcv_1m"))
        client = TestClient(_make_app(pool=pool))
        resp = client.get("/api/portfolio/positions")
        assert resp.status_code == 200
        assert resp.json()[0]["current_price"] == 67420.0


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

    def test_update_trade_db_exception_returns_503(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetchrow = AsyncMock(side_effect=ConnectionResetError("DB error"))
        client = TestClient(_make_app(pool=pool))

        _assert_unavailable(client.patch("/api/portfolio/trades/1", json={"notes": "fail"}))


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

    def test_csv_export_db_exception_returns_503(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(side_effect=asyncpg.PostgresError("DB down"))
        client = TestClient(_make_app(pool=pool))

        _assert_unavailable(client.get("/api/portfolio/export/csv"))

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
