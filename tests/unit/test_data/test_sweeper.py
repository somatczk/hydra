"""The cache sweeper's rails and arithmetic, on an in-memory asyncpg-shaped pool.

The fake pool answers the sweeper's three real queries (the running-session set,
the LRU-ordered ledger, the per-dataset auto recount) plus ``delete_auto_dataset``'s
transactional pair of DELETEs, so every test pins which dataset the budget math
picks — not that SQL runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from prometheus_client import REGISTRY
from structlog.testing import capture_logs

from hydra.core import metrics
from hydra.core.config import DataConfig
from hydra.data.sweeper import DataCacheSweeper

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)


def _metric(name: str) -> float | None:
    return REGISTRY.get_sample_value(name)


@pytest.fixture(autouse=True)
def _fresh_registry() -> AsyncIterator[None]:
    metrics._reset_metrics()
    yield
    metrics._reset_metrics()


# ---------------------------------------------------------------------------
# The fake pool
# ---------------------------------------------------------------------------


@dataclass
class _Dataset:
    """One ledger row and the bars behind it (the ledger's ``rows`` is advisory)."""

    exchange: str
    symbol: str
    timeframe: str
    auto_bars: int
    ledger_rows: int
    last_used_at: datetime
    status: str = "ready"
    last_fetched_at: datetime | None = None
    swept: bool = False
    concurrent_commit: int = 0  # bars a fetch commits between the recount and the delete


@dataclass
class _FakePool:
    datasets: list[_Dataset] = field(default_factory=list)
    running: set[tuple[str, str]] = field(default_factory=set)
    fail_on_delete: bool = False
    hang_counts: bool = False

    def ledger_rows(self) -> list[_Dataset]:
        return [d for d in self.datasets if not d.swept]

    def _find(self, exchange: str, symbol: str, timeframe: str) -> _Dataset:
        for d in self.ledger_rows():
            if (d.exchange, d.symbol, d.timeframe) == (exchange, symbol, timeframe):
                return d
        raise AssertionError(f"no ledger row for {(exchange, symbol, timeframe)}")

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self)


class _FakeAcquire:
    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self._pool)

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakeConn:
    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        pool = self._pool
        if "FROM trading_sessions" in sql:
            return [{"exchange_id": e, "symbol": s} for e, s in sorted(pool.running)]
        if "FROM data_cache_entries" in sql:
            return [
                {
                    "exchange": d.exchange,
                    "symbol": d.symbol,
                    "timeframe": d.timeframe,
                    "first_ts": NOW,
                    "last_ts": NOW,
                    "rows": d.ledger_rows,
                    "status": d.status,
                    "created_at": NOW,
                    "last_fetched_at": d.last_fetched_at or NOW,
                    "last_used_at": d.last_used_at,
                }
                for d in sorted(pool.ledger_rows(), key=lambda d: d.last_used_at)
            ]
        raise AssertionError(f"unexpected fetch: {sql}")

    async def fetchval(self, sql: str, *args: Any) -> int:
        if "count(*)" in sql:
            if self._pool.hang_counts:
                await asyncio.Event().wait()  # a hung DB never answers
            exchange, symbol, timeframe = args
            return self._pool._find(exchange, symbol, timeframe).auto_bars
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def execute(self, sql: str, *args: Any) -> str:
        pool = self._pool
        exchange, symbol, timeframe = args[:3]
        if pool.fail_on_delete:
            raise RuntimeError("delete exploded")
        if "DELETE FROM ts.ohlcv_1m" in sql:
            removed = pool._find(exchange, symbol, timeframe)
            removed.swept = True
            return f"DELETE {removed.auto_bars + removed.concurrent_commit}"
        if "DELETE FROM data_cache_entries" in sql:
            return "DELETE 1"
        raise AssertionError(f"unexpected execute: {sql}")


class _FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _cfg(**overrides: Any) -> DataConfig:
    return DataConfig(fetch_patience_s=100, **overrides)


def _sweeper(pool: _FakePool, cfg: DataConfig) -> DataCacheSweeper:
    return DataCacheSweeper(pool, cfg)


# ---------------------------------------------------------------------------
# The LRU budget arithmetic
# ---------------------------------------------------------------------------


