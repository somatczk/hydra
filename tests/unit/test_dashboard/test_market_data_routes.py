"""Market data routes: real order book via one cached client, funding rates fail loudly."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import ccxt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hydra.dashboard.routes import market_data
from hydra.dashboard.routes.market_data import close_market_clients, router

_BOOK: dict[str, Any] = {
    "bids": [[65000.5, 1.25], [65000.0, 0.5], [64999.0, 2.0]],
    "asks": [[65001.0, 0.75], [65002.5, 1.0], [65003.0, 3.0]],
    "timestamp": 1_760_000_000_000,
}


class _FakeExchange:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, int]] = []
        self.closed = 0

    async def fetch_order_book(self, symbol: str, limit: int) -> dict[str, Any]:
        self.calls.append((symbol, limit))
        if self.error is not None:
            raise self.error
        return _BOOK

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
        assert "mock" not in data

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
        assert client.get("/api/market/orderbook?depth=0").status_code == 422
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
