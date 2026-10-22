"""Unit tests for the backtest worker: healthcheck, handlers and job finishing.

The durable-queue behaviour (restart safety, cancel, timeout, requeue) runs against TimescaleDB in
tests/integration/test_backtest_jobs.py.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import yaml

from hydra.backtest import worker
from hydra.backtest.jobs import BacktestRunRequest, summary_from_row
from hydra.services.backtest import _preflight
from hydra.strategy.config import StrategyConfig

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
        "request": json.dumps(body.model_dump(mode="json")),
        "strategy_config": json.dumps(config.model_dump(mode="json")),
        "detail": "{}",
    }


async def _preflighted(strategy_id: str, body: BacktestRunRequest) -> Any:
    return await _preflight(
        object(), strategy_id, None, body.start_date, body.end_date, require_data=False
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# fetch_fresh uses the async REST factory
# ---------------------------------------------------------------------------


async def test_fetch_fresh_uses_the_async_rest_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """The backfill gets an async REST client; the sync client made every fetch_fresh fail.

    A sync ccxt client's ``fetch_ohlcv`` returns a list, so awaiting it raised TypeError and
    fetch_fresh silently fell back to stored bars (D1 behaviour).
    """
    import inspect

    from hydra.backtest import jobs

    seen: dict[str, Any] = {}

    class _Backfill:
        def __init__(self, *, repository: Any, exchange_factories: dict[str, Any]) -> None:
            seen["factories"] = exchange_factories

        async def bulk_download(self, **kwargs: Any) -> None:
            client = seen["factories"]["binance"]()
            try:
                seen["fetch_is_coroutine"] = inspect.iscoroutinefunction(client.fetch_ohlcv)
            finally:
                await client.close()

        async def close(self) -> None:
            return None

    monkeypatch.setattr(jobs, "ExchangeBackfillService", _Backfill)
    config = StrategyConfig(
        id="fresh",
        name="Fresh",
        strategy_class="hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        symbols=["BTCUSDT"],
        parameters={"rules": _RULES, "required_history": 10},
    )

    start, end = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
    error = await jobs.fetch_fresh_bars(object(), config, start, end)

    assert error is None
    assert seen["fetch_is_coroutine"] is True


async def test_fetch_fresh_reports_the_error_and_still_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hydra.backtest import jobs

    class _Backfill:
        def __init__(self, *, repository: Any, exchange_factories: dict[str, Any]) -> None:
            pass

        async def bulk_download(self, **kwargs: Any) -> None:
            msg = "exchange is down"
            raise OSError(msg)

        async def close(self) -> None:
            return None

    monkeypatch.setattr(jobs, "ExchangeBackfillService", _Backfill)
    config = StrategyConfig(
        id="fresh",
        name="Fresh",
        strategy_class="hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        symbols=["BTCUSDT"],
        parameters={"rules": _RULES, "required_history": 10},
    )

    start, end = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
    assert await jobs.fetch_fresh_bars(object(), config, start, end) == "exchange is down"


async def test_persist_failure_fails_the_job(strategies_dir: Path) -> None:
    _write_strategy(strategies_dir, "saw")
    body = BacktestRunRequest(strategy_id="saw", start_date="2026-01-01", end_date="2026-01-03")
    pf = await _preflighted("saw", body)
    rows = _saw_rows(datetime(2026, 1, 1, tzinfo=UTC), 60, timedelta(hours=1))
    pool = _Pool(_BarsThenFailingWrites(rows))

    with pytest.raises(worker.JobError) as exc:
        await worker.run_backtest_job(pool, _job(body, pf.config), worker.JobRun())

    assert str(exc.value).startswith("result not persisted")


async def test_curves_carry_their_own_timestamps_and_metrics_persist(
    strategies_dir: Path,
) -> None:
    _write_strategy(strategies_dir, "saw")
    body = BacktestRunRequest(strategy_id="saw", start_date="2026-01-01", end_date="2026-01-03")
    pf = await _preflighted("saw", body)
    rows = _saw_rows(datetime(2026, 1, 1, tzinfo=UTC), 60, timedelta(hours=1))
    conn = _BarsThenRecordedWrites(rows)

    outcome = await worker.run_backtest_job(_Pool(conn), _job(body, pf.config), worker.JobRun())

    assert outcome.result_id == "bt-task-unit"
    query, args = conn.inserts[0]
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

    async def _boom(pool: Any, job: Any, run: worker.JobRun) -> Any:
        raise RuntimeError("saw: ZeroDivisionError('division by zero')")

    monkeypatch.setattr(worker, "_HANDLERS", {"backtest": _boom})
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", str(tmp_path / "heartbeat"))
    conn = _JobsConn()
    job = {
        "id": "task-x",
        "kind": "backtest",
        "request": "{}",
        "strategy_config": "{}",
        "detail": "{}",
    }

    await worker._run_claimed(_Pool(conn), job, tick_s=0.01)

    finishes = [(q, a) for q, a in conn.executed if "finished_at = now()" in q]
    assert len(finishes) == 1
    query, args = finishes[0]
    assert "UPDATE backtest_jobs" in query
    assert args == ("task-x", "failed", None, None, "saw: ZeroDivisionError('division by zero')")
