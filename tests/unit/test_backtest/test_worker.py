"""Unit tests for the backtest worker: healthcheck, handlers and job finishing.

The durable-queue behaviour (restart safety, cancel, timeout, requeue) runs against TimescaleDB in
tests/integration/test_backtest_jobs.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import yaml

from hydra.backtest import jobs, worker
from hydra.backtest.jobs import (
    BacktestRunRequest,
    HyperoptRunRequest,
    ParamDefRequest,
    WalkForwardRunRequest,
    summary_from_row,
)
from hydra.core.config import DataConfig, HydraConfig
from hydra.core.types import Timeframe
from hydra.data.backfill import GapRange
from hydra.data.errors import DataFetchError, NoMarketDataError
from hydra.services.backtest import _preflight
from hydra.strategy.config import StrategyConfig
from tests.fixtures.binance_kline import FakeRestExchange

_RULES = {
    "entry_long": {
        "operator": "AND",
        "conditions": [
            {
                "indicator": "close",
                "params": {},
                "comparator": "crosses_above",
                "value": "sma:period=5",
            }
        ],
    },
    "exit_long": {
        "operator": "AND",
        "conditions": [
            {
                "indicator": "close",
                "params": {},
                "comparator": "crosses_below",
                "value": "sma:period=5",
            }
        ],
    },
}


def _write_strategy(directory: Path, strategy_id: str) -> None:
    data = {
        "id": strategy_id,
        "name": f"Strategy {strategy_id}",
        "strategy_class": "hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        "symbols": ["BTCUSDT"],
        "exchange": {"exchange_id": "binance", "market_type": "SPOT"},
        "timeframes": {"primary": "1h"},
        "parameters": {"rules": _RULES, "required_history": 10},
    }
    (directory / f"{strategy_id}.yaml").write_text(yaml.safe_dump(data))


# ---------------------------------------------------------------------------
# Healthcheck
# ---------------------------------------------------------------------------


def test_check_fresh_heartbeat_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", str(tmp_path / "heartbeat"))
    worker.touch_heartbeat()
    assert worker.check() == 0


def test_check_stale_heartbeat_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "heartbeat"
    path.write_text("")
    stale = time.time() - 120
    os.utime(path, (stale, stale))
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", str(path))
    assert worker.check() == 1


def test_check_missing_heartbeat_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", str(tmp_path / "missing"))
    assert worker.check() == 1


# ---------------------------------------------------------------------------
# Fakes: bar rows from the loader, writes recorded (or failing)
# ---------------------------------------------------------------------------


class _Acquire:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Pool:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


class _BarsThenFailingWrites:
    """Serves stored bars to the loader; the result INSERT fails like a dropped connection."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        assert "FROM ts.ohlcv_1m" in query
        return self._rows

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        # the two reads PgRiskStateStore.load_config does before a backtest runs
        assert "risk_config" in query or "system_config" in query, query
        if "risk_config" in query:
            return {"kill_switch_active": False}
        return {"value": "10000"}

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def execute(self, query: str, *args: Any) -> str:
        raise asyncpg.exceptions.ConnectionDoesNotExistError("connection was closed")


class _BarsThenRecordedWrites(_BarsThenFailingWrites):
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        super().__init__(rows)
        self.inserts: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.inserts.append((query, args))
        return "OK"

    async def executemany(self, query: str, args: Any) -> None:
        return None