class TestEvictionArithmetic:
    def _stacked_cache(self) -> _FakePool:
        base = NOW
        return _FakePool(
            datasets=[
                _Dataset("binance", "BTCUSDT", "1m", 1000, 1000, base),  # least recently used
                _Dataset("binance", "ETHUSDT", "1m", 2000, 2000, base + timedelta(hours=1)),
                _Dataset("binance", "SOLUSDT", "1m", 3000, 3000, base + timedelta(hours=2)),
            ]
        )

    async def test_exactly_the_two_oldest_go_and_the_counter_counts_their_rows(self) -> None:
        pool = self._stacked_cache()
        # 6000 rows x 205 B = 1_230_000 against a 615_000-byte budget: after the two
        # oldest go the newest fits exactly (3000 x 205 = 615_000).
        cfg = _cfg(cache_max_gb=615_000 / 2**30)

        deleted_rows, deleted_datasets = await _sweeper(pool, cfg).sweep_once(pool)

        assert (deleted_rows, deleted_datasets) == (3000, 2)
        remaining = [(d.symbol, d.auto_bars) for d in pool.ledger_rows()]
        assert remaining == [("SOLUSDT", 3000)]
        assert _metric("hydra_data_sweep_deleted_rows_total") == 3000.0
        assert _metric("hydra_data_cache_rows") == 3000.0
        assert _metric("hydra_data_cache_bytes") == 3000 * 205.0
        assert _metric("hydra_data_cache_pairs") == 1.0
        assert _metric("hydra_data_cache_budget_bytes") == 615_000.0

    async def test_within_budget_nothing_is_deleted(self) -> None:
        pool = self._stacked_cache()
        cfg = _cfg(cache_max_gb=10.0)  # 960k of bars fit easily

        deleted_rows, deleted_datasets = await _sweeper(pool, cfg).sweep_once(pool)

        assert (deleted_rows, deleted_datasets) == (0, 0)
        assert len(pool.ledger_rows()) == 3
        assert _metric("hydra_data_sweep_deleted_rows_total") == 0.0
        assert _metric("hydra_data_cache_pairs") == 3.0

    async def test_zero_budget_deletes_every_eligible_dataset(self) -> None:
        pool = self._stacked_cache()
        cfg = _cfg(cache_max_gb=0.0)

        deleted_rows, deleted_datasets = await _sweeper(pool, cfg).sweep_once(pool)

        assert (deleted_rows, deleted_datasets) == (6000, 3)
        assert pool.ledger_rows() == []
        assert _metric("hydra_data_cache_rows") == 0.0
        assert _metric("hydra_data_cache_bytes") == 0.0
        assert _metric("hydra_data_cache_pairs") == 0.0


class TestRunningSessionGuard:
    async def test_a_running_symbols_datasets_survive_on_every_timeframe(self) -> None:
        base = NOW
        pool = _FakePool(
            datasets=[
                _Dataset("binance", "DOGEUSDT", "1m", 500, 500, base),
                _Dataset("binance", "DOGEUSDT", "1h", 700, 700, base + timedelta(minutes=1)),
                _Dataset("binance", "DOGEUSDT", "1d", 900, 900, base + timedelta(minutes=2)),
                _Dataset("binance", "BTCUSDT", "1m", 600, 600, base + timedelta(minutes=3)),
            ],
            running={("binance", "DOGEUSDT")},
        )
        cfg = _cfg(cache_max_gb=0.0)  # delete every eligible: DOGE must not qualify

        deleted_rows, deleted_datasets = await _sweeper(pool, cfg).sweep_once(pool)

        assert (deleted_rows, deleted_datasets) == (600, 1)
        assert [(d.symbol, d.timeframe) for d in pool.ledger_rows()] == [
            ("DOGEUSDT", "1m"),
            ("DOGEUSDT", "1h"),
            ("DOGEUSDT", "1d"),
        ]

    async def test_the_guard_skips_protected_rows_and_takes_the_next_least_recent(self) -> None:
        base = NOW
        pool = _FakePool(
            datasets=[
                _Dataset("binance", "DOGEUSDT", "1m", 1000, 1000, base),  # oldest, protected
                _Dataset("binance", "BTCUSDT", "1m", 3000, 3000, base + timedelta(hours=1)),
                _Dataset("binance", "SOLUSDT", "1m", 100, 100, base + timedelta(hours=2)),
            ],
            running={("binance", "DOGEUSDT")},
        )
        cfg = _cfg(cache_max_gb=500_000 / 2**30)

        deleted_rows, deleted_datasets = await _sweeper(pool, cfg).sweep_once(pool)

        # DOGE is the oldest but protected: BTC, the next least recent, goes and fits
        assert (deleted_rows, deleted_datasets) == (3000, 1)
        assert {d.symbol for d in pool.ledger_rows()} == {"DOGEUSDT", "SOLUSDT"}


