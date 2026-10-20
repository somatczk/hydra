"""Market data routes: real order book via one cached client, funding rates fail loudly."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import ccxt
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hydra.dashboard import api
from hydra.dashboard.routes import market_data
from hydra.dashboard.routes._db import install_error_handlers
from hydra.dashboard.routes.market_data import close_market_clients, router

_BOOK: dict[str, Any] = {
    "bids": [[65000.5, 1.25], [65000.0, 0.5], [64999.0, 2.0]],
    "asks": [[65001.0, 0.75], [65002.5, 1.0], [65003.0, 3.0]],
    "timestamp": 1_760_000_000_000,
}


class _FakeExchange:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.book: dict[str, Any] = _BOOK
        self.calls: list[tuple[str, int]] = []
        self.closed = 0

    async def fetch_order_book(self, symbol: str, limit: int) -> dict[str, Any]:
        self.calls.append((symbol, limit))
        if self.error is not None:
            raise self.error
        return self.book

    async def close(self) -> None:
        self.closed += 1


class _Factory:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.created: list[_FakeExchange] = []

    def __call__(self, exchange_id: str) -> _FakeExchange:
        ex = _FakeExchange(self.error)
        self.created.append(ex)
        return ex


@pytest.fixture()
def factory(monkeypatch: pytest.MonkeyPatch) -> _Factory:
    f = _Factory()
    monkeypatch.setattr(market_data, "_exchange_factory", f)
    return f


def _make_app(pool: Any = None) -> FastAPI:
    app = FastAPI()
    app.state.db_pool = pool
    install_error_handlers(app)
    app.include_router(router)
    return app


class TestOrderBook:
    def test_single_client_reused(self, factory: _Factory) -> None:
        client = TestClient(_make_app())
        for _ in range(10):
            resp = client.get("/api/market/orderbook?symbol=BTC/USDT")
            assert resp.status_code == 200
        assert len(factory.created) == 1
        data = resp.json()
        assert data["bids"] == _BOOK["bids"]
        assert data["asks"] == _BOOK["asks"]
        assert data["spread"] == 0.5
        assert data["exchange"] == "binance"
        assert data["timestamp"].startswith("2025-10-09T08:53:20")
        assert data["fetched_at"] > data["timestamp"]
        assert "mock" not in data

    def test_missing_exchange_timestamp_is_null_not_server_time(self, factory: _Factory) -> None:
        book = {**_BOOK, "timestamp": None}
        app = _make_app()
        client = TestClient(app)
        client.get("/api/market/orderbook")
        factory.created[0].book = book
        data = client.get("/api/market/orderbook").json()
        assert data["timestamp"] is None
        assert data["fetched_at"].startswith("20")

    def test_ccxt_failure_is_503_no_mock(self, factory: _Factory) -> None:
        factory.error = ccxt.NetworkError("binance GET https://api.binance.com 418 I'm a teapot")
        client = TestClient(_make_app())
        resp = client.get("/api/market/orderbook")
        assert resp.status_code == 503
        body = resp.json()
        assert body["detail"]["state"] == "unavailable"
        assert body["detail"]["code"] == "exchange_unavailable"
        assert "mock" not in resp.text
        assert "teapot" not in resp.text
        assert factory.created[0].closed == 1

        client.get("/api/market/orderbook")
        assert len(factory.created) == 2

    def test_bad_symbol_422(self, factory: _Factory) -> None:
        factory.error = ccxt.BadSymbol("binance does not have market symbol NOPE")
        resp = TestClient(_make_app()).get("/api/market/orderbook?symbol=NOPE")
        assert resp.status_code == 422
        assert resp.json()["detail"]["state"] == "invalid"

    @pytest.mark.parametrize("exchange", ["bybit", "__class__", "okx"])
    def test_exchange_allowlist(self, factory: _Factory, exchange: str) -> None:
        resp = TestClient(_make_app()).get(f"/api/market/orderbook?exchange={exchange}")
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "exchange_not_allowed"
        assert factory.created == []

    def test_depth_cap(self, factory: _Factory) -> None:
        client = TestClient(_make_app())
        resp = client.get("/api/market/orderbook?depth=0")
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["code"] == "validation_error"
        assert detail["state"] == "invalid"
        assert detail["message"].startswith("query.depth: ")
        assert detail["errors"][0]["loc"] == ["query", "depth"]
        assert client.get("/api/market/orderbook?depth=101").status_code == 422
        assert factory.created == []
        assert client.get("/api/market/orderbook?depth=100").status_code == 200
        assert factory.created[0].calls == [("BTCUSDT", 100)]

    def test_depth_truncates_levels(self, factory: _Factory) -> None:
        data = TestClient(_make_app()).get("/api/market/orderbook?depth=2").json()
        assert data["bids"] == _BOOK["bids"][:2]
        assert data["asks"] == _BOOK["asks"][:2]

    def test_close_market_clients_closes_cached(self, factory: _Factory) -> None:
        app = _make_app()
        TestClient(app).get("/api/market/orderbook")
        asyncio.run(close_market_clients(app))
        assert factory.created[0].closed == 1
        assert app.state.market_data_clients == {}

    def test_close_market_clients_without_clients_is_noop(self) -> None:
        asyncio.run(close_market_clients(_make_app()))


class _ScriptedExchange:
    """Each fetch_order_book call runs the next scripted step; fails if closed while in flight."""

    def __init__(self, steps: list[Callable[[], Awaitable[None]]]) -> None:
        self.steps = steps
        self.closed = 0

    async def fetch_order_book(self, symbol: str, limit: int) -> dict[str, Any]:
        await self.steps.pop(0)()
        if self.closed:
            raise ccxt.NetworkError("session closed")
        return _BOOK

    async def close(self) -> None:
        self.closed += 1


class TestConcurrentEviction:
    async def test_late_failure_does_not_evict_replacement_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        b_started, b_fail, c_started, c_go = (asyncio.Event() for _ in range(4))

        async def b_step() -> None:
            b_started.set()
            await b_fail.wait()
            raise ccxt.NetworkError("late failure")

        async def a_step() -> None:
            raise ccxt.NetworkError("fast failure")

        async def c_step() -> None:
            c_started.set()
            await c_go.wait()

        client0 = _ScriptedExchange([b_step, a_step])
        client1 = _ScriptedExchange([c_step])
        made = iter([client0, client1])
        monkeypatch.setattr(market_data, "_exchange_factory", lambda _id: next(made))

        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            url = "/api/market/orderbook"
            b = asyncio.create_task(http.get(url))
            await b_started.wait()
            a = await http.get(url)
            c = asyncio.create_task(http.get(url))
            await c_started.wait()
            b_fail.set()
            b_resp = await b
            c_go.set()
            c_resp = await c

        assert a.status_code == 503
        assert b_resp.status_code == 503
        assert c_resp.status_code == 200
        assert client0.closed >= 1
        assert client1.closed == 0
        assert app.state.market_data_clients["binance"] is client1


class TestLifespanClosesClients:
    async def test_app_shutdown_closes_cached_clients(self) -> None:
        names = [
            "_init_logging",
            "_init_db_pool",
            "_load_exchange_credentials",
            "_init_session_manager",
            "_run_migrations",
            "_populate_backtest_cache",
            "_shutdown_session_manager",
            "_close_db_pool",
        ]
        fake = _FakeExchange()
        app = FastAPI()
        with patch.multiple(api, **{n: AsyncMock() for n in names}):
            async with api._lifespan(app):
                app.state.market_data_clients = {"binance": fake}
        assert fake.closed == 1
        assert app.state.market_data_clients == {}


class TestFundingRates:
    @pytest.mark.parametrize(
        "path", ["/api/market/funding-rates", "/api/market/funding-rates/current"]
    )
    def test_funding_rates_no_pool_503(self, path: str) -> None:
        resp = TestClient(_make_app(pool=None)).get(path)
        assert resp.status_code == 503
        assert resp.json()["detail"]["state"] == "unavailable"

    @pytest.mark.parametrize(
        "path", ["/api/market/funding-rates", "/api/market/funding-rates/current"]
    )
    def test_funding_rates_db_down_503(self, path: str) -> None:
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(side_effect=OSError("refused"))
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        resp = TestClient(_make_app(pool=pool)).get(path)
        assert resp.status_code == 503

    def test_funding_rates_empty_table_is_200_empty(self) -> None:
        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[])
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        resp = TestClient(_make_app(pool=pool)).get("/api/market/funding-rates")
        assert resp.status_code == 200
        assert resp.json() == []


def test_mock_order_book_is_gone() -> None:
    assert not hasattr(market_data, "_build_mock_order_book")
    assert "mock" not in market_data.OrderBookSnapshot.model_fields
