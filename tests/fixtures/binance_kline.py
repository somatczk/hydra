"""Offline Binance kline driver for feed tests, plus fake ccxt exchanges.

``BinanceKlineDriver`` pushes Binance spot kline JSON through the real
``ccxt.pro.binance.handle_ohlcv`` and returns exactly what ``watch_ohlcv_for_symbols``
returns for that message (``{unified: {timeframe: rows}}``, with ``newUpdates`` batching),
so the ccxt return shape the feed consumes is real. No network is touched.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

import ccxt.pro

HOUR_MS = 3_600_000

_INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": HOUR_MS, "4h": 4 * HOUR_MS}


def spot_market(base: str = "BTC", quote: str = "USDT") -> dict[str, Any]:
    """A minimal ccxt spot market structure."""
    market_id = f"{base}{quote}"
    return {
        "id": market_id,
        "lowercaseId": market_id.lower(),
        "symbol": f"{base}/{quote}",
        "base": base,
        "quote": quote,
        "baseId": base,
        "quoteId": quote,
        "type": "spot",
        "spot": True,
        "margin": False,
        "swap": False,
        "future": False,
        "option": False,
        "contract": False,
        "linear": None,
        "inverse": None,
        "active": True,
        "precision": {"amount": 1e-5, "price": 0.01},
        "limits": {},
        "info": {},
    }


def swap_market(base: str = "BTC", quote: str = "USDT") -> dict[str, Any]:
    """The linear perpetual that shares the spot market's Binance id."""
    return {
        **spot_market(base, quote),
        "symbol": f"{base}/{quote}:{quote}",
        "type": "swap",
        "spot": False,
        "swap": True,
        "contract": True,
        "linear": True,
        "settle": quote,
        "settleId": quote,
    }


def binance_markets() -> dict[str, dict[str, Any]]:
    """BTC/USDT and ETH/USDT spot plus the BTC/USDT perpetual, keyed by unified symbol."""
    markets = [spot_market("BTC"), swap_market("BTC"), spot_market("ETH")]
    return {m["symbol"]: m for m in markets}


def binanceusdm_markets() -> dict[str, dict[str, Any]]:
    """The BTC/USDT and ETH/USDT linear perpetuals only, keyed by unified symbol.

    What the ``binanceusdm`` class's ``load_markets`` returns with its linear-only
    ``fetchMarkets`` options: linear swaps and no spot market at all.
    """
    return {m["symbol"]: m for m in (swap_market("BTC"), swap_market("ETH"))}


class _SpotStreamClient:
    url = "wss://stream.binance.com:9443/ws"

    def resolve(self, result: Any, message_hash: str) -> Any:
        return result


class BinanceKlineDriver:
    """Feed kline messages through the real ccxt.pro binance parser and read them back."""

    def __init__(self) -> None:
        self.exchange = ccxt.pro.binance()
        self.exchange.set_markets(list(binance_markets().values()))
        self._client = _SpotStreamClient()

    def push(
        self,
        market_id: str,
        interval: str,
        open_ms: int,
        ohlcv: tuple[float, float, float, float, float],
        *,
        closed: bool = False,
    ) -> None:
        """Handle one Binance kline message (``closed`` is the ``x`` flag ccxt drops)."""
        o, h, low, c, v = ohlcv
        message = {
            "e": "kline",
            "E": open_ms,
            "s": market_id,
            "k": {
                "t": open_ms,
                "T": open_ms + _INTERVAL_MS[interval] - 1,
                "s": market_id,
                "i": interval,
                "o": str(o),
                "h": str(h),
                "l": str(low),
                "c": str(c),
                "v": str(v),
                "x": closed,
            },
        }
        self.exchange.handle_ohlcv(self._client, message)

    def read(self, unified: str, timeframe: str) -> dict[str, dict[str, list[list[Any]]]]:
        """What ``watch_ohlcv_for_symbols`` returns for this stream now (consumes updates)."""
        ex = self.exchange
        candles = ex.ohlcvs[unified][timeframe]
        limit = candles.getLimit(unified, None) if ex.newUpdates else None
        rows = ex.filter_by_since_limit(candles, None, limit, 0, True)
        return ex.create_ohlcv_object(unified, timeframe, [list(r) for r in rows])

    def push_and_read(
        self,
        market_id: str,
        unified: str,
        interval: str,
        open_ms: int,
        ohlcv: tuple[float, float, float, float, float],
        *,
        closed: bool = False,
    ) -> dict[str, dict[str, list[list[Any]]]]:
        self.push(market_id, interval, open_ms, ohlcv, closed=closed)
        return self.read(unified, interval)


_CLOSED = object()


class FakeWsExchange:
    """ccxt.pro stand-in: ``watch_ohlcv_for_symbols`` returns what the test queues.

    ``close()`` behaves like ccxt.pro's: the pending watch future is cancelled, so the
    awaiting task sees ``CancelledError`` although the task itself was not cancelled. With
    ``wake_on_close = False`` the pending watch is left hanging instead.
    """

    def __init__(self, markets: dict[str, dict[str, Any]] | None = None) -> None:
        self.markets = markets if markets is not None else binance_markets()
        self.inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.watch_calls = 0
        self.close_calls = 0
        self.subscriptions: list[list[list[str]]] = []
        self.set_markets_calls = 0
        self.load_markets_calls = 0
        self.wake_on_close = True

    async def load_markets(self) -> dict[str, dict[str, Any]]:
        self.load_markets_calls += 1
        return self.markets

    def set_markets(self, markets: Any) -> None:
        self.set_markets_calls += 1
        self.markets = markets

    async def watch_ohlcv_for_symbols(self, symbols_and_timeframes: list[list[str]]) -> Any:
        self.watch_calls += 1
        self.subscriptions.append(symbols_and_timeframes)
        item = await self.inbox.get()
        if item is _CLOSED:
            raise asyncio.CancelledError
        if isinstance(item, BaseException):
            raise item
        return item

    def feed(self, *items: Any) -> None:
        for item in items:
            self.inbox.put_nowait(item)

    async def close(self) -> None:
        self.close_calls += 1
        if self.wake_on_close:
            self.inbox.put_nowait(_CLOSED)


class FakeRestExchange:
    """ccxt async REST stand-in serving ``fetch_ohlcv`` pages from a fixed candle list."""

    def __init__(self, rows: Iterable[list[Any]] = (), markets: dict[str, Any] | None = None):
        self.rows = sorted((list(r) for r in rows), key=lambda r: r[0])
        self.markets = markets if markets is not None else binance_markets()
        self.calls: list[tuple[str, str, int, int]] = []
        self.errors: list[BaseException] = []
        self.close_calls = 0

    async def load_markets(self) -> dict[str, Any]:
        return self.markets

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None = None, limit: int | None = None
    ) -> list[list[Any]]:
        self.calls.append((symbol, timeframe, since or 0, limit or 0))
        if self.errors:
            raise self.errors.pop(0)
        page = [r for r in self.rows if since is None or r[0] >= since]
        return [list(r) for r in page[: limit or len(page)]]

    async def close(self) -> None:
        self.close_calls += 1