class TestFetchingRails:
    async def test_a_fresh_fetching_dataset_is_skipped(self) -> None:
        base = NOW
        fetching = _Dataset(
            "binance",
            "ETHUSDT",
            "1m",
            2000,
            2000,
            base,
            status="fetching",
            last_fetched_at=datetime.now(UTC) - timedelta(seconds=30),
        )
        pool = _FakePool(
            datasets=[
                _Dataset("binance", "BTCUSDT", "1m", 1000, 1000, base + timedelta(hours=1)),
                fetching,
            ]
        )
        cfg = _cfg(cache_max_gb=0.0)

        deleted_rows, deleted_datasets = await _sweeper(pool, cfg).sweep_once(pool)

        assert (deleted_rows, deleted_datasets) == (1000, 1)
        assert [d.symbol for d in pool.ledger_rows()] == ["ETHUSDT"]

    async def test_a_stale_fetching_dataset_is_a_crashed_fetch_and_gets_swept(self) -> None:
        base = NOW
        stale = _Dataset(
            "binance",
            "ETHUSDT",
            "1m",
            2000,
            2000,
            base,
            status="fetching",
            last_fetched_at=datetime.now(UTC) - timedelta(seconds=201),  # past 2x patience
        )
        fresh = _Dataset(
            "binance",
            "SOLUSDT",
            "1m",
            1000,
            1000,
            base + timedelta(hours=1),
            status="fetching",
            last_fetched_at=datetime.now(UTC),
        )
        pool = _FakePool(datasets=[stale, fresh])
        cfg = _cfg(cache_max_gb=0.0)

        deleted_rows, deleted_datasets = await _sweeper(pool, cfg).sweep_once(pool)

        assert (deleted_rows, deleted_datasets) == (2000, 1)
        assert [d.symbol for d in pool.ledger_rows()] == ["SOLUSDT"]


class TestRecompute:
    async def test_the_eviction_input_is_the_recounted_bars_not_the_ledger(self) -> None:
        pool = _FakePool(
            datasets=[
                # the ledger claims 100 rows; the bars hold 50
                _Dataset("binance", "BTCUSDT", "1m", 50, 100, NOW),
            ]
        )
        # 100 x 205 would breach 10_250 and evict; the recounted 50 x 205 = 10_250
        # fits exactly and stays.
        cfg = _cfg(cache_max_gb=10_250 / 2**30)

        deleted_rows, deleted_datasets = await _sweeper(pool, cfg).sweep_once(pool)

        assert (deleted_rows, deleted_datasets) == (0, 0)
        assert _metric("hydra_data_cache_rows") == 50.0
        assert _metric("hydra_data_cache_bytes") == 50 * 205.0


class TestConcurrentCommitRace:
    async def test_an_overshooting_delete_never_negates_the_accounting(self) -> None:
        """A fetch commits 15 bars between the recount (10) and the delete (25).

        The deletes already committed stand; the totals clamp at zero so the
        sweep is never counted as failed by its own metric guard, and the
        deleted counter still reports every row that went.
        """
        racing = _Dataset("binance", "BTCUSDT", "1m", 10, 10, NOW, concurrent_commit=15)
        pool = _FakePool(datasets=[racing])
        cfg = _cfg(cache_max_gb=0.0)

        deleted_rows, deleted_datasets = await _sweeper(pool, cfg).sweep_once(pool)

        assert (deleted_rows, deleted_datasets) == (25, 1)
        assert pool.ledger_rows() == []
        assert _metric("hydra_data_sweep_deleted_rows_total") == 25.0
        assert _metric("hydra_data_cache_rows") == 0.0
        assert _metric("hydra_data_cache_bytes") == 0.0
        assert _metric("hydra_data_cache_pairs") == 0.0
        assert _metric("hydra_data_sweep_errors_total") == 0.0  # pre-zeroed by the contract


