"""rank_universe / compute_universe / UniverseRefresher: the pure ranking and its loop.

The ranking tests are pure (no network, exact numbers): eligibility, ordering, the
tie-break and the 100-cap boundary. ``compute_universe`` runs against a record client
(one ``load_markets`` + one ``fetch_tickers``), and the refresher's schedule runs on an
injected sleep — no test waits on wall time.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from itertools import permutations
from typing import Any

import pytest

from hydra.data.universe import (
    _NON_TRADING_BASES,
    UniverseEntry,
    UniverseRefresher,
    compute_universe,
    rank_universe,
)


def _market(
    symbol: str,
    *,
    base: str | None = None,
    quote: str = "USDT",
    spot: bool = True,
    active: bool = True,
) -> dict[str, Any]:
    return {
        "id": symbol.replace("/", ""),
        "symbol": symbol,
        "base": base if base is not None else symbol.split("/")[0],
        "quote": quote,
        "spot": spot,
        "active": active,
    }


def _ticker(symbol: str, quote_volume: float | None) -> dict[str, Any]:
    return {"symbol": symbol, "quoteVolume": quote_volume, "baseVolume": 0.0}


def _eligible(n: int, top_volume: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """*n* eligible markets with volumes ``top_volume`` down to ``top_volume - n + 1``."""
    symbols = [f"M{i:03d}/USDT" for i in range(n)]
    markets = [_market(symbol) for symbol in symbols]
    tickers = [_ticker(symbol, top_volume - i) for i, symbol in enumerate(symbols)]
    return markets, tickers


class TestRankUniverse:
    def test_120_eligible_markets_rank_exactly_100_in_volume_order(self) -> None:
        markets, tickers = _eligible(120, 1200)
        entries = rank_universe(markets, tickers)
        assert len(entries) == 100
        assert [entry.rank for entry in entries] == list(range(1, 101))
        assert entries[0] == UniverseEntry(rank=1, symbol="M000USDT", quote_volume=1200.0)
        assert entries[99] == UniverseEntry(rank=100, symbol="M099USDT", quote_volume=1101.0)
        volumes = [entry.quote_volume for entry in entries]
        assert volumes == sorted(volumes, reverse=True)
        assert all(entry.symbol.endswith("USDT") for entry in entries)

    def test_the_rank_100_boundary_keeps_1081_and_excludes_1080(self) -> None:
        """101 eligible markets, volumes 1180 down to 1080: member 100 is the 1081."""
        markets, tickers = _eligible(101, 1180)
        entries = rank_universe(markets, tickers)
        assert len(entries) == 100
        assert entries[99].rank == 100
        assert entries[99].quote_volume == 1081.0
        assert entries[99].symbol == "M099USDT"
        symbols = {entry.symbol for entry in entries}
        assert "M100USDT" not in symbols  # volume 1080, the first excluded

    def test_fewer_than_100_eligible_report_the_honest_count(self) -> None:
        markets, tickers = _eligible(3, 30.0)
        entries = rank_universe(markets, tickers)
        assert [entry.symbol for entry in entries] == ["M000USDT", "M001USDT", "M002USDT"]
        assert [entry.quote_volume for entry in entries] == [30.0, 29.0, 28.0]
        assert [entry.rank for entry in entries] == [1, 2, 3]

    def test_the_symbol_is_the_exchange_market_id_without_the_slash(self) -> None:
        entries = rank_universe([_market("BTC/USDT")], [_ticker("BTC/USDT", 10.0)])
        assert entries == [UniverseEntry(rank=1, symbol="BTCUSDT", quote_volume=10.0)]

    def test_the_largest_volume_stablecoin_base_is_excluded(self) -> None:
        markets = [_market("USDC/USDT"), _market("BTC/USDT"), _market("ETH/USDT")]
        tickers = [
            _ticker("USDC/USDT", 9_999_999.0),
            _ticker("BTC/USDT", 500.0),
            _ticker("ETH/USDT", 400.0),
        ]
        entries = rank_universe(markets, tickers)
        assert [entry.symbol for entry in entries] == ["BTCUSDT", "ETHUSDT"]
        assert entries[0].rank == 1
        assert entries[1].rank == 2

    def test_the_fiat_offramp_base_is_excluded(self) -> None:
        entries = rank_universe(
            [_market("EUR/USDT", base="EUR"), _market("BTC/USDT")],
            [_ticker("EUR/USDT", 9_999_999.0), _ticker("BTC/USDT", 1.0)],
        )
        assert [entry.symbol for entry in entries] == ["BTCUSDT"]

    def test_the_leveraged_token_base_is_excluded(self) -> None:
        entries = rank_universe(
            [_market("BTCUP/USDT", base="BTCUP"), _market("BTC/USDT")],
            [_ticker("BTCUP/USDT", 9_999_999.0), _ticker("BTC/USDT", 1.0)],
        )
        assert [entry.symbol for entry in entries] == ["BTCUSDT"]

    def test_a_non_usdt_quote_is_excluded(self) -> None:
        entries = rank_universe(
            [_market("ETH/BTC", quote="BTC"), _market("BTC/USDT")],
            [_ticker("ETH/BTC", 9_999_999.0), _ticker("BTC/USDT", 1.0)],
        )
        assert [entry.symbol for entry in entries] == ["BTCUSDT"]

    def test_an_inactive_market_is_excluded(self) -> None:
        entries = rank_universe(
            [_market("BTC/USDT", active=False), _market("ETH/USDT")],
            [_ticker("BTC/USDT", 9_999_999.0), _ticker("ETH/USDT", 1.0)],
        )
        assert [entry.symbol for entry in entries] == ["ETHUSDT"]

    def test_a_non_spot_market_is_excluded(self) -> None:
        entries = rank_universe(
            [_market("BTC/USDT", spot=False), _market("ETH/USDT")],
            [_ticker("BTC/USDT", 9_999_999.0), _ticker("ETH/USDT", 1.0)],
        )
        assert [entry.symbol for entry in entries] == ["ETHUSDT"]

    @pytest.mark.parametrize("volume", [0, 0.0, None, float("nan"), float("inf")])
    def test_zero_missing_or_non_finite_quote_volume_is_excluded(
        self, volume: float | None
    ) -> None:
        entries = rank_universe(
            [_market("BTC/USDT"), _market("ETH/USDT")],
            [_ticker("BTC/USDT", volume), _ticker("ETH/USDT", 1.0)],
        )
        assert [entry.symbol for entry in entries] == ["ETHUSDT"]

    def test_a_market_without_any_ticker_is_excluded(self) -> None:
        entries = rank_universe(
            [_market("BTC/USDT"), _market("ETH/USDT")], [_ticker("ETH/USDT", 1.0)]
        )
        assert [entry.symbol for entry in entries] == ["ETHUSDT"]

    def test_equal_volumes_tie_break_on_the_symbol_ascending_deterministically(self) -> None:
        markets = [_market(symbol) for symbol in ("ZEC/USDT", "ADA/USDT", "BTC/USDT")]
        tickers = [_ticker(symbol, 100.0) for symbol in ("ZEC/USDT", "ADA/USDT", "BTC/USDT")]
        expected = ["ADAUSDT", "BTCUSDT", "ZECUSDT"]
        for order in permutations(range(3)):
            reordered_markets = [markets[i] for i in order]
            reordered_tickers = [tickers[i] for i in order]
            entries = rank_universe(reordered_markets, reordered_tickers)
            assert [entry.symbol for entry in entries] == expected
            assert [entry.rank for entry in entries] == [1, 2, 3]

    def test_the_non_trading_base_list_is_the_decided_one(self) -> None:
        assert (
            frozenset(
                {
                    "USDC",
                    "TUSD",
                    "FDUSD",
                    "DAI",
                    "USD1",
                    "USDP",
                    "BUSD",
                    "AEUR",
                    "EURI",
                    "EUR",
                    "TRY",
                    "BRL",
                    "ARS",
                    "GBP",
                    "AUD",
                    "JPY",
                    "RUB",
                    "UAH",
                    "ZAR",
                    "MXN",
                    "PLN",
                    "RON",
                    "CZK",
                    "INR",
                    "IDR",
                    "NGN",
                    "COP",
                    "CAD",
                    "CHF",
                }
            )
            == _NON_TRADING_BASES
        )


class _RecordClient:
    """The pooled client seam: one ``load_markets`` + one ``fetch_tickers``."""

    def __init__(self, markets: Mapping[str, Any], tickers: Mapping[str, Any]) -> None:
        self._markets = markets
        self._tickers = tickers
        self.calls: list[str] = []

    async def load_markets(self) -> Mapping[str, Any]:
        self.calls.append("load_markets")
        return self._markets

    async def fetch_tickers(self) -> Mapping[str, Any]:
        self.calls.append("fetch_tickers")
        return self._tickers


class TestComputeUniverse:
    async def test_one_load_markets_plus_one_fetch_tickers_ranked(self) -> None:
        client = _RecordClient(
            {
                "BTC/USDT": _market("BTC/USDT"),
                "USDC/USDT": _market("USDC/USDT"),
                "ETH/BTC": _market("ETH/BTC", quote="BTC"),
            },
            {
                "BTC/USDT": _ticker("BTC/USDT", 100.0),
                "USDC/USDT": _ticker("USDC/USDT", 9_999.0),
                "ETH/BTC": _ticker("ETH/BTC", 9_999.0),
            },
        )
        entries = await compute_universe(client)
        assert entries == [UniverseEntry(rank=1, symbol="BTCUSDT", quote_volume=100.0)]
        assert client.calls == ["load_markets", "fetch_tickers"]


class _StepSleep:
    """An injectable sleep: records durations and parks until the test releases it."""

    def __init__(self) -> None:
        self.slept: list[float] = []
        self._gates: list[asyncio.Event] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)
        gate = asyncio.Event()
        self._gates.append(gate)
        await gate.wait()

    def release(self) -> None:
        if self._gates:
            self._gates.pop(0).set()


class TestUniverseRefresher:
    async def test_the_first_refresh_is_immediate_then_every_interval(self) -> None:
        calls: list[int] = []
        sleep = _StepSleep()

        async def refresh() -> None:
            calls.append(1)

        refresher = UniverseRefresher(refresh, interval_s=86_400.0, sleep=sleep)
        await refresher.start()
        await asyncio.sleep(0)  # the task runs its first refresh and parks in the interval
        assert calls == [1]  # no sleep preceded the first refresh
        assert sleep.slept == [86_400.0]

        sleep.release()
        await asyncio.sleep(0)  # the loop refreshes again, then parks in the interval
        assert calls == [1, 1]
        assert sleep.slept == [86_400.0, 86_400.0]

        await refresher.stop()
        sleep.release()
        await asyncio.sleep(0)
        assert calls == [1, 1]  # cancelled: no third refresh

    async def test_a_failed_refresh_warns_and_retries_hourly(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        attempts: list[int] = []
        sleep = _StepSleep()

        async def refresh() -> None:
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("binance unreachable")

        refresher = UniverseRefresher(refresh, interval_s=86_400.0, retry_s=3_600.0, sleep=sleep)
        with caplog.at_level(logging.WARNING, logger="hydra.data.universe"):
            await refresher.start()
            await asyncio.sleep(0)  # the first refresh fails and parks in the retry
            assert attempts == [1]
            assert sleep.slept == [3_600.0]  # the hourly retry, never the daily interval

            sleep.release()
            await asyncio.sleep(0)  # the retry succeeds and parks in the interval
            assert attempts == [1, 1]
            assert sleep.slept == [3_600.0, 86_400.0]
        assert "Universe refresh failed" in caplog.text
        assert "binance unreachable" in caplog.text

        await refresher.stop()
        sleep.release()
        await asyncio.sleep(0)
        assert attempts == [1, 1]  # cancelled: no third attempt

    async def test_stop_cancels_the_loop_no_further_refresh_runs(self) -> None:
        calls: list[int] = []

        async def refresh() -> None:
            calls.append(1)

        refresher = UniverseRefresher(refresh)  # real asyncio.sleep for the interval
        await refresher.start()
        await asyncio.sleep(0)
        await refresher.stop()
        await asyncio.sleep(0.01)
        assert calls == [1]
        await refresher.stop()  # stopping twice stays a no-op

    async def test_start_twice_is_refused(self) -> None:
        async def refresh() -> None:
            return None

        refresher = UniverseRefresher(refresh)
        await refresher.start()
        try:
            with pytest.raises(RuntimeError, match="called twice"):
                await refresher.start()
        finally:
            await refresher.stop()