class _JobsConn:
    """Serves the tick loop's UPDATE ... RETURNING and records every write."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        assert "RETURNING cancel_requested" in query
        return {"cancel_requested": False}

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append((query, args))
        return "OK"


def _saw_rows(start: datetime, count: int, step: timedelta) -> list[dict[str, Any]]:
    closes = [Decimal(1000 + (i % 20) * 5) for i in range(count)]
    return [
        {
            "open": c,
            "high": c,
            "low": c,
            "close": c,
            "volume": 10,
            "timestamp": start + i * step,
        }
        for i, c in enumerate(closes)
    ]


def _job(body: BacktestRunRequest, config: Any) -> dict[str, Any]:
    return {
        "id": "task-unit",
        "kind": "backtest",
        "worker_id": "w-unit",
        "request": json.dumps(body.model_dump(mode="json")),
        "strategy_config": json.dumps(config.model_dump(mode="json")),
        "detail": "{}",
    }


async def _preflighted(strategy_id: str, body: BacktestRunRequest) -> Any:
    return await _preflight(
        object(), strategy_id, None, body.start_date, body.end_date, require_data=False
    )


# ---------------------------------------------------------------------------
# The on-demand fetch phase: one engine per worker, progress 0→20
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_T1 = datetime(2026, 1, 1, 19, tzinfo=UTC)
_RANGE = [[_T0.isoformat(), _T1.isoformat()]]  # one gap of 20 hourly bars


def _fetch_config() -> StrategyConfig:
    return StrategyConfig(
        id="fetch",
        name="Fetch",
        strategy_class="hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        parameters={"rules": _RULES, "required_history": 10},
    )


def _progress(state: str, bars: int, weight: int = 2, elapsed: int = 0) -> dict[str, Any]:
    """The engine's progress dict, exactly the shape ``ensure_available`` emits."""
    return {
        "state": state,
        "ranges": [list(pair) for pair in _RANGE],
        "bars_fetched": bars,
        "weight_spent": weight,
        "elapsed_ms": elapsed,
    }