class TestOverBudgetNothingEligible:
    async def test_over_budget_with_nothing_eligible_warns_writes_gauges_and_exits(self) -> None:
        """Every dataset protected or mid-fetch, budget exceeded: warn, exit, no spin."""
        protected = _Dataset("binance", "DOGEUSDT", "1m", 1000, 1000, NOW)
        mid_fetch = _Dataset(
            "binance",
            "BTCUSDT",
            "1m",
            2000,
            2000,
            NOW + timedelta(hours=1),
            status="fetching",
            last_fetched_at=datetime.now(UTC),
        )
        pool = _FakePool(datasets=[protected, mid_fetch], running={("binance", "DOGEUSDT")})
        # 3000 rows x 205 B = 615_000 bytes against a 10_000-byte budget
        cfg = _cfg(cache_max_gb=10_000 / 2**30)

        with capture_logs() as logs:
            assert await _sweeper(pool, cfg).sweep_once(pool) == (0, 0)

        assert pool.ledger_rows() == [protected, mid_fetch]
        assert logs == [
            {
                "event": "data cache still over budget with nothing eligible",
                "log_level": "warning",
                "estimated_bytes": 3000 * 205,
                "budget_bytes": 10_000,
            }
        ]
        assert _metric("hydra_data_cache_rows") == 3000.0
        assert _metric("hydra_data_cache_bytes") == 3000 * 205.0
        assert _metric("hydra_data_cache_pairs") == 2.0
        assert _metric("hydra_data_sweep_deleted_rows_total") == 0.0


class TestFailuresAndLifecycle:
    async def _until(self, predicate: Any, timeout_s: float = 5.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while not predicate():
            if asyncio.get_running_loop().time() > deadline:
                pytest.fail("condition never became true")
            await asyncio.sleep(0.01)

    async def test_a_raising_delete_is_counted_swallowed_and_retried(self) -> None:
        pool = _FakePool(datasets=[_Dataset("binance", "BTCUSDT", "1m", 1000, 1000, NOW)])
        pool.fail_on_delete = True
        sweeper = _sweeper(pool, _cfg(cache_max_gb=0.0, sweep_interval_s=1))

        await sweeper.start()
        await self._until(lambda: _metric("hydra_data_sweep_errors_total") == 1.0)
        task = sweeper._task
        assert task is not None and not task.done()
        assert _metric("hydra_data_sweep_last_timestamp_seconds") is None  # no cycle finished

        pool.fail_on_delete = False
        await self._until(lambda: not pool.ledger_rows())
        assert _metric("hydra_data_sweep_errors_total") == 1.0  # the retry was healthy
        assert _metric("hydra_data_sweep_last_timestamp_seconds") > 0
        await sweeper.stop()

    async def test_a_hung_count_cannot_wedge_the_loop(self) -> None:
        pool = _FakePool(datasets=[_Dataset("binance", "BTCUSDT", "1m", 1000, 1000, NOW)])
        pool.hang_counts = True
        sweeper = _sweeper(pool, _cfg(cache_max_gb=0.0, sweep_interval_s=1))

        await sweeper.start()
        # the cycle hangs on the recount; wait_for cancels it at one interval
        await self._until(lambda: _metric("hydra_data_sweep_errors_total") == 1.0, timeout_s=3)

        task = sweeper._task
        assert task is not None and not task.done()

        pool.hang_counts = False
        await self._until(lambda: not pool.ledger_rows(), timeout_s=3)
        assert _metric("hydra_data_sweep_errors_total") == 1.0
        await sweeper.stop()

    async def test_no_pool_start_is_a_noop_and_stop_is_safe(self) -> None:
        sweeper = DataCacheSweeper(None, DataConfig())

        await sweeper.start()  # dev: no database, nothing to guard or delete
        assert sweeper._task is None
        await sweeper.stop()

        pool = _FakePool(datasets=[_Dataset("binance", "BTCUSDT", "1m", 10, 10, NOW)])
        assert await sweeper.sweep_once(pool) == (0, 0)  # default 10 GB: nothing to evict
        assert len(pool.ledger_rows()) == 1

    async def test_stop_cancels_a_hanging_cycle_and_returns(self) -> None:
        pool = _FakePool(datasets=[_Dataset("binance", "BTCUSDT", "1m", 1000, 1000, NOW)])
        pool.hang_counts = True
        sweeper = _sweeper(pool, _cfg(cache_max_gb=0.0, sweep_interval_s=3600))

        await sweeper.start()
        assert sweeper._task is not None
        await sweeper.stop()
        assert sweeper._task is None
