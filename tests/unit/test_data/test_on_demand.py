"""Tests for hydra.data.on_demand — the budgeted, patient, serialized fetch engine.

No network: the ccxt client is ``FakeRestExchange``, the repository is an in-memory
seam with the real hole/upsert/ledger semantics, and the clock and sleep are
injected, so every paced wait and patience deadline is asserted exactly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import ccxt
import pytest

from hydra.core.config import DataConfig
from hydra.core.types import OHLCV, ExchangeId, Timeframe
from hydra.data.backfill import MAX_PAGE_SIZE, GapRange
from hydra.data.bars import timeframe_seconds
from hydra.data.errors import DataFetchError, NoMarketDataError
from hydra.data.on_demand import OnDemandFetchService
from tests.fixtures.binance_kline import FakeRestExchange, binance_markets

T0 = datetime(2026, 3, 1, tzinfo=UTC)
_LATER = datetime(2030, 1, 1, tzinfo=UTC)


def _ts(minute: int) -> datetime:
    """The ``minute``-th 1m bar open after T0."""
    return T0 + timedelta(minutes=minute)


def _row(minute: int, close: float = 42100.0) -> list[float]:
    return [_ts(minute).timestamp() * 1000, 42000.0, 42500.0, 41800.0, close, 100.0]


def _row_ts(ts: datetime, close: float = 42100.0) -> list[float]:
    return [ts.timestamp() * 1000, 42000.0, 42500.0, 41800.0, close, 100.0]


def _bar(minute: int, close: str = "42100.0") -> OHLCV:
    price = Decimal(close)
    return OHLCV(
        open=Decimal("42000"),
        high=Decimal("42500"),
        low=Decimal("41800"),
        close=price,
        volume=Decimal("100"),
        timestamp=_ts(minute),
    )


# ---------------------------------------------------------------------------
# Injectable clock and sleep
# ---------------------------------------------------------------------------


class FakeClock:
    """Callable monotonic clock; sleeping advances it by the slept amount."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class RecordingSleep:
    """Injectable sleep that records every wait and advances the fake clock."""

    def __init__(self, clock: FakeClock) -> None:
        self.calls: list[float] = []
        self._clock = clock

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self._clock.now += seconds


# ---------------------------------------------------------------------------
# Repository seam: real hole/upsert/source/ledger semantics, in memory
# ---------------------------------------------------------------------------

_DatasetKey = tuple[str, str, Timeframe]
_RowKey = tuple[str, str, Timeframe, datetime]