class _FakeEngine:
    """Stands in for ``OnDemandFetchService`` at the wiring seam, recording the call."""

    def __init__(
        self,
        *,
        emits: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
        pause_s: float = 0.0,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._emits = emits or []
        self._error = error
        self._pause_s = pause_s

    async def ensure_available(
        self,
        repo: Any,
        exchange: str,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        *,
        force: bool = False,
        on_progress: Any = None,
    ) -> None:
        self.calls.append(
            {
                "exchange": exchange,
                "symbol": symbol,
                "timeframe": timeframe,
                "start": start,
                "end": end,
                "force": force,
            }
        )
        for progress in self._emits:
            if on_progress is not None:
                on_progress(progress)
            if self._pause_s:
                await asyncio.sleep(self._pause_s)
        if self._error is not None:
            raise self._error


def _engine_call(body: Any, force: bool = False) -> dict[str, Any]:
    """The exact ensure_available call every handler must make for ``body``."""
    return {
        "exchange": "binance",
        "symbol": "BTCUSDT",
        "timeframe": Timeframe.H1,
        "start": body.start_date,
        "end": body.end_date,
        "force": force,
    }


async def test_the_kill_switch_skips_the_fetch_phase() -> None:
    """No engine (auto_fetch_enabled=false): no fetch, no progress — today's behaviour."""
    run = worker.JobRun()

    await worker._ensure_bars(object(), None, _fetch_config(), _T0, _T1, run)

    assert run.progress == 0.0
    assert run.detail == {}


async def test_the_fetch_phase_maps_the_engine_progress_onto_the_band() -> None:
    """detail['fetch'] is the engine's dict verbatim; progress climbs 0→20 and stops there."""
    run = worker.JobRun()
    seen: list[float] = []
    emits = [
        _progress("fetching", 0, weight=0),
        _progress("fetching", 10, weight=4, elapsed=5),
        _progress("fetched", 20, weight=6, elapsed=9),
    ]

    class _Recorder:
        async def ensure_available(self, *args: Any, on_progress: Any = None, **kw: Any) -> None:
            for progress in emits:
                if on_progress is not None:
                    on_progress(progress)
                seen.append(run.progress)

    await worker._ensure_bars(object(), _Recorder(), _fetch_config(), _T0, _T1, run)

    # 20 hourly bars owed: 10 fetched lands at 10.0, the final emit caps at 20.0
    assert seen == [0.0, 10.0, 20.0]
    assert run.progress == 20.0
    assert run.detail["fetch"] == emits[-1]


async def test_a_data_fetch_error_fails_the_phase_retryable_with_the_reason() -> None:
    run = worker.JobRun()
    error = DataFetchError(
        "rate_limited",
        "binance",
        "BTCUSDT",
        Timeframe.H1,
        weight_spent=2,
        elapsed_s=31.2,
        remaining_ranges=[GapRange(start=_T0, end=_T1)],
    )
    engine = _FakeEngine(emits=[_progress("fetching", 0, weight=0)], error=error)

    with pytest.raises(worker.JobError) as excinfo:
        await worker._ensure_bars(object(), engine, _fetch_config(), _T0, _T1, run)

    assert str(excinfo.value) == error.message
    assert excinfo.value.__cause__ is error
    assert run.detail["fetch"] == {
        **_progress("fetching", 0, weight=0),
        "state": "failed",
        "reason": "rate_limited",
    }
    assert run.progress == 0.0


async def test_a_range_the_exchange_lacks_keeps_the_no_market_data_vocabulary() -> None:
    run = worker.JobRun()
    error = NoMarketDataError("binance", "BTCUSDT", Timeframe.H1, _T0, _T1, None, None)
    engine = _FakeEngine(emits=[_progress("fetching", 0, weight=0)], error=error)

    with pytest.raises(NoMarketDataError) as excinfo:
        await worker._ensure_bars(object(), engine, _fetch_config(), _T0, _T1, run)

    assert excinfo.value is error  # the P0-B class re-raised unchanged, never wrapped
    assert run.detail["fetch"]["state"] == "failed"


async def test_backtest_handler_ensures_bars_through_the_engine_before_loading(
    strategies_dir: Path,
) -> None:
    _write_strategy(strategies_dir, "saw")
    body = BacktestRunRequest(strategy_id="saw", start_date="2026-01-01", end_date="2026-01-03")
    pf = await _preflighted("saw", body)
    rows = _saw_rows(datetime(2026, 1, 1, tzinfo=UTC), 60, timedelta(hours=1))
    engine = _FakeEngine(emits=[_progress("fetching", 0, weight=0), _progress("fetched", 60)])
    run = worker.JobRun()

    outcome = await worker.run_backtest_job(
        _Pool(_BarsThenRecordedWrites(rows)), engine, _job(body, pf.config), run
    )

    assert engine.calls == [_engine_call(body)]
    assert outcome.result_id == "bt-task-unit"
    assert run.detail["fetch"] == _progress("fetched", 60)
    assert run.detail["provenance"]["bar_count"] == 60


async def test_fetch_fresh_maps_to_the_engine_force(strategies_dir: Path) -> None:
    """body.fetch_fresh keeps its meaning: the whole range refetched (force=True)."""
    _write_strategy(strategies_dir, "saw")
    body = BacktestRunRequest(
        strategy_id="saw", start_date="2026-01-01", end_date="2026-01-03", fetch_fresh=True
    )
    pf = await _preflighted("saw", body)
    rows = _saw_rows(datetime(2026, 1, 1, tzinfo=UTC), 60, timedelta(hours=1))
    engine = _FakeEngine(emits=[_progress("fetching", 0, weight=0), _progress("fetched", 60)])
    run = worker.JobRun()

    outcome = await worker.run_backtest_job(
        _Pool(_BarsThenRecordedWrites(rows)), engine, _job(body, pf.config), run
    )

    assert engine.calls == [_engine_call(body, force=True)]
    assert outcome.result_id == "bt-task-unit"


async def test_hyperopt_handler_ensures_bars_through_the_engine(strategies_dir: Path) -> None:
    _write_strategy(strategies_dir, "saw")
    body = HyperoptRunRequest(
        strategy_id="saw",
        start_date="2026-01-01",
        end_date="2026-01-03",
        method="grid",
        max_trials=1,
        param_space=[ParamDefRequest(name="sma_period", type="int", low=4, high=5)],
    )
    pf = await _preflighted("saw", body)
    rows = _saw_rows(datetime(2026, 1, 1, tzinfo=UTC), 60, timedelta(hours=1))
    engine = _FakeEngine(emits=[_progress("fetching", 0, weight=0), _progress("fetched", 60)])
    run = worker.JobRun()

    outcome = await worker.run_hyperopt_job(
        _Pool(_BarsThenRecordedWrites(rows)), engine, _job(body, pf.config), run
    )

    assert engine.calls == [_engine_call(body)]
    assert outcome.result is not None
    assert outcome.result["provenance"]["bar_count"] == 60
    assert run.detail["fetch"] == _progress("fetched", 60)
    assert run.progress == 20.0


async def test_walkforward_handler_ensures_bars_through_the_engine(strategies_dir: Path) -> None:
    _write_strategy(strategies_dir, "saw")
    body = WalkForwardRunRequest(
        strategy_id="saw",
        start_date="2026-01-01",
        end_date="2026-03-05",
        fold_count=2,
        param_space=[ParamDefRequest(name="sma_period", type="int", low=4, high=5)],
    )
    pf = await _preflighted("saw", body)
    rows = _saw_rows(datetime(2026, 1, 1, tzinfo=UTC), 1512, timedelta(hours=1))
    engine = _FakeEngine(emits=[_progress("fetching", 0, weight=0), _progress("fetched", 1512)])
    run = worker.JobRun()

    outcome = await worker.run_walkforward_job(
        _Pool(_BarsThenRecordedWrites(rows)), engine, _job(body, pf.config), run
    )

    assert engine.calls == [_engine_call(body)]
    assert outcome.result is not None
    assert outcome.result["folds"]
    assert run.detail["fetch"] == _progress("fetched", 1512)


class _TickAndLoadConn(_BarsThenRecordedWrites):
    """Serves the loader and the tick loop; records every tick's progress and detail."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        super().__init__(rows)
        self.ticks: list[tuple[float, dict[str, Any]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        if "RETURNING cancel_requested" in query:
            self.ticks.append((args[1], json.loads(args[2])))
            return {"cancel_requested": False}
        return await super().fetchrow(query, *args)


async def test_mid_fetch_progress_is_visible_on_the_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, strategies_dir: Path
) -> None:
    """The 10 s tick flushes detail.fetch while the fetch runs, live on the status row."""
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", str(tmp_path / "heartbeat"))
    _write_strategy(strategies_dir, "saw")
    body = BacktestRunRequest(strategy_id="saw", start_date="2026-01-01", end_date="2026-01-03")
    pf = await _preflighted("saw", body)
    rows = _saw_rows(datetime(2026, 1, 1, tzinfo=UTC), 60, timedelta(hours=1))
    engine = _FakeEngine(
        emits=[_progress("fetching", 0, weight=0), _progress("fetched", 60)], pause_s=0.05
    )
    conn = _TickAndLoadConn(rows)

    await worker._run_claimed(_Pool(conn), _job(body, pf.config), tick_s=0.01, fetch_engine=engine)

    mid = [(p, d) for p, d in conn.ticks if d["fetch"]["state"] == "fetching"]
    assert mid, conn.ticks
    progress, detail = mid[0]
    assert detail["fetch"]["bars_fetched"] == 0
    assert progress < 20.0
    done = [(p, d) for p, d in conn.ticks if d["fetch"]["state"] == "fetched"]
    assert done and done[0][0] == 20.0  # the phase's 20.0, before the run moves it on
    finishes = [(q, a) for q, a in conn.inserts if "finished_at = now()" in q]
    assert len(finishes) == 1
    assert finishes[0][1][:2] == ("task-unit", "completed")
    assert json.loads(finishes[0][1][5])["fetch"] == _progress("fetched", 60)


async def test_a_failed_fetch_fails_the_job_row_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fetch past patience: the row fails with the typed reason; detail.fetch says failed."""
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", str(tmp_path / "heartbeat"))
    body = BacktestRunRequest(strategy_id="saw", start_date="2026-01-01", end_date="2026-01-03")
    error = DataFetchError(
        "rate_limited",
        "binance",
        "BTCUSDT",
        Timeframe.H1,
        weight_spent=2,
        elapsed_s=31.2,
        remaining_ranges=[GapRange(start=body.start_date, end=body.end_date)],
    )
    engine = _FakeEngine(error=error)
    conn = _JobsConn()

    await worker._run_claimed(
        _Pool(conn), _job(body, _fetch_config()), tick_s=0.01, fetch_engine=engine
    )

    finishes = [(q, a) for q, a in conn.executed if "finished_at = now()" in q]
    assert len(finishes) == 1
    _, args = finishes[0]
    assert args[:2] == ("task-unit", "failed")
    assert args[4] == error.message
    assert "(rate_limited)" in args[4]
    assert json.loads(args[5])["fetch"] == {"state": "failed", "reason": "rate_limited"}


async def test_a_no_market_data_job_failure_keeps_the_vocabulary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-listing range: the row fails with today's no_market_data message."""
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", str(tmp_path / "heartbeat"))
    body = BacktestRunRequest(strategy_id="saw", start_date="2026-01-01", end_date="2026-01-03")
    error = NoMarketDataError(
        "binance",
        "BTCUSDT",
        Timeframe.H1,
        body.start_date,
        body.end_date,
        datetime(2026, 1, 4, tzinfo=UTC),
        datetime(2026, 1, 9, tzinfo=UTC),
    )
    engine = _FakeEngine(error=error)
    conn = _JobsConn()

    await worker._run_claimed(
        _Pool(conn), _job(body, _fetch_config()), tick_s=0.01, fetch_engine=engine
    )

    finishes = [(q, a) for q, a in conn.executed if "finished_at = now()" in q]
    assert len(finishes) == 1
    _, args = finishes[0]
    assert args[:2] == ("task-unit", "failed")
    assert args[4] == error.message
    assert "available: 2026-01-04T00:00:00+00:00 to 2026-01-09T00:00:00+00:00" in args[4]
    assert json.loads(args[5])["fetch"]["state"] == "failed"


# ---------------------------------------------------------------------------
# The worker-lifetime client pool: one client, one engine per process
# ---------------------------------------------------------------------------


def _kline(ts: datetime) -> list[float]:
    return [ts.timestamp() * 1000, 42000.0, 42500.0, 41800.0, 42100.0, 100.0]


class _FetchConn(_BarsThenRecordedWrites):
    """Serves the real engine's SQL and the loader; the detect queue is popped per read."""

    def __init__(
        self, rows: list[dict[str, Any]], detect_answers: list[list[dict[str, Any]]]
    ) -> None:
        super().__init__(rows)
        self.detect_answers = list(detect_answers)

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "WITH present" in query:
            return self.detect_answers.pop(0)
        if "unnest" in query:
            return []
        assert "ORDER BY timestamp ASC" in query, query
        return self._rows


async def test_consecutive_fetching_jobs_share_one_pooled_client() -> None:
    """Two fetching jobs through the ONE engine: the client is constructed exactly once."""
    constructed: list[str] = []
    exchange = FakeRestExchange(
        [_kline(datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=i)) for i in range(24)]
    )

    def _factory(exchange_id: str, creds: str | None) -> Any:
        constructed.append(exchange_id)
        return exchange

    clients = worker._WorkerFetchClients(_factory)
    engine = clients.fetch_engine(HydraConfig(data=DataConfig()))
    assert engine is not None

    body = BacktestRunRequest(strategy_id="saw", start_date="2026-01-01", end_date="2026-01-02")
    hole = [
        {
            "gap_start": datetime(2026, 1, 1, tzinfo=UTC),
            "gap_end": datetime(2026, 1, 1, 23, tzinfo=UTC),
        }
    ]
    rows = _saw_rows(datetime(2026, 1, 1, tzinfo=UTC), 24, timedelta(hours=1))

    for _ in range(2):
        conn = _FetchConn(rows, detect_answers=[hole, []])
        run = worker.JobRun()
        outcome = await worker.run_backtest_job(
            _Pool(conn), engine, _job(body, _fetch_config()), run
        )
        assert outcome.result_id == "bt-task-unit"
        assert run.detail["fetch"]["state"] == "fetched"
        assert run.detail["fetch"]["bars_fetched"] == 24

    assert constructed == ["binance"]  # the pool built the client once, not per job
    assert len(exchange.calls) == 2  # both jobs really fetched through it
    assert exchange.close_calls == 0  # held for the worker's lifetime

    await clients.close()
    assert exchange.close_calls == 1


