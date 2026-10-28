"""The funding-rate writer: parse, pagination, pacing, resume and the refresher loop (P4-B).

Every exchange call goes through a ccxt-shaped fake client (no egress) and every
pool through a fake that mirrors the insert-if-absent contract; the real SQL is
proven on real TimescaleDB by ``tests/integration/test_funding_pg.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from hydra.data.funding import (
    EXCHANGE_ID,
    FundingRate,
    FundingRefresher,
    FundingWriteReport,
    ensure_funding_rates,
    fetch_funding_page,
    parse_funding_rate_history,
    running_perp_symbols,
    upsert_funding_rates,
)
from hydra.data.on_demand import WeightBudget

T0 = datetime(2026, 1, 1, 0, tzinfo=UTC)
H8 = timedelta(hours=8)
H1MS = timedelta(milliseconds=1)

RATES = ("0.0001", "-0.00005", "0.0002", "0.0001", "0.0003")
TIMES = [T0 + i * H8 for i in range(len(RATES))]
MS = [int(t.timestamp() * 1000) for t in TIMES]


def _entry(timestamp: datetime, rate: str) -> dict[str, Any]:
    """A ccxt ``fetch_funding_rate_history`` entry, as binanceusdm parses them."""
    return {
        "info": {
            "symbol": "BTCUSDT",
            "fundingRate": rate,
            "fundingTime": int(timestamp.timestamp() * 1000),
        },
        "symbol": "BTC/USDT:USDT",
        "fundingRate": float(rate),
        "timestamp": int(timestamp.timestamp() * 1000),
        "datetime": timestamp.isoformat(),
    }


ENTRIES: list[dict[str, Any]] = [_entry(ts, rate) for ts, rate in zip(TIMES, RATES, strict=True)]


class _FakeClient:
    """ccxt-shaped funding history: pages oldest-first, clamped by since/until."""

    def __init__(self, entries: Sequence[Mapping[str, Any]] = ENTRIES) -> None:
        self.entries = sorted(entries, key=lambda e: int(e["timestamp"]))
        self.calls: list[dict[str, Any]] = []
        self.closed = 0

    async def fetch_funding_rate_history(
        self,
        symbol: str,
        since: int | None = None,
        limit: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        until = (params or {}).get("until")
        self.calls.append({"symbol": symbol, "since": since, "limit": limit, "until": until})
        page = [
            entry
            for entry in self.entries
            if (since is None or int(entry["timestamp"]) >= since)
            and (until is None or int(entry["timestamp"]) <= until)
        ]
        return page[: limit or len(page)]

    async def close(self) -> None:
        self.closed += 1


class _FailingSymbolClient(_FakeClient):
    """Fails one symbol's history call; records every symbol served."""

    def __init__(self, fail_symbol: str) -> None:
        super().__init__()
        self.fail_symbol = fail_symbol
        self.symbols: list[str] = []

    async def fetch_funding_rate_history(
        self,
        symbol: str,
        since: int | None = None,
        limit: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self.symbols.append(symbol)
        if symbol == self.fail_symbol:
            raise RuntimeError("fapi unreachable")
        return await super().fetch_funding_rate_history(symbol, since, limit, params)


class _FakePool:
    """A recording pool mirroring the writer's queries and insert-if-absent contract.

    ``locked=False`` emulates another process holding the per-symbol advisory lock.
    """

    def __init__(
        self,
        running: Sequence[str] = (),
        stored: Sequence[FundingRate] = (),
        *,
        locked: bool = True,
    ) -> None:
        self.running = list(running)
        self.stored = {(row.symbol, row.timestamp): row for row in stored}
        self.locked = locked

    def acquire(self) -> _FakeConn:
        return _FakeConn(self)


class _FakeConn:
    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def transaction(self) -> _FakeConn:
        return self

    async def fetch(self, sql: str, *args: Any) -> Any:
        if "FROM trading_sessions" in sql:
            assert args == (EXCHANGE_ID,)
            return [{"symbol": symbol} for symbol in self._pool.running]
        if "INSERT INTO ts.funding_rates" in sql:
            symbols, rates, next_times, timestamps = args[1], args[2], args[3], args[4]
            inserted: list[int] = []
            for symbol, rate, next_time, timestamp in zip(
                symbols, rates, next_times, timestamps, strict=True
            ):
                if (symbol, timestamp) in self._pool.stored:
                    continue
                self._pool.stored[(symbol, timestamp)] = FundingRate(
                    symbol, Decimal(str(rate)), timestamp, next_time
                )
                inserted.append(1)
            return inserted
        raise AssertionError(f"unexpected fetch: {sql}")

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "pg_try_advisory_xact_lock" in sql:
            return self._pool.locked
        if "SELECT max(timestamp)" in sql:
            timestamps = [ts for (symbol, ts) in self._pool.stored if symbol == args[1]]
            return max(timestamps) if timestamps else None
        raise AssertionError(f"unexpected fetchval: {sql}")


class _LockSpyPool(_FakePool):
    """Records every advisory-lock key the writer asks for; *contested* keys are denied."""

    def __init__(
        self,
        *args: Any,
        contested: frozenset[str] = frozenset(),
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.lock_keys: list[str] = []
        self.contested = contested

    def acquire(self) -> _FakeConn:
        return _LockSpyConn(self)


class _LockSpyConn(_FakeConn):
    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "pg_try_advisory_xact_lock" in sql:
            self._pool.lock_keys.append(str(args[0]))
            if str(args[0]) in self._pool.contested:
                return False
        return await super().fetchval(sql, *args)


class _Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _Sleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class _BlockingSleep(_Sleep):
    """Records the wait, then blocks until the test releases it (a real sleep)."""

    def __init__(self) -> None:
        super().__init__()
        self._gate = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        await self._gate.wait()


class TestParse:
    def test_ccxt_entries_become_rows_for_the_requested_symbol(self) -> None:
        rows = parse_funding_rate_history(ENTRIES[:3], symbol="BTCUSDT")
        assert rows == [
            FundingRate("BTCUSDT", Decimal(RATES[0]), TIMES[0]),
            FundingRate("BTCUSDT", Decimal(RATES[1]), TIMES[1]),
            FundingRate("BTCUSDT", Decimal(RATES[2]), TIMES[2]),
        ]
        assert all(row.next_funding_time is None for row in rows)

    def test_an_entry_with_a_next_funding_time_keeps_it(self) -> None:
        entry = {**ENTRIES[0], "nextFundingTime": MS[1]}
        (row,) = parse_funding_rate_history([entry], symbol="BTCUSDT")
        assert row.next_funding_time == TIMES[1]

    def test_entries_without_a_timestamp_or_rate_are_dropped(self) -> None:
        assert (
            parse_funding_rate_history(
                [{"fundingRate": 0.1}, {"timestamp": 0}, {}], symbol="BTCUSDT"
            )
            == []
        )


class TestFetchPage:
    async def test_the_page_is_clamped_to_until_and_carries_the_cursor(self) -> None:
        client = _FakeClient(ENTRIES)
        page = await fetch_funding_page(client, "BTCUSDT", MS[0], MS[2])
        assert [row.timestamp for row in page] == TIMES[:3]
        assert client.calls == [
            {"symbol": "BTCUSDT", "since": MS[0], "limit": 1000, "until": MS[2]}
        ]


class TestUpsert:
    async def test_a_writer_that_loses_the_advisory_lock_inserts_nothing(self) -> None:
        pool = _FakePool(locked=False)
        rows = parse_funding_rate_history(ENTRIES, symbol="BTCUSDT")
        assert await upsert_funding_rates(pool, rows) == 0
        assert pool.stored == {}

    async def test_a_mixed_symbol_batch_locks_and_inserts_every_symbol(self) -> None:
        """One lock per distinct symbol: mixed rows never insert another symbol unlocked."""
        pool = _LockSpyPool()
        rows = [
            FundingRate("ETHUSDT", Decimal("0.0001"), TIMES[0]),
            FundingRate("BTCUSDT", Decimal("0.0002"), TIMES[0]),
            FundingRate("BTCUSDT", Decimal("-0.00005"), TIMES[1]),
        ]
        assert await upsert_funding_rates(pool, rows) == 3
        assert pool.lock_keys == [
            f"funding_rates:{EXCHANGE_ID}:BTCUSDT",
            f"funding_rates:{EXCHANGE_ID}:ETHUSDT",
        ]
        assert sorted(pool.stored) == [
            ("BTCUSDT", TIMES[0]),
            ("BTCUSDT", TIMES[1]),
            ("ETHUSDT", TIMES[0]),
        ]

    async def test_a_single_symbol_batch_takes_one_lock_in_the_same_format(self) -> None:
        pool = _LockSpyPool()
        rows = parse_funding_rate_history(ENTRIES, symbol="BTCUSDT")
        assert await upsert_funding_rates(pool, rows) == 5
        assert pool.lock_keys == [f"funding_rates:{EXCHANGE_ID}:BTCUSDT"]

    async def test_a_mixed_batch_that_misses_one_symbol_lock_defers_entirely(self) -> None:
        pool = _LockSpyPool(contested={f"funding_rates:{EXCHANGE_ID}:ETHUSDT"})
        rows = [
            FundingRate("BTCUSDT", Decimal("0.0002"), TIMES[0]),
            FundingRate("ETHUSDT", Decimal("0.0001"), TIMES[0]),
        ]
        assert await upsert_funding_rates(pool, rows) == 0
        assert pool.stored == {}
        assert pool.lock_keys == [
            f"funding_rates:{EXCHANGE_ID}:BTCUSDT",
            f"funding_rates:{EXCHANGE_ID}:ETHUSDT",
        ]


class TestEnsure:
    async def test_explicit_range_walks_every_page_and_inserts_once(self) -> None:
        client, pool = _FakeClient(), _FakePool()
        report = await ensure_funding_rates(
            pool, client, "BTCUSDT", start=T0, end=TIMES[-1], page_size=2
        )
        assert report == FundingWriteReport(rows_fetched=5, rows_inserted=5, weight_spent=3)
        assert [call["since"] for call in client.calls] == [MS[0], MS[1] + 1, MS[3] + 1]

    async def test_a_refetch_of_the_same_range_inserts_nothing(self) -> None:
        client, pool = _FakeClient(), _FakePool()
        for _ in range(2):
            await ensure_funding_rates(
                pool, client, "BTCUSDT", start=T0, end=TIMES[-1], page_size=2
            )
        report = await ensure_funding_rates(
            pool, client, "BTCUSDT", start=T0, end=TIMES[-1], page_size=2
        )
        assert report.rows_fetched == 5
        assert report.rows_inserted == 0
        assert len(pool.stored) == 5

    async def test_no_stored_rows_and_no_start_fetches_nothing(self) -> None:
        """The cold-start honesty: the top-up never guesses a range to backfill."""
        client, pool = _FakeClient(), _FakePool()
        report = await ensure_funding_rates(pool, client, "BTCUSDT", end=TIMES[-1])
        assert report == FundingWriteReport(0, 0, 0)
        assert client.calls == []

    async def test_start_none_resumes_after_the_newest_stored_row(self) -> None:
        stored = [
            FundingRate("BTCUSDT", Decimal("0.0001"), TIMES[0]),
            FundingRate("BTCUSDT", Decimal("-0.00005"), TIMES[1]),
        ]
        client, pool = _FakeClient(), _FakePool(stored=stored)
        report = await ensure_funding_rates(pool, client, "BTCUSDT", now=TIMES[-1])
        assert report == FundingWriteReport(rows_fetched=3, rows_inserted=3, weight_spent=1)
        assert client.calls[0]["since"] == MS[1] + 1
        assert client.calls[0]["until"] == MS[-1]

    async def test_an_empty_page_ends_the_walk(self) -> None:
        client, pool = _FakeClient(ENTRIES[:1]), _FakePool()
        report = await ensure_funding_rates(
            pool, client, "BTCUSDT", start=T0, end=TIMES[-1], page_size=2
        )
        assert report == FundingWriteReport(rows_fetched=1, rows_inserted=1, weight_spent=2)
        assert len(client.calls) == 2

    async def test_pages_are_paced_inside_the_weight_budget(self) -> None:
        client, pool = _FakeClient(), _FakePool()
        sleep, clock = _Sleep(), _Clock()
        budget = WeightBudget(1, clock=clock, sleep=sleep)
        report = await ensure_funding_rates(
            pool, client, "BTCUSDT", start=T0, end=TIMES[-1], page_size=2, budget=budget
        )
        assert report.rows_fetched == 5
        assert report.weight_spent == 3
        assert sleep.calls == [60.0, 60.0]  # pages 2 and 3 wait the window out

    async def test_the_report_counts_only_fetched_and_missing_rows(self) -> None:
        stored = [FundingRate("BTCUSDT", Decimal("0.0001"), TIMES[0])]
        client, pool = _FakeClient(), _FakePool(stored=stored)
        report = await ensure_funding_rates(
            pool, client, "BTCUSDT", start=T0, end=TIMES[2], page_size=3
        )
        assert report == FundingWriteReport(rows_fetched=3, rows_inserted=2, weight_spent=1)


class TestRunningPerpSymbols:
    async def test_the_worklist_is_the_running_binanceusdm_sessions(self) -> None:
        pool = _FakePool(running=["ETHUSDT", "BTCUSDT"])
        assert await running_perp_symbols(pool) == ["BTCUSDT", "ETHUSDT"]


class TestFundingRefresher:
    async def test_with_no_perp_sessions_the_cycle_is_a_noop(self) -> None:
        def factory() -> Any:
            raise AssertionError("no client is built with no perp sessions")

        refresher = FundingRefresher(_FakePool(), client_factory=factory)
        await refresher.start()
        await asyncio.sleep(0)
        await refresher.stop()

    async def test_a_running_session_symbol_is_topped_up_and_the_client_closed(self) -> None:
        stored = [FundingRate("BTCUSDT", Decimal("0.0001"), TIMES[0])]
        pool = _FakePool(running=["BTCUSDT"], stored=stored)
        client = _FakeClient()
        refresher = FundingRefresher(pool, client_factory=lambda: client)
        await refresher.start()
        await asyncio.sleep(0)
        await refresher.stop()
        assert client.calls[0]["since"] == MS[0] + 1
        assert len(pool.stored) == 5
        assert client.closed == 1

    async def test_one_symbol_failing_does_not_stop_the_others(self) -> None:
        stored = [
            FundingRate("BTCUSDT", Decimal("0.0001"), TIMES[0]),
            FundingRate("ETHUSDT", Decimal("0.0001"), TIMES[0]),
        ]
        pool = _FakePool(running=["BTCUSDT", "ETHUSDT"], stored=stored)
        client = _FailingSymbolClient("ETHUSDT")
        refresher = FundingRefresher(pool, client_factory=lambda: client)
        await refresher.start()
        await asyncio.sleep(0)
        await refresher.stop()
        assert client.symbols[0] == "BTCUSDT" and client.symbols[-1] == "ETHUSDT"
        assert len(pool.stored) == 6  # BTCUSDT topped up; ETHUSDT keeps its one stored row

    async def test_a_failed_cycle_never_raises_and_retries_after_retry_s(self) -> None:
        def factory() -> Any:
            raise AssertionError("no client is built when the worklist read fails")

        class _BrokenPool(_FakePool):
            def acquire(self) -> Any:
                raise OSError("db gone")

        sleep = _BlockingSleep()
        refresher = FundingRefresher(
            _BrokenPool(), client_factory=factory, retry_s=3600.0, sleep=sleep
        )
        await refresher.start()
        await asyncio.sleep(0)
        assert sleep.calls == [3600.0]
        await refresher.stop()

    async def test_start_twice_is_refused_and_stop_is_idempotent(self) -> None:
        refresher = FundingRefresher(_FakePool())
        await refresher.start()
        with pytest.raises(RuntimeError, match="called twice"):
            await refresher.start()
        await refresher.stop()
        await refresher.stop()