class FakeRepo:
    """In-memory ``MarketDataRepository`` with the storage layer's real semantics.

    Holes are computed over the bar grid like ``missing_ranges``; an upsert inserts
    new rows, corrects changed values and never rewrites an existing row's source;
    the cache ledger is credited only for inserted rows, exactly like the SQL.
    """

    def __init__(self, bars: Iterable[tuple[str, str, Timeframe, OHLCV]] = ()) -> None:
        self._values: dict[_RowKey, OHLCV] = {}
        self._sources: dict[_RowKey, str] = {}
        for exchange_id, symbol, timeframe, ohlcv in bars:
            key = (exchange_id, symbol, timeframe, ohlcv.timestamp)
            self._values[key] = ohlcv
            self._sources[key] = "feed"
        self.upserts: list[tuple[list[tuple[str, str, Timeframe, OHLCV]], str]] = []
        self.statuses: list[tuple[str, datetime, datetime]] = []
        self.ledger_credits: dict[_DatasetKey, int] = {}

    def stored_ts(
        self,
        timeframe: Timeframe = Timeframe.M1,
        exchange_id: str = "binance",
        symbol: str = "BTCUSDT",
    ) -> list[datetime]:
        return sorted(
            ts for e, s, tf, ts in self._values if (e, s, tf) == (exchange_id, symbol, timeframe)
        )

    def source_of(self, ts: datetime) -> str | None:
        for key, source in self._sources.items():
            if key[2] is Timeframe.M1 and key[3] == ts:
                return source
        return None

    async def missing_ranges(
        self,
        exchange_id: str,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[tuple[datetime, datetime]]:
        step = timedelta(seconds=timeframe_seconds(timeframe))
        prefix = (exchange_id, symbol, timeframe)
        holes: list[tuple[datetime, datetime]] = []
        cur = start
        while cur <= end:
            if (*prefix, cur) not in self._values:
                gap_start = cur
                while cur <= end and (*prefix, cur) not in self._values:
                    cur += step
                holes.append((gap_start, cur - step))
            else:
                cur += step
        return holes

    async def upsert_closed_bars(
        self,
        bars: list[tuple[str, str, Timeframe, OHLCV]],
        source: str = "feed",
    ) -> tuple[int, int]:
        self.upserts.append((list(bars), source))
        inserted = 0
        updated = 0
        credits: dict[_DatasetKey, int] = {}
        for exchange_id, symbol, timeframe, ohlcv in bars:
            key = (exchange_id, symbol, timeframe, ohlcv.timestamp)
            existing = self._values.get(key)
            if existing is None:
                self._values[key] = ohlcv
                self._sources[key] = source
                inserted += 1
                dataset = (exchange_id, symbol, timeframe)
                credits[dataset] = credits.get(dataset, 0) + 1
            elif existing != ohlcv:
                self._values[key] = ohlcv
                updated += 1
        for dataset, rows in credits.items():
            self.ledger_credits[dataset] = self.ledger_credits.get(dataset, 0) + rows
        return (inserted, updated)

    async def set_cache_status(
        self,
        exchange_id: str,
        symbol: str,
        timeframe: Timeframe,
        status: str,
        first_ts: datetime,
        last_ts: datetime,
    ) -> None:
        self.statuses.append((status, first_ts, last_ts))

    async def get_bar_range(
        self,
        exchange_id: str,
        symbol: str,
        timeframe: Timeframe,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[datetime, datetime, int] | None:
        stamps = sorted(
            ts
            for e, s, tf, ts in self._values
            if (e, s, tf) == (exchange_id, symbol, timeframe)
            and (start is None or ts >= start)
            and (end is None or ts <= end)
        )
        if not stamps:
            return None
        return (stamps[0], stamps[-1], len(stamps))

    async def store_bars(self, bars: list[tuple[str, str, Timeframe, OHLCV]]) -> int:
        raise AssertionError("the on-demand fetch path must write through upsert_closed_bars")


def _stored(minutes: range | list[int]) -> list[tuple[str, str, Timeframe, OHLCV]]:
    return [("binance", "BTCUSDT", Timeframe.M1, _bar(m)) for m in minutes]


# ---------------------------------------------------------------------------
# Engine construction
# ---------------------------------------------------------------------------


def _engine(
    exchange: FakeRestExchange,
    *,
    weight_per_min: int = 900,
    patience_s: int = 900,
    max_bars: int = 2_000_000,
    page_size: int = MAX_PAGE_SIZE,
    now: datetime | None = _LATER,
) -> tuple[OnDemandFetchService, RecordingSleep]:
    the_clock = FakeClock()
    the_sleep = RecordingSleep(the_clock)

    async def client_factory(exchange_id: ExchangeId) -> FakeRestExchange:
        return exchange

    service = OnDemandFetchService(
        client_factory,
        weight_per_min=weight_per_min,
        patience_s=patience_s,
        max_bars=max_bars,
        page_size=page_size,
        clock=the_clock,
        sleep=the_sleep,
        now=now,
    )
    return service, the_sleep


_RANGE_START = _ts(0)
_RANGE_END = _ts(9)


def _ensure(
    service: OnDemandFetchService,
    repo: FakeRepo,
    *,
    start: datetime = _RANGE_START,
    end: datetime = _RANGE_END,
    force: bool = False,
    on_progress: Any = None,
):
    return service.ensure_available(
        repo,
        "binance",
        "BTCUSDT",
        Timeframe.M1,
        start,
        end,
        force=force,
        on_progress=on_progress,
    )


def _ms(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Gap-only fetching
# ---------------------------------------------------------------------------


class TestGapOnly:
    async def test_fetches_exactly_the_missing_range(self) -> None:
        """d5,d6 of d1..d10 are missing: one fetch_ohlcv since=d5, stored source='auto'."""
        repo = FakeRepo(_stored([0, 1, 2, 3, 6, 7, 8, 9]))
        exchange = FakeRestExchange([_row(m) for m in range(10)])
        service, _ = _engine(exchange)
        progress: list[dict[str, Any]] = []

        report = await _ensure(service, repo, on_progress=progress.append)

        assert exchange.calls == [("BTCUSDT", "1m", _ms(_ts(4)), 1000)]
        assert repo.upserts == [
            (
                [
                    ("binance", "BTCUSDT", Timeframe.M1, _bar(4)),
                    ("binance", "BTCUSDT", Timeframe.M1, _bar(5)),
                ],
                "auto",
            )
        ]
        assert repo.stored_ts() == [_ts(m) for m in range(10)]
        assert repo.ledger_credits == {("binance", "BTCUSDT", Timeframe.M1): 2}
        assert (report.bars_fetched, report.weight_spent, report.ranges_fetched) == (2, 2, 1)
        assert report.gap_report == []
        assert report.elapsed_ms == 0
        assert repo.statuses == [("fetching", _ts(0), _ts(9)), ("ready", _ts(0), _ts(9))]
        ranges = [[_ts(4).isoformat(), _ts(5).isoformat()]]
        assert progress == [
            {
                "state": "fetching",
                "ranges": ranges,
                "bars_fetched": 0,
                "weight_spent": 0,
                "elapsed_ms": 0,
            },
            {
                "state": "fetching",
                "ranges": ranges,
                "bars_fetched": 2,
                "weight_spent": 2,
                "elapsed_ms": 0,
            },
            {
                "state": "fetched",
                "ranges": ranges,
                "bars_fetched": 2,
                "weight_spent": 2,
                "elapsed_ms": 0,
            },
        ]

    async def test_fully_stored_range_fetches_and_writes_nothing(self) -> None:
        """A warm dataset: no exchange call, no upsert, no ledger-status write."""
        repo = FakeRepo(_stored(range(10)))
        exchange = FakeRestExchange([_row(m) for m in range(10)])
        service, _ = _engine(exchange)

        report = await _ensure(service, repo)

        assert exchange.calls == []
        assert repo.upserts == []
        assert repo.statuses == []
        assert repo.ledger_credits == {}
        assert (report.bars_fetched, report.weight_spent, report.ranges_fetched) == (0, 0, 0)
        assert report.gap_report == []


# ---------------------------------------------------------------------------
# Budget pacing
# ---------------------------------------------------------------------------


class TestBudgetPacing:
    async def test_pages_wait_exactly_to_the_window_boundary(self) -> None:
        """5 pages x 2 weight vs a 6/min bucket: pages 1-3 immediate, page 4 waits 60.0s."""
        repo = FakeRepo()
        exchange = FakeRestExchange([_row(m) for m in range(10)])
        service, sleep = _engine(exchange, weight_per_min=6, page_size=2)

        report = await _ensure(service, repo)

        assert sleep.calls == [60.0]
        assert [(c[2], c[3]) for c in exchange.calls] == [(_ms(_ts(m)), 2) for m in (0, 2, 4, 6, 8)]
        assert (report.bars_fetched, report.weight_spent) == (10, 10)
        assert repo.stored_ts() == [_ts(m) for m in range(10)]

    async def test_exact_window_fill_has_no_dead_tail_wait(self) -> None:
        """3 pages fill a 6/min window exactly: no wait for a page nobody will request."""
        repo = FakeRepo()
        exchange = FakeRestExchange([_row(m) for m in range(6)])
        service, sleep = _engine(exchange, weight_per_min=6, page_size=2)

        report = await _ensure(service, repo, start=_ts(0), end=_ts(5))

        assert sleep.calls == []
        assert len(exchange.calls) == 3
        assert (report.bars_fetched, report.weight_spent) == (6, 6)

    async def test_load_markets_weight_charged_once_per_client(self) -> None:
        """A cold client pays the 20-weight load_markets once; the next gap pays 2 only."""

        class _LazyMarkets(FakeRestExchange):
            def __init__(self, rows: list[list[float]]) -> None:
                super().__init__(rows)
                self.markets = None
                self.load_markets_calls = 0

            async def load_markets(self) -> dict[str, Any]:
                self.load_markets_calls += 1
                self.markets = binance_markets()
                return self.markets

        repo = FakeRepo()
        exchange = _LazyMarkets([_row(m) for m in range(5)])
        service, _ = _engine(exchange)

        first = await _ensure(service, repo, start=_ts(0), end=_ts(2))
        second = await _ensure(service, repo, start=_ts(3), end=_ts(4))

        assert (first.bars_fetched, first.weight_spent) == (3, 22)
        assert (second.bars_fetched, second.weight_spent) == (2, 2)
        assert exchange.load_markets_calls == 1


# ---------------------------------------------------------------------------
# Serialization: the per-dataset single-writer lock
# ---------------------------------------------------------------------------


class _YieldingExchange(FakeRestExchange):
    """Yields to the loop per fetch so a concurrent caller truly contends the lock."""

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: int | None = None,
        limit: int | None = None,
    ) -> list[list[Any]]:
        await asyncio.sleep(0)
        return await super().fetch_ohlcv(symbol, timeframe, since=since, limit=limit)


class TestSerialization:
    async def test_concurrent_same_dataset_calls_serialize(self) -> None:
        """The second caller waits for the first's writes; one fetch, no interleave."""
        repo = FakeRepo()
        exchange = _YieldingExchange([_row(m) for m in range(5)])
        service, _ = _engine(exchange, page_size=1)
        events: list[str] = []

        async def run(tag: str):
            def on_progress(progress: dict[str, Any]) -> None:
                events.append(f"{tag}:{progress['state']}:{progress['bars_fetched']}")

            return await _ensure(service, repo, start=_ts(0), end=_ts(4), on_progress=on_progress)

        first, second = await asyncio.gather(run("A"), run("B"))

        assert events == [
            "A:fetching:0",
            "A:fetching:1",
            "A:fetching:2",
            "A:fetching:3",
            "A:fetching:4",
            "A:fetching:5",
            "A:fetched:5",
            "B:fetched:0",
        ]
        assert len(exchange.calls) == 5
        assert len(repo.upserts) == 5
        assert all(source == "auto" for _, source in repo.upserts)
        assert repo.statuses == [("fetching", _ts(0), _ts(4)), ("ready", _ts(0), _ts(4))]
        assert (first.bars_fetched, first.weight_spent) == (5, 10)
        assert (second.bars_fetched, second.weight_spent, second.ranges_fetched) == (0, 0, 0)


# ---------------------------------------------------------------------------
# Patience
# ---------------------------------------------------------------------------


class TestPatience:
    async def test_rate_limit_storm_past_patience_raises_data_fetch_error(self) -> None:
        """Always RateLimitExceeded with patience 5s: typed failure, exact weight spent."""
        repo = FakeRepo()
        exchange = FakeRestExchange([_row(m) for m in range(5)])
        exchange.errors = [ccxt.RateLimitExceeded("429")] * 100
        service, _ = _engine(exchange, patience_s=5)

        with pytest.raises(DataFetchError) as excinfo:
            await _ensure(service, repo)

        exc = excinfo.value
        assert exc.reason == "rate_limited"
        assert exc.weight_spent == 2
        assert exc.elapsed_s >= 30.0
        assert exc.remaining_ranges == [GapRange(start=_ts(0), end=_ts(9))]
        assert isinstance(exc.__cause__, ccxt.RateLimitExceeded)
        assert len(exchange.calls) == 6
        assert repo.upserts == []
        assert repo.statuses == [("fetching", _ts(0), _ts(9)), ("ready", _ts(0), _ts(9))]

    async def test_non_network_error_propagates_unchanged(self) -> None:
        """A non-retryable exchange error is never swallowed or remapped."""
        repo = FakeRepo()
        exchange = FakeRestExchange([_row(m) for m in range(5)])
        exchange.errors = [ccxt.BadSymbol("nope")]
        service, _ = _engine(exchange)

        with pytest.raises(ccxt.BadSymbol):
            await _ensure(service, repo)

        assert repo.upserts == []
        assert repo.statuses == [("fetching", _ts(0), _ts(9)), ("ready", _ts(0), _ts(9))]


# ---------------------------------------------------------------------------
# Availability vocabulary
# ---------------------------------------------------------------------------


class TestAvailability:
    async def test_exchange_has_none_raises_no_market_data_with_available_range(self) -> None:
        """Empty pages over the whole requested range: the P0-B error, filled honestly."""
        repo = FakeRepo(_stored(range(4)))
        exchange = FakeRestExchange([])
        service, _ = _engine(exchange)

        with pytest.raises(NoMarketDataError) as excinfo:
            await _ensure(service, repo, start=_ts(4), end=_ts(9))

        exc = excinfo.value
        assert (exc.requested_start, exc.requested_end) == (_ts(4), _ts(9))
        assert (exc.available_first_ts, exc.available_last_ts) == (_ts(0), _ts(3))
        assert exc.message == (
            "No market data for binance BTCUSDT 1m between "
            f"{_ts(4).isoformat()} and {_ts(9).isoformat()}; "
            f"available: {_ts(0).isoformat()} to {_ts(3).isoformat()}"
        )
        assert exc.to_detail()["state"] == "no_data"
        assert exchange.calls == [("BTCUSDT", "1m", _ms(_ts(4)), 1000)]
        assert repo.upserts == []
        assert repo.statuses == [("fetching", _ts(4), _ts(9)), ("ready", _ts(4), _ts(9))]

    async def test_accepted_gaps_are_reported_not_raised(self) -> None:
        """d5 fetched, d6 genuinely missing: gap_report=['d6'], the run proceeds."""
        repo = FakeRepo(_stored([0, 1, 2, 3, 6, 7, 8, 9]))
        exchange = FakeRestExchange([_row(m) for m in [0, 1, 2, 3, 4, 6, 7, 8, 9]])
        service, _ = _engine(exchange)

        report = await _ensure(service, repo)

        assert report.gap_report == [GapRange(start=_ts(5), end=_ts(5))]
        assert report.bars_fetched == 1
        assert repo.stored_ts() == [_ts(m) for m in [0, 1, 2, 3, 4, 6, 7, 8, 9]]
        assert len(exchange.calls) == 1

    async def test_too_much_data_fails_fast_before_any_egress(self) -> None:
        """10 missing bars vs a 5-bar cap: typed refusal, zero weight, no exchange call."""
        repo = FakeRepo()
        exchange = FakeRestExchange([_row(m) for m in range(10)])
        service, _ = _engine(exchange, max_bars=5)

        with pytest.raises(DataFetchError) as excinfo:
            await _ensure(service, repo)

        exc = excinfo.value
        assert exc.reason == "too_much_data"
        assert exc.weight_spent == 0
        assert exc.remaining_ranges == [GapRange(start=_ts(0), end=_ts(9))]
        assert exchange.calls == []
        assert repo.upserts == []
        assert repo.statuses == []

    async def test_force_refetches_the_whole_range_as_one_gap(self) -> None:
        """force=True over stored feed rows: one range, values correct, attribution kept."""
        repo = FakeRepo(_stored(range(10)))
        exchange = FakeRestExchange([_row(m) for m in range(10)])
        service, _ = _engine(exchange)

        report = await _ensure(service, repo, force=True)

        assert len(exchange.calls) == 1
        assert exchange.calls[0][2] == _ms(_ts(0))
        assert repo.upserts == [
            (
                [("binance", "BTCUSDT", Timeframe.M1, _bar(m)) for m in range(10)],
                "auto",
            )
        ]
        assert repo.ledger_credits == {}
        assert all(repo.source_of(_ts(m)) == "feed" for m in range(10))
        assert (report.bars_fetched, report.weight_spent, report.ranges_fetched) == (10, 2, 1)
        assert report.gap_report == []
        assert repo.statuses == [("fetching", _ts(0), _ts(9)), ("ready", _ts(0), _ts(9))]

    async def test_forming_candle_is_never_stored(self) -> None:
        """With now mid-candle, the open candle is served by the exchange but never stored."""
        t = datetime(2026, 2, 1, 10, tzinfo=UTC)
        repo = FakeRepo()
        rows = [_row_ts(t - timedelta(hours=2)), _row_ts(t - timedelta(hours=1)), _row_ts(t)]
        exchange = FakeRestExchange(rows)
        service, _ = _engine(exchange, now=t + timedelta(minutes=30))

        report = await service.ensure_available(
            repo,
            "binance",
            "BTCUSDT",
            Timeframe.H1,
            t - timedelta(hours=2),
            t + timedelta(hours=1),
        )

        assert repo.stored_ts(Timeframe.H1) == [t - timedelta(hours=2), t - timedelta(hours=1)]
        assert report.bars_fetched == 2
        assert report.gap_report == [GapRange(start=t, end=t + timedelta(hours=1))]
        assert len(exchange.calls) == 1


# ---------------------------------------------------------------------------
# Construction from the data config section
# ---------------------------------------------------------------------------


class TestFromConfig:
    async def test_patience_knob_threads_from_the_data_section(self) -> None:
        repo = FakeRepo()
        exchange = FakeRestExchange([_row(m) for m in range(5)])
        exchange.errors = [ccxt.RateLimitExceeded("429")] * 100
        clock = FakeClock()
        sleep = RecordingSleep(clock)

        async def client_factory(exchange_id: ExchangeId) -> FakeRestExchange:
            return exchange

        service = OnDemandFetchService.from_config(
            DataConfig(fetch_patience_s=5), client_factory, clock=clock, sleep=sleep
        )

        with pytest.raises(DataFetchError) as excinfo:
            await _ensure(service, repo)

        assert excinfo.value.reason == "rate_limited"
        assert excinfo.value.weight_spent == 2

    async def test_max_bars_knob_threads_from_the_data_section(self) -> None:
        repo = FakeRepo()
        exchange = FakeRestExchange([_row(m) for m in range(10)])

        async def client_factory(exchange_id: ExchangeId) -> FakeRestExchange:
            return exchange

        service = OnDemandFetchService.from_config(DataConfig(fetch_max_bars=5), client_factory)

        with pytest.raises(DataFetchError) as excinfo:
            await _ensure(service, repo)

        assert excinfo.value.reason == "too_much_data"
        assert exchange.calls == []