def test_no_engine_while_the_kill_switch_is_off() -> None:
    """auto_fetch_enabled=false constructs no engine at all — the worker fetches nothing."""
    clients = worker._WorkerFetchClients()
    assert clients.fetch_engine(HydraConfig(data=DataConfig(auto_fetch_enabled=False))) is None


async def test_persist_failure_fails_the_job(strategies_dir: Path) -> None:
    _write_strategy(strategies_dir, "saw")
    body = BacktestRunRequest(strategy_id="saw", start_date="2026-01-01", end_date="2026-01-03")
    pf = await _preflighted("saw", body)
    rows = _saw_rows(datetime(2026, 1, 1, tzinfo=UTC), 60, timedelta(hours=1))
    pool = _Pool(_BarsThenFailingWrites(rows))

    with pytest.raises(worker.JobError) as exc:
        await worker.run_backtest_job(pool, None, _job(body, pf.config), worker.JobRun())

    assert str(exc.value).startswith("result not persisted")


async def test_curves_carry_their_own_timestamps_and_metrics_persist(
    strategies_dir: Path,
) -> None:
    _write_strategy(strategies_dir, "saw")
    body = BacktestRunRequest(strategy_id="saw", start_date="2026-01-01", end_date="2026-01-03")
    pf = await _preflighted("saw", body)
    rows = _saw_rows(datetime(2026, 1, 1, tzinfo=UTC), 60, timedelta(hours=1))
    conn = _BarsThenRecordedWrites(rows)

    outcome = await worker.run_backtest_job(
        _Pool(conn), None, _job(body, pf.config), worker.JobRun()
    )

    assert outcome.result_id == "bt-task-unit"
    # the LRU's last_used_at UPDATE precedes the results write; locate, don't assume position
    query, args = next((q, a) for q, a in conn.inserts if "INSERT INTO backtest_results" in q)
    columns = re.search(r"INSERT INTO backtest_results\s*\(([^)]*)\)", query)
    assert columns is not None
    row = dict(zip([c.strip() for c in columns.group(1).split(",")], args, strict=True))
    equity = json.loads(row["equity_curve"])
    bench = json.loads(row["benchmark_equity"])
    labels = [p["timestamp"] for p in equity]
    assert labels[-len(rows) :] == [r["timestamp"].isoformat() for r in rows]
    assert [p["timestamp"] for p in bench] == labels
    close_at = {r["timestamp"].isoformat(): r["close"] for r in rows}
    for point in bench:
        assert point["value"] == float(10000 * close_at[point["timestamp"]] / rows[0]["close"])
    metrics = summary_from_row(row)["metrics"]
    assert metrics["total_return"] == round((equity[-1]["value"] / equity[0]["value"] - 1) * 100, 2)
    assert isinstance(metrics["max_drawdown_duration_s"], int)


