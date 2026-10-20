"""Tests for portfolio API routes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hydra.dashboard.routes import portfolio
from hydra.dashboard.routes._db import install_error_handlers
from hydra.dashboard.routes.portfolio import router


def _make_app(pool: Any = None) -> FastAPI:
    app = FastAPI()
    app.state.db_pool = pool
    install_error_handlers(app)
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


# ---------------------------------------------------------------------------
# Account maths on a table-level fake (the DB boundary): RK-13, RK-15, DA-5, paper capital
# ---------------------------------------------------------------------------

T0 = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)


class FakePortfolioDb:
    """The rows the portfolio queries read, answered the way the SQL defines them."""

    def __init__(self) -> None:
        self.paper_capital: str | None = "10000"
        self.sessions: dict[str, dict[str, Any]] = {}
        self.trades: list[dict[str, Any]] = []
        self.positions: list[dict[str, Any]] = []
        self.snapshots: list[dict[str, Any]] = []
        self.bars: list[dict[str, Any]] = []

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[FakePortfolioDb]:
        yield self

    def session(self, sid: str, allocated: float | None, status: str = "running") -> None:
        self.sessions[sid] = {"paper_capital": allocated, "status": status, "timeframe": "1h"}

    def snapshot(self, sid: str | None, minute: int, value: float, source: str = "paper") -> None:
        self.snapshots.append(
            {
                "id": len(self.snapshots) + 1,
                "timestamp": T0 + timedelta(minutes=minute),
                "session_id": sid,
                "total_value": Decimal(str(value)),
                "source": source,
            }
        )

    def trade(
        self, sid: str, pnl: float, fee: float, when: datetime, source: str = "paper"
    ) -> None:
        self.trades.append(
            {"session_id": sid, "pnl": pnl, "fee": fee, "timestamp": when, "source": source}
        )

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if sql == "SELECT value FROM system_config WHERE key = 'paper_capital'":
            return self.paper_capital
        if sql == portfolio._RUNNING_UNREALIZED_SQL:
            (source,) = args
            return sum(
                p["unrealized_pnl"]
                for p in self.positions
                if self.sessions.get(p["session_id"], {}).get("status") == "running"
                and source in (None, p["source"])
            )
        if sql == portfolio._LATEST_CLOSE_SQL:
            exchange, symbol, timeframe = args
            bars = [
                b
                for b in self.bars
                if (b["exchange"], b["symbol"], b["timeframe"]) == (exchange, symbol, timeframe)
            ]
            return max(bars, key=lambda b: b["timestamp"])["close"] if bars else None
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any]:
        assert sql == portfolio._TRADE_TOTALS_SQL
        (source,) = args
        rows = [t for t in self.trades if source in (None, t["source"])]
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return {
            "realized": sum(t["pnl"] for t in rows),
            "fees": sum(t["fee"] for t in rows),
            "daily": sum(t["pnl"] - t["fee"] for t in rows if t["timestamp"] >= today),
        }

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if sql == portfolio._SESSION_SNAPSHOTS_SQL:
            rows = [
                {**b, "allocated": self.sessions[b["session_id"]]["paper_capital"]}
                for b in self.snapshots
                if b["source"] == "paper"
                and b["session_id"] in self.sessions
                and self.sessions[b["session_id"]]["paper_capital"] is not None
            ]
            return sorted(rows, key=lambda r: (r["timestamp"], r["id"]))
        if sql == portfolio._ACCOUNT_SNAPSHOTS_SQL:
            (source,) = args
            rows = [
                b
                for b in self.snapshots
                if b["session_id"] is None and source in (None, b["source"])
            ]
            return sorted(rows, key=lambda r: (r["timestamp"], r["id"]))
        if sql == portfolio._POSITIONS_SQL:
            (source,) = args
            return [
                {
                    **p,
                    "timeframe": self.sessions.get(p["session_id"], {}).get("timeframe", "1m"),
                }
                for p in self.positions
                if p["quantity"] != 0 and source in (None, p["source"])
            ]
        raise AssertionError(f"unexpected fetch: {sql}")


@pytest.fixture()
def db() -> FakePortfolioDb:
    return FakePortfolioDb()


@pytest.fixture()
def client(db: FakePortfolioDb) -> TestClient:
    return TestClient(_make_app(pool=db))


class TestPaperSummary:
    def test_empty_paper_account_is_the_persisted_paper_capital(
        self, db: FakePortfolioDb, client: TestClient
    ) -> None:
        db.paper_capital = "25000"
        data = client.get("/api/portfolio/summary?source=paper").json()
        assert data["total_value"] == 25000.0
        assert data["realized_pnl"] == 0.0
        assert data["max_drawdown_pct"] == 0.0

    def test_missing_paper_capital_row_is_503_never_10000(
        self, db: FakePortfolioDb, client: TestClient
    ) -> None:
        db.paper_capital = None
        for path in ("summary", "equity-curve", "monthly-returns", "live-metrics"):
            resp = client.get(f"/api/portfolio/{path}?source=paper")
            assert resp.status_code == 503, path
            assert resp.json()["detail"]["code"] == "system_config_missing"

    def test_two_interleaved_sleeves_are_not_an_80pct_drawdown(
        self, db: FakePortfolioDb, client: TestClient
    ) -> None:
        # RK-13: 9000 and 1000 sleeves, both profitable, snapshots interleaved
        db.session("big", 9000)
        db.session("small", 1000)
        for minute, (sid, value) in enumerate(
            [("big", 9100), ("small", 1050), ("big", 9150), ("small", 1060)]
        ):
            db.snapshot(sid, minute, value)
        data = client.get("/api/portfolio/summary?source=paper").json()
        assert data["max_drawdown_pct"] == 0.0
        curve = client.get("/api/portfolio/equity-curve?source=paper").json()
        assert [p["value"] for p in curve] == [10100.0, 10150.0, 10200.0, 10210.0]

    def test_account_drawdown_from_one_losing_sleeve(
        self, db: FakePortfolioDb, client: TestClient
    ) -> None:
        db.session("big", 9000)
        db.session("small", 1000)
        db.snapshot("big", 0, 9100)
        db.snapshot("small", 1, 1050)
        db.snapshot("big", 2, 8600)
        # peak 10150, trough 10000 - 400 + 50 = 9650
        data = client.get("/api/portfolio/summary?source=paper").json()
        assert data["max_drawdown_pct"] == round((10150 - 9650) / 10150 * 100, 1)

    def test_stopped_session_keeps_its_result_and_legacy_rows_are_ignored(
        self, db: FakePortfolioDb, client: TestClient
    ) -> None:
        db.session("old", 5000, status="stopped")
        db.session("new", 5000)
        db.snapshot("old", 0, 4800)
        db.snapshot(None, 1, 123.0)
        db.snapshot("new", 2, 5100)
        curve = client.get("/api/portfolio/equity-curve?source=paper").json()
        assert [p["value"] for p in curve] == [9800.0, 9900.0]

    def test_daily_pnl_counts_stopped_sessions_and_nets_fees(
        self, db: FakePortfolioDb, client: TestClient
    ) -> None:
        # RK-15: stopping the losing session must not hide today's loss
        now = datetime.now(UTC)
        db.session("loser", 5000, status="stopped")
        db.session("winner", 5000)
        db.trade("loser", -100.0, 20.0, now)
        db.trade("winner", 50.0, 0.0, now)
        db.trade("winner", 500.0, 0.0, now - timedelta(days=2))
        data = client.get("/api/portfolio/summary?source=paper").json()
        assert data["daily_pnl"] == -70.0
        assert data["realized_pnl"] == 450.0
        assert data["total_fees"] == 20.0
        assert data["total_value"] == 10000.0 + 450.0 - 20.0

    def test_unrealized_counts_running_sessions_only(
        self, db: FakePortfolioDb, client: TestClient
    ) -> None:
        db.session("run", 5000)
        db.session("done", 5000, status="stopped")
        db.positions = [
            {"session_id": "run", "unrealized_pnl": 30.0, "source": "paper"},
            {"session_id": "done", "unrealized_pnl": 999.0, "source": "paper"},
        ]
        data = client.get("/api/portfolio/summary?source=paper").json()
        assert data["unrealized_pnl"] == 30.0
        assert data["total_value"] == 10030.0

    def test_monthly_returns_from_the_account_curve(
        self, db: FakePortfolioDb, client: TestClient
    ) -> None:
        db.session("a", 5000)
        db.session("b", 5000)
        db.snapshot("a", 0, 5000)
        db.snapshot("b", 1, 5500)
        assert client.get("/api/portfolio/monthly-returns?source=paper").json() == [
            {"month": "2026-10", "return_pct": 5.0}
        ]


def _position(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": 1,
        "strategy_id": "strat-1",
        "session_id": "s1",
        "exchange_id": "binance",
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "quantity": 0.15,
        "avg_entry_price": 67420.0,
        "unrealized_pnl": 100.0,
        "realized_pnl": 0.0,
        "source": "paper",
    }
    row.update(overrides)
    return row


def _bar(exchange: str, timeframe: str, minute: int, close: float) -> dict[str, Any]:
    return {
        "exchange": exchange,
        "symbol": "BTCUSDT",
        "timeframe": timeframe,
        "timestamp": T0 + timedelta(minutes=minute),
        "close": close,
    }


class TestPositions:
    def test_mark_is_the_positions_own_exchange_and_session_timeframe(
        self, db: FakePortfolioDb, client: TestClient
    ) -> None:
        # DA-5: other venues and other timeframes are newer but must not be read
        db.session("s1", 5000)
        db.positions = [_position()]
        db.bars = [
            _bar("binance", "1h", 0, 68000.0),
            _bar("bybit", "1h", 30, 99999.0),
            _bar("binance", "1d", 45, 50000.0),
        ]
        [pos] = client.get("/api/portfolio/positions?source=paper").json()
        assert pos["pair"] == "BTC/USDT"
        assert pos["current_price"] == 68000.0
        assert pos["unrealized_pnl"] == 87.0

    def test_no_bar_uses_the_stored_unrealized_pnl(
        self, db: FakePortfolioDb, client: TestClient
    ) -> None:
        db.session("s1", 5000)
        db.positions = [_position(unrealized_pnl=15.0, quantity=0.1, avg_entry_price=60000.0)]
        [pos] = client.get("/api/portfolio/positions").json()
        assert pos["unrealized_pnl"] == 15.0
        assert pos["current_price"] == 60150.0

    def test_missing_ohlcv_table_uses_the_stored_unrealized_pnl(self) -> None:
        pool, conn = _make_mock_pool()
        conn.fetch = AsyncMock(return_value=[{**_position(unrealized_pnl=0.0), "timeframe": "1h"}])
        conn.fetchval = AsyncMock(side_effect=asyncpg.UndefinedTableError("no ts.ohlcv_1m"))
        resp = TestClient(_make_app(pool=pool)).get("/api/portfolio/positions")
        assert resp.status_code == 200
        assert resp.json()[0]["current_price"] == 67420.0

    def test_flat_rows_are_not_positions(self, db: FakePortfolioDb, client: TestClient) -> None:
        db.positions = [_position(quantity=0)]
        assert client.get("/api/portfolio/positions").json() == []


class TestDateFilters:
    """DB-20: a bad date is a 422 envelope, never a 500."""

    @pytest.mark.parametrize("path", ["/api/portfolio/trades", "/api/portfolio/export/csv"])
    @pytest.mark.parametrize("param", ["from_date", "to_date"])
    def test_bad_date_is_422_envelope(self, path: str, param: str) -> None:
        pool, conn = _make_mock_pool()
        conn.fetchval = AsyncMock(return_value=0)
        conn.fetch = AsyncMock(return_value=[])
        resp = TestClient(_make_app(pool=pool)).get(f"{path}?{param}=notadate")
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["code"] == "validation_error"
        assert detail["state"] == "invalid"
        assert detail["errors"][0]["loc"] == ["query", param]
        conn.fetch.assert_not_awaited()

    @pytest.mark.parametrize("path", ["/api/portfolio/trades", "/api/portfolio/export/csv"])
    def test_dates_reach_the_query_as_aware_utc(self, path: str) -> None:
        pool, conn = _make_mock_pool()
        conn.fetchval = AsyncMock(return_value=0)
        conn.fetch = AsyncMock(return_value=[])
        resp = TestClient(_make_app(pool=pool)).get(
            f"{path}?from_date=2026-01-01&to_date=2026-01-31T12:00:00%2B02:00"
        )
        assert resp.status_code == 200
        args = conn.fetch.await_args.args[1:]
        assert args[0] == datetime(2026, 1, 1, tzinfo=UTC)
        assert args[1] == datetime(2026, 1, 31, 10, 0, tzinfo=UTC)
        assert all(a.tzinfo is not None for a in args[:2])


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
