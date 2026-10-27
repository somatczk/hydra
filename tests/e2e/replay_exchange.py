"""Replay of committed Binance closed bars through ccxt.pro- and ccxt-shaped fakes.

``ReplayMarket`` loads ``data/binance_btcusdt_1m.json`` and rebases it to the wall clock: the first
``warmup`` bars end at the newest closed minute (they are seeded into ``ts.ohlcv_1m``), and each
later bar opens one minute after the previous one, so the live bars lie at or after the current
minute. Every live bar is streamed as three updates of the forming candle: open only, then a
synthetic update whose close is the bar's high, then the real final bar. One extra open-only frame
of the following minute closes the last real bar. The engine's feed may only act on final bars.

Frames are released explicitly (``release_through``) and the cursor lives on ``ReplayMarket``, so
the WebSocket instances the feed creates (or recreates on restart) share it. ``ReplayExchange``
blocks once released frames run out, which is a stalled stream. No network is touched.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from hydra.core.types import OHLCV
from hydra.execution.client_factory import ClientFactory, SharedRestClients, bars_repository

FIXTURE = Path(__file__).parent / "data" / "binance_btcusdt_1m.json"
MINUTE = timedelta(minutes=1)
UNIFIED = "BTC/USDT"


def btcusdt_spot_market() -> dict[str, Any]:
    """Binance BTC/USDT spot filters as ccxt reports them (lot 0.00001, min notional 5 USDT)."""
    return {
        "id": "BTCUSDT",
        "lowercaseId": "btcusdt",
        "symbol": UNIFIED,
        "base": "BTC",
        "quote": "USDT",
        "baseId": "BTC",
        "quoteId": "USDT",
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
        "precision": {"amount": 1e-05, "price": 0.01},
        "limits": {"amount": {"min": 1e-05}, "cost": {"min": 5.0}},
        "info": {},
    }


@dataclass(frozen=True, slots=True)
class Bar:
    """One fixture bar, rebased: Decimal prices as the fixture states them."""

    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @property
    def ms(self) -> int:
        return int(self.open_time.timestamp() * 1000)

    def ohlcv(self) -> OHLCV:
        return OHLCV(
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            timestamp=self.open_time,
        )

    def row(self, close: Decimal | None = None, *, final: bool = True) -> list[float]:
        """A ccxt OHLCV row; ``final=False`` is the open-only first update of the candle."""
        if not final:
            o = float(self.open)
            return [self.ms, o, o, o, o, 0.0]
        c = self.close if close is None else close
        return [
            self.ms,
            float(self.open),
            float(self.high),
            float(self.low),
            float(c),
            float(self.volume),
        ]


def load_fixture() -> list[list[Any]]:
    doc = json.loads(FIXTURE.read_text())
    provenance = doc["provenance"]
    assert (provenance["exchange"], provenance["symbol"], provenance["interval"]) == (
        "binance",
        "BTCUSDT",
        "1m",
    )
    rows: list[list[Any]] = doc["bars"]
    return rows


class ReplayMarket:
    """The fixture, rebased by ``anchor`` to the newest closed minute, and the replay cursor."""

    def __init__(self, warmup: int) -> None:
        self.rows = load_fixture()
        self.warmup = warmup
        self.bars: list[Bar] = []
        self.frames: list[list[float]] = []
        self.allowed = 0
        self.served = 0
        self._wake = asyncio.Event()
        self.instances: list[ReplayExchange] = []
        self.rest_calls: list[tuple[str, int | None]] = []

    def anchor(self, tail: datetime) -> None:
        """Warm-up bars end at ``tail``; live bar ``j`` opens ``j + 1`` minutes after it."""
        start = tail - (self.warmup - 1) * MINUTE
        self.bars = [
            Bar(start + i * MINUTE, *(Decimal(v) for v in row[1:]))
            for i, row in enumerate(self.rows)
        ]
        self.frames = []
        for bar in self.live:
            self.frames += [bar.row(final=False), bar.row(bar.high), bar.row()]
        last = self.bars[-1]
        after = Bar(
            last.open_time + MINUTE, last.close, last.close, last.close, last.close, Decimal(0)
        )
        self.frames.append(after.row(final=False))

    @property
    def history(self) -> list[Bar]:
        return self.bars[: self.warmup]

    @property
    def live(self) -> list[Bar]:
        return self.bars[self.warmup :]

    def release_through(self, live_index: int) -> None:
        """Let the stream run until live bar ``live_index`` has closed (its successor opened)."""
        self.allowed = max(self.allowed, 3 * (live_index + 1) + 1)
        self._wake.set()

    async def next_frame(self, closed: asyncio.Event) -> list[float] | None:
        while self.served >= self.allowed:
            self._wake.clear()
            waiters = [
                asyncio.ensure_future(self._wake.wait()),
                asyncio.ensure_future(closed.wait()),
            ]
            try:
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in waiters:
                    w.cancel()
            if closed.is_set():
                return None
        frame = self.frames[self.served]
        self.served += 1
        return frame

    def clients(self, *, history_window: int) -> ClientFactory:
        """The session seams: replay WebSocket, REST over the same bars, Binance spot rules."""

        def ws_factory(exchange_id: str) -> Callable[[], ReplayExchange]:
            def make() -> ReplayExchange:
                ex = ReplayExchange(self)
                self.instances.append(ex)
                return ex

            return make

        async def markets_loader(exchange_id: str) -> list[Mapping[str, Any]]:
            return [btcusdt_spot_market()]

        return ClientFactory(
            ws_factory=ws_factory,
            rest_pool=SharedRestClients(rest_factory=lambda eid, creds: ReplayRest(self)),
            markets_loader=markets_loader,
            repository_factory=bars_repository,
            history_window=history_window,
        )


class ReplayExchange:
    """ccxt.pro stand-in: ``watch_ohlcv_for_symbols`` returns the next released frame."""

    def __init__(self, market: ReplayMarket) -> None:
        self._market = market
        self.markets = {UNIFIED: btcusdt_spot_market()}
        self._closed = asyncio.Event()
        self.close_calls = 0

    async def load_markets(self) -> dict[str, Any]:
        return self.markets

    def set_markets(self, markets: Any) -> None:
        self.markets = dict(markets)

    async def watch_ohlcv_for_symbols(
        self, symbols_and_timeframes: list[list[str]]
    ) -> dict[str, dict[str, list[list[float]]]]:
        assert symbols_and_timeframes == [[UNIFIED, "1m"]], symbols_and_timeframes
        frame = await self._market.next_frame(self._closed)
        if frame is None:
            raise asyncio.CancelledError
        return {UNIFIED: {"1m": [frame]}}

    async def watch_trades(self, symbol: str) -> list[Any]:
        await self._closed.wait()
        raise asyncio.CancelledError

    async def close(self) -> None:
        self.close_calls += 1
        self._closed.set()


class ReplayRest:
    """ccxt async REST stand-in over the same rebased bars (final values only)."""

    def __init__(self, market: ReplayMarket) -> None:
        self._market = market

    async def load_markets(self) -> dict[str, Any]:
        return {UNIFIED: btcusdt_spot_market()}

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None = None, limit: int | None = None
    ) -> list[list[float]]:
        assert (symbol, timeframe) == (UNIFIED, "1m")
        self._market.rest_calls.append((symbol, since))
        rows = [b.row() for b in self._market.bars if since is None or b.ms >= since]
        return rows[: limit or len(rows)]

    async def close(self) -> None:
        return None