async def test_handler_exception_fails_the_job_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handler exception (e.g. StrategyError) ends the row failed with its message."""

    async def _boom(pool: Any, fetch_engine: Any, job: Any, run: worker.JobRun) -> Any:
        raise RuntimeError("saw: ZeroDivisionError('division by zero')")

    monkeypatch.setattr(worker, "_HANDLERS", {"backtest": _boom})
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", str(tmp_path / "heartbeat"))
    conn = _JobsConn()
    job = {
        "id": "task-x",
        "kind": "backtest",
        "worker_id": "w-unit",
        "request": "{}",
        "strategy_config": "{}",
        "detail": "{}",
    }

    await worker._run_claimed(_Pool(conn), job, tick_s=0.01)

    finishes = [(q, a) for q, a in conn.executed if "finished_at = now()" in q]
    assert len(finishes) == 1
    query, args = finishes[0]
    assert "UPDATE backtest_jobs" in query
    assert "detail = COALESCE" in query
    assert args == (
        "task-x",
        "failed",
        None,
        None,
        "saw: ZeroDivisionError('division by zero')",
        "{}",
        "w-unit",
    )
    assert "AND worker_id = $7" in query


# ---------------------------------------------------------------------------
# Durability: the terminal write flushes detail, survives DB blips, cleans up
# ---------------------------------------------------------------------------


async def test_finish_flushes_the_handlers_final_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job completing between ticks must persist its detail (provenance, trials, fetch)."""

    async def _handler(pool: Any, fetch_engine: Any, job: Any, run: worker.JobRun) -> Any:
        run.detail["provenance"] = {"bar_count": 300, "hydra_version": "9.9.9"}
        run.detail["completed_trials"] = 4
        return worker.JobOutcome(result_id="bt-task-x")

    monkeypatch.setattr(worker, "_HANDLERS", {"backtest": _handler})
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", str(tmp_path / "heartbeat"))
    conn = _JobsConn()
    job = {
        "id": "task-x",
        "kind": "backtest",
        "worker_id": "w-unit",
        "request": "{}",
        "strategy_config": "{}",
        "detail": "{}",
    }

    await worker._run_claimed(_Pool(conn), job, tick_s=0.01)

    finishes = [(q, a) for q, a in conn.executed if "finished_at = now()" in q]
    assert len(finishes) == 1
    args = finishes[0][1]
    assert args[:2] == ("task-x", "completed")
    detail = json.loads(args[5])
    assert detail == {
        "provenance": {"bar_count": 300, "hydra_version": "9.9.9"},
        "completed_trials": 4,
    }


class _TickBlipConn:
    """Serves the claim; every tick UPDATE fails like a dropped connection."""

    def __init__(self) -> None:
        self.finishes: list[tuple[str, tuple[Any, ...]]] = []
        self.tick_failures = 0

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        if "RETURNING cancel_requested" in query:
            self.tick_failures += 1
            raise asyncpg.exceptions.ConnectionDoesNotExistError("connection was closed")
        assert "RETURNING *" in query
        return {
            "id": "task-blip",
            "kind": "backtest",
            "worker_id": "w-unit",
            "request": "{}",
            "strategy_config": "{}",
            "detail": "{}",
        }

    async def execute(self, query: str, *args: Any) -> str:
        self.finishes.append((query, args))
        return "OK"


async def test_a_tick_database_error_does_not_kill_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One failed progress flush must neither fail the job nor stop the heartbeat."""

    async def _handler(pool: Any, fetch_engine: Any, job: Any, run: worker.JobRun) -> Any:
        await asyncio.sleep(0.05)
        return worker.JobOutcome(result_id="bt-task-blip")

    monkeypatch.setattr(worker, "_HANDLERS", {"backtest": _handler})
    heartbeat = tmp_path / "heartbeat"
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", str(heartbeat))
    conn = _TickBlipConn()
    job = {
        "id": "task-blip",
        "kind": "backtest",
        "worker_id": "w-unit",
        "request": "{}",
        "strategy_config": "{}",
        "detail": "{}",
    }

    await worker._run_claimed(_Pool(conn), job, tick_s=0.005)

    assert conn.tick_failures >= 1
    assert len(conn.finishes) == 1
    assert conn.finishes[0][1][:2] == ("task-blip", "completed")
    assert heartbeat.exists()
    assert worker.check() == 0


class _FinishOnceFailingConn(_JobsConn):
    """The first terminal UPDATE fails like a dropped connection; the retry succeeds."""

    def __init__(self) -> None:
        super().__init__()
        self._failed_once = False

    async def execute(self, query: str, *args: Any) -> str:
        if "finished_at = now()" in query and not self._failed_once:
            self._failed_once = True
            raise asyncpg.exceptions.ConnectionDoesNotExistError("connection was closed")
        self.executed.append((query, args))
        return "OK"


async def test_a_database_error_at_the_finish_write_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The outcome exists only in memory, so the terminal write outlives a DB blip."""

    async def _handler(pool: Any, fetch_engine: Any, job: Any, run: worker.JobRun) -> Any:
        return worker.JobOutcome(result_id="bt-task-r")

    monkeypatch.setattr(worker, "_HANDLERS", {"backtest": _handler})
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", str(tmp_path / "heartbeat"))
    conn = _FinishOnceFailingConn()
    job = {
        "id": "task-r",
        "kind": "backtest",
        "worker_id": "w-unit",
        "request": "{}",
        "strategy_config": "{}",
        "detail": "{}",
    }

    await worker._run_claimed(_Pool(conn), job, tick_s=0.01)

    finishes = [(q, a) for q, a in conn.executed if "finished_at = now()" in q]
    assert len(finishes) == 1
    assert finishes[0][1][:2] == ("task-r", "completed")


class _CancelConn:
    """Every tick reports a cancel request, so the tick cancels the handler task."""

    def __init__(self) -> None:
        self.finishes: list[tuple[str, tuple[Any, ...]]] = []
        self.deletes: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        assert "RETURNING cancel_requested" in query
        return {"cancel_requested": True}

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def execute(self, query: str, *args: Any) -> str:
        if "DELETE FROM" in query:
            self.deletes.append((query, args))
        else:
            self.finishes.append((query, args))
        return "OK"


async def test_cancel_after_the_persist_straddle_discards_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel landing after the result commit must not leave an orphan completed result."""

    async def _handler(pool: Any, fetch_engine: Any, job: Any, run: worker.JobRun) -> Any:
        run.persisted_result_id = f"bt-{job['id']}"
        await asyncio.sleep(5)  # cancelled by the tick's cancel_requested

    monkeypatch.setattr(worker, "_HANDLERS", {"backtest": _handler})
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", str(tmp_path / "heartbeat"))
    conn = _CancelConn()
    job = {
        "id": "task-c",
        "kind": "backtest",
        "worker_id": "w-unit",
        "request": "{}",
        "strategy_config": "{}",
        "detail": "{}",
    }

    await worker._run_claimed(_Pool(conn), job, tick_s=0.01)

    assert len(conn.finishes) == 1
    assert conn.finishes[0][1][:2] == ("task-c", "cancelled")
    assert [args for _, args in conn.deletes] == [("bt-task-c",), ("bt-task-c",)]


# ---------------------------------------------------------------------------
# The lease: a worker whose row was requeued abandons the job without a write
# ---------------------------------------------------------------------------


class _LeaseLostConn:
    """The tick's fenced UPDATE matches no row: a peer requeued the job after the lease expired."""

    def __init__(self) -> None:
        self.tick_args: list[tuple[Any, ...]] = []
        self.writes: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> None:
        assert "AND worker_id = $4" in query
        self.tick_args.append(args)
        return None

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def execute(self, query: str, *args: Any) -> str:
        self.writes.append((query, args))
        return "OK"


async def test_a_lost_lease_abandons_the_job_without_a_write_or_a_discard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The result id is shared with the new owner's run, so the loser must not delete it."""

    async def _handler(pool: Any, fetch_engine: Any, job: Any, run: worker.JobRun) -> Any:
        run.persisted_result_id = f"bt-{job['id']}"
        await asyncio.sleep(5)  # cancelled by the tick's lost lease

    monkeypatch.setattr(worker, "_HANDLERS", {"backtest": _handler})
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", str(tmp_path / "heartbeat"))
    conn = _LeaseLostConn()
    job = {
        "id": "task-l",
        "kind": "backtest",
        "worker_id": "w-old",
        "request": "{}",
        "strategy_config": "{}",
        "detail": "{}",
    }

    await asyncio.wait_for(worker._run_claimed(_Pool(conn), job, tick_s=0.01), 2.0)

    assert [args[3] for args in conn.tick_args] == ["w-old"]
    assert conn.writes == []  # no terminal write, no requeue, no result discard


class _FencedFinishConn(_JobsConn):
    """The terminal UPDATE matches no row: the lease expired between the last tick and the end."""

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append((query, args))
        return "UPDATE 0"


async def test_a_fenced_finish_is_dropped_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _handler(pool: Any, fetch_engine: Any, job: Any, run: worker.JobRun) -> Any:
        return worker.JobOutcome(result_id="bt-task-f")

    monkeypatch.setattr(worker, "_HANDLERS", {"backtest": _handler})
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", str(tmp_path / "heartbeat"))
    conn = _FencedFinishConn()
    job = {
        "id": "task-f",
        "kind": "backtest",
        "worker_id": "w-old",
        "request": "{}",
        "strategy_config": "{}",
        "detail": "{}",
    }

    await asyncio.wait_for(worker._run_claimed(_Pool(conn), job, tick_s=0.01), 2.0)

    finishes = [a for q, a in conn.executed if "finished_at = now()" in q]
    assert len(finishes) == 1
    assert finishes[0][:2] == ("task-f", "completed")
    assert finishes[0][6] == "w-old"


async def test_an_idle_worker_sweeps_expired_leases_before_claiming(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The periodic sweep runs on the idle path, throttled, and never as the worker's own id."""
    sweeps: list[tuple[float, dict[str, Any]]] = []
    claims = 0

    async def _requeue(pool: Any, lease_s: float, **kwargs: Any) -> None:
        sweeps.append((lease_s, kwargs))

    async def _claim(pool: Any, worker_id: str) -> None:
        nonlocal claims
        claims += 1
        if claims == 3:
            signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(jobs, "requeue_abandoned", _requeue)
    monkeypatch.setattr(jobs, "claim", _claim)
    monkeypatch.setattr(worker, "IDLE_SLEEP_S", 0.01)
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", str(tmp_path / "heartbeat"))

    await asyncio.wait_for(worker._serve_forever(object(), None, 180), 2.0)

    assert claims == 3
    assert sweeps == [(180, {})]


async def test_wait_for_schema_keeps_the_heartbeat_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long migration wait must not let the file-age healthcheck restart the worker."""
    heartbeat = tmp_path / "heartbeat"
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", str(heartbeat))

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            worker.wait_for_schema("postgresql://127.0.0.1:1/none", poll_s=0.01), 0.3
        )

    assert heartbeat.exists()
    assert worker.check() == 0
