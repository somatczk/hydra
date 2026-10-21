"""Unit tests for the backtest routes: strategy resolution, pre-flight errors, state handling.

The happy path (real bars in, provenance and trades out) runs against TimescaleDB in
tests/integration/test_backtest_real_data.py.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hydra.backtest.jobs import BacktestRunRequest, detail_from_row, summary_from_row
from hydra.backtest.walkforward import WalkForwardAnalyzer
from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import OHLCV, Timeframe
from hydra.dashboard.routes._db import install_error_handlers
from hydra.dashboard.routes.backtest import router
from hydra.services.backtest import (
    _RESULTS,
    _TASKS,
    BacktestResultDetail,
    _load_backtest_strategy,
    _preflight,
    _run_backtest_task,
)
from hydra.services.errors import ServiceError
from hydra.services.research import (
    _HYPEROPT_TASKS,
    _WALKFORWARD_TASKS,
    _walkforward_months,
    _window_fits,
)
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig

SRC = Path(__file__).resolve().parents[3] / "src" / "hydra"

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


def _write_strategy(directory: Path, strategy_id: str, **overrides: Any) -> Path:
    data: dict[str, Any] = {
        "id": strategy_id,
        "name": f"Strategy {strategy_id}",
        "strategy_class": "hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        "symbols": ["BTCUSDT"],
        "exchange": {"exchange_id": "binance", "market_type": "SPOT"},
        "timeframes": {"primary": "1h"},
        "parameters": {"rules": _RULES, "required_history": 10},
    }
    data.update(overrides)
    path = directory / f"{strategy_id}.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def _make_app(pool: object | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    install_error_handlers(app)
    app.state.db_pool = pool
    return app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(_make_app(pool=None))


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    stores: list[dict[str, Any]] = [_TASKS, _RESULTS, _HYPEROPT_TASKS, _WALKFORWARD_TASKS]
    saved = [dict(s) for s in stores]
    for s in stores:
        s.clear()
    yield
    for s, snapshot in zip(stores, saved, strict=True):
        s.clear()
        s.update(snapshot)


def _run_body(strategy_id: str = "saw", **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "strategy_id": strategy_id,
        "start_date": "2026-01-01",
        "end_date": "2026-02-01",
    }
    body.update(overrides)
    return body


def _hyperopt_body(strategy_id: str = "saw", **overrides: Any) -> dict[str, Any]:
    body = _run_body(
        strategy_id, param_space=[{"name": "sma_period", "type": "int", "low": 3, "high": 8}]
    )
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# No synthetic data or placeholder results anywhere in the dashboard
# ---------------------------------------------------------------------------


def test_dashboard_has_no_synthetic_bar_generator() -> None:
    offenders: list[str] = []
    for path in [
        *(SRC / "dashboard").rglob("*.py"),
        *(SRC / "services").rglob("*.py"),
        SRC / "backtest" / "jobs.py",
    ]:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_generate_sample_bars":
                offenders.append(f"{path.name}: def {node.name}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "default_rng"
            ):
                offenders.append(f"{path.name}:{node.lineno} default_rng()")
    assert offenders == []


def test_no_seeded_placeholder_results() -> None:
    source = "".join(
        (SRC / rel).read_text()
        for rel in ("services/backtest.py", "services/research.py", "backtest/jobs.py")
    )
    assert not any(f'"bt-{n}"' in source for n in (1, 2, 3))
    assert "LSTM Momentum" not in source


# ---------------------------------------------------------------------------
# Strategy resolution
# ---------------------------------------------------------------------------


class TestLoadBacktestStrategy:
    def test_full_config_is_loaded(self, strategies_dir: Path) -> None:
        _write_strategy(
            strategies_dir,
            "sized",
            exchange={"exchange_id": "kraken", "market_type": "SPOT"},
            timeframes={"primary": "4h"},
            position_sizing={
                "method": "fixed_fractional",
                "risk_per_trade_pct": 2.0,
                "max_position_pct": 25,
            },
        )
        cls, config = _load_backtest_strategy("sized")
        assert cls.__name__ == "RuleBasedStrategy"
        assert config.position_sizing.risk_per_trade_pct == 2.0
        assert config.position_sizing.max_position_pct == 25.0
        assert config.exchange.exchange_id == "kraken"
        assert config.timeframes.primary == Timeframe.H4
        assert config.parameters["required_history"] == 10

    def test_unknown_strategy_is_404(self) -> None:
        with pytest.raises(ServiceError) as exc:
            _load_backtest_strategy("does_not_exist_xyz")
        assert exc.value.status_code == 404
        assert exc.value.detail["state"] == "not_found"

    def test_unimportable_class_is_422(self, strategies_dir: Path) -> None:
        _write_strategy(
            strategies_dir, "grid", strategy_class="hydra.strategy.builtin.grid.GridStrategy"
        )
        with pytest.raises(ServiceError) as exc:
            _load_backtest_strategy("grid")
        assert exc.value.status_code == 422
        assert "cannot import strategy class" in exc.value.detail["message"]

    def test_non_strategy_class_is_422(self, strategies_dir: Path) -> None:
        _write_strategy(
            strategies_dir, "ctx", strategy_class="hydra.strategy.context.StrategyContext"
        )
        with pytest.raises(ServiceError) as exc:
            _load_backtest_strategy("ctx")
        assert exc.value.status_code == 422
        assert "not supported by the backtester" in exc.value.detail["message"]

    def test_futures_is_422(self, strategies_dir: Path) -> None:
        _write_strategy(
            strategies_dir, "perp", exchange={"exchange_id": "binance", "market_type": "FUTURES"}
        )
        with pytest.raises(ServiceError) as exc:
            _load_backtest_strategy("perp")
        assert exc.value.status_code == 422
        assert "futures" in exc.value.detail["message"]

    def test_malformed_config_is_422(self, strategies_dir: Path) -> None:
        _write_strategy(strategies_dir, "broken", symbols=5)
        with pytest.raises(ServiceError) as exc:
            _load_backtest_strategy("broken")
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "invalid_strategy"

    def test_class_outside_strategy_package_is_not_imported(self, strategies_dir: Path) -> None:
        _write_strategy(strategies_dir, "evil", strategy_class="os.system")
        with pytest.raises(ServiceError) as exc:
            _load_backtest_strategy("evil")
        assert exc.value.status_code == 422
        assert "must live under hydra.strategy." in exc.value.detail["message"]

    def test_strategy_files_are_read_at_call_time(self, strategies_dir: Path) -> None:
        with pytest.raises(ServiceError):
            _load_backtest_strategy("late")
        _write_strategy(strategies_dir, "late")
        _, config = _load_backtest_strategy("late")
        assert config.id == "late"


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------


class TestRequestDates:
    def test_bare_end_date_is_inclusive_end_of_day_utc(self) -> None:
        body = BacktestRunRequest(strategy_id="s", start_date="2026-03-01", end_date="2026-03-01")
        assert body.start_date == datetime(2026, 3, 1, tzinfo=UTC)
        assert body.end_date == datetime(2026, 3, 1, 23, 59, 59, 999999, tzinfo=UTC)

    def test_offset_datetimes_are_converted_not_relabelled(self) -> None:
        body = BacktestRunRequest(
            strategy_id="s",
            start_date="2026-03-01T05:00:00+02:00",
            end_date="2026-03-02T00:00:00",
        )
        assert body.start_date == datetime(2026, 3, 1, 3, 0, tzinfo=UTC)
        assert body.end_date == datetime(2026, 3, 2, tzinfo=UTC)

    def test_start_after_end_is_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/backtest/run", json=_run_body(start_date="2026-03-02", end_date="2026-03-01")
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["state"] == "invalid"
        assert "start_date must be before end_date" in detail["message"]

    def test_unsupported_timeframe_is_422(self, client: TestClient) -> None:
        resp = client.post("/api/backtest/run", json=_run_body(timeframe="30m"))
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "validation_error"
        assert _TASKS == {}

    def test_hyperopt_requires_dates(self, client: TestClient) -> None:
        body = _hyperopt_body()
        del body["start_date"], body["end_date"]
        resp = client.post("/api/backtest/hyperopt", json=body)
        assert resp.status_code == 422
        assert "start_date" in resp.json()["detail"]["message"]

    def test_non_positive_capital_is_422(self, client: TestClient) -> None:
        resp = client.post("/api/backtest/run", json=_run_body(initial_capital=0))
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Pre-flight: unknown strategy 404, no database 503, nothing queued
# ---------------------------------------------------------------------------


class TestPreflightErrors:
    @pytest.mark.parametrize(
        ("path", "body"),
        [
            ("/api/backtest/run", _run_body("does_not_exist_xyz")),
            ("/api/backtest/hyperopt", _hyperopt_body("does_not_exist_xyz")),
            ("/api/backtest/walkforward", _run_body("does_not_exist_xyz")),
        ],
    )
    def test_unknown_strategy_is_404_and_queues_nothing(
        self, client: TestClient, path: str, body: dict[str, Any]
    ) -> None:
        resp = client.post(path, json=body)
        assert resp.status_code == 404
        assert resp.json()["detail"] == {
            "code": "strategy_not_found",
            "message": "Strategy does_not_exist_xyz not found",
            "state": "not_found",
            "strategy_id": "does_not_exist_xyz",
        }
        assert _TASKS == {} and _HYPEROPT_TASKS == {} and _WALKFORWARD_TASKS == {}

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            ("/api/backtest/run", _run_body()),
            ("/api/backtest/hyperopt", _hyperopt_body()),
            ("/api/backtest/walkforward", _run_body()),
        ],
    )
    def test_no_database_is_503_and_queues_nothing(
        self, client: TestClient, strategies_dir: Path, path: str, body: dict[str, Any]
    ) -> None:
        _write_strategy(strategies_dir, "saw")
        resp = client.post(path, json=body)
        assert resp.status_code == 503
        assert resp.json()["detail"]["state"] == "unavailable"
        assert _TASKS == {} and _HYPEROPT_TASKS == {} and _WALKFORWARD_TASKS == {}

    def test_unsupported_class_is_422_before_db(
        self, client: TestClient, strategies_dir: Path
    ) -> None:
        _write_strategy(
            strategies_dir, "dca", strategy_class="hydra.strategy.builtin.dca.DCAStrategy"
        )
        resp = client.post("/api/backtest/run", json=_run_body("dca"))
        assert resp.status_code == 422
        assert resp.json()["detail"]["state"] == "invalid"

    def test_hyperopt_bad_param_space_is_422(
        self, client: TestClient, strategies_dir: Path
    ) -> None:
        _write_strategy(strategies_dir, "saw")
        resp = client.post(
            "/api/backtest/hyperopt",
            json=_hyperopt_body(param_space=[{"name": "x", "type": "int"}]),
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "invalid_param_space"

    async def test_request_timeframe_overrides_strategy_primary(self, strategies_dir: Path) -> None:
        _write_strategy(strategies_dir, "saw", timeframes={"primary": "4h"})

        pool = object()
        start, end = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
        pf = await _preflight(pool, "saw", Timeframe.D1, start, end, require_data=False)
        assert pf.timeframe == Timeframe.D1
        assert pf.config.timeframes.primary == Timeframe.D1
        default = await _preflight(pool, "saw", None, start, end, require_data=False)
        assert default.timeframe == Timeframe.H4


# ---------------------------------------------------------------------------
# Status / results
# ---------------------------------------------------------------------------


_CACHED = {
    "id": "bt-task-cached",
    "strategy": "Saw",
    "strategy_id": "saw",
    "initial_capital": 10000.0,
    "period": "2026-01-01T00:00:00+00:00 - 2026-01-02T00:00:00+00:00",
    "status": "completed",
    "name": "",
    "provenance": {
        "exchange": "binance",
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "bar_count": 25,
        "first_ts": "2026-01-01T00:00:00+00:00",
        "last_ts": "2026-01-02T00:00:00+00:00",
        "gap_count": 0,
        "requested_start": "2026-01-01T00:00:00+00:00",
        "requested_end": "2026-01-02T23:59:59.999999+00:00",
        "data_source": "db",
    },
    "stopped_reason": None,
    "metrics": {
        "total_trades": 2,
        "win_rate": 50.0,
        "total_pnl": 5.0,
        "max_drawdown": 1.0,
        "sharpe_ratio": 0.5,
    },
    "equity_curve": [],
    "benchmark_equity": [],
    "trades": [
        {
            "entry_time": "2026-01-01T01:00:00+00:00",
            "exit_time": "2026-01-01T02:00:00+00:00",
            "side": "LONG",
            "entry_price": 100.0,
            "exit_price": 110.0,
            "pnl": 10.0,
        },
        {
            "entry_time": "2026-01-01T03:00:00+00:00",
            "exit_time": "2026-01-01T04:00:00+00:00",
            "side": "LONG",
            "entry_price": 110.0,
            "exit_price": 105.0,
            "pnl": -5.0,
        },
    ],
    "transactions": [],
}


class TestStatusAndResults:
    def test_status_reports_error_and_result_id(self, client: TestClient) -> None:
        _TASKS["task-x"] = {
            "task_id": "task-x",
            "status": "failed",
            "progress": 0.0,
            "error": "no market data",
        }
        resp = client.get("/api/backtest/status/task-x")
        assert resp.json() == {
            "task_id": "task-x",
            "status": "failed",
            "progress": 0.0,
            "error": "no market data",
            "result_id": None,
            "fetch_error": None,
        }

    def test_unknown_task_is_404(self, client: TestClient) -> None:
        resp = client.get("/api/backtest/status/nope")
        assert resp.status_code == 404
        assert resp.json()["detail"]["state"] == "not_found"

    def test_list_without_database_is_503(self, client: TestClient) -> None:
        _RESULTS["bt-task-cached"] = dict(_CACHED)
        resp = client.get("/api/backtest/results")
        assert resp.status_code == 503

    def test_cached_detail_carries_provenance(self, client: TestClient) -> None:
        _RESULTS["bt-task-cached"] = dict(_CACHED)
        resp = client.get("/api/backtest/results/bt-task-cached")
        assert resp.status_code == 200
        body = resp.json()
        assert body["provenance"] == _CACHED["provenance"]
        assert body["strategy_id"] == "saw"
        assert body["initial_capital"] == 10000.0

    def test_uncached_detail_without_database_is_503(self, client: TestClient) -> None:
        resp = client.get("/api/backtest/results/bt-1")
        assert resp.status_code == 503

    def test_verify_recomputes_from_trades(self, client: TestClient) -> None:
        _RESULTS["bt-task-cached"] = dict(_CACHED)
        body = client.get("/api/backtest/results/bt-task-cached/verify").json()
        assert body["computed_trade_count"] == 2
        assert body["computed_total_pnl"] == 5.0
        assert body["computed_win_rate"] == 50.0
        assert body["all_passed"] is True

    def test_rename_and_delete_without_database_are_503(self, client: TestClient) -> None:
        _RESULTS["bt-task-cached"] = dict(_CACHED)
        assert (
            client.patch("/api/backtest/results/bt-task-cached", json={"name": "x"}).status_code
            == 503
        )
        assert client.delete("/api/backtest/results/bt-task-cached").status_code == 503
        assert _RESULTS["bt-task-cached"] == _CACHED


# ---------------------------------------------------------------------------
# Strategy listing / params
# ---------------------------------------------------------------------------


class TestStrategyOptions:
    def test_lists_primary_timeframe(self, client: TestClient, strategies_dir: Path) -> None:
        _write_strategy(strategies_dir, "saw", timeframes={"primary": "4h"})
        assert client.get("/api/backtest/strategies").json() == [
            {"id": "saw", "name": "Strategy saw", "primary_timeframe": "4h"}
        ]

    def test_empty_dir_lists_nothing(self, client: TestClient) -> None:
        assert client.get("/api/backtest/strategies").json() == []

    def test_params_for_unknown_strategy_is_404(self, client: TestClient) -> None:
        resp = client.get("/api/backtest/strategies/does_not_exist_xyz/params")
        assert resp.status_code == 404
        assert resp.json()["detail"]["state"] == "not_found"

    def test_params_for_known_strategy(self, client: TestClient, strategies_dir: Path) -> None:
        _write_strategy(strategies_dir, "saw")
        names = [p["name"] for p in client.get("/api/backtest/strategies/saw/params").json()]
        assert names == ["sma_period"]

    def test_params_for_strategy_without_rules_is_empty(
        self, client: TestClient, strategies_dir: Path
    ) -> None:
        _write_strategy(strategies_dir, "plain", parameters={})
        assert client.get("/api/backtest/strategies/plain/params").json() == []


# ---------------------------------------------------------------------------
# Walk-forward window sizing comes from the real span
# ---------------------------------------------------------------------------


class _IdleStrategy(BaseStrategy):
    @property
    def required_history(self) -> int:
        return 1

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        return []


class TestWalkForwardMonths:
    def test_months_from_span(self) -> None:
        first = datetime(2025, 1, 1, tzinfo=UTC)
        last = first + timedelta(days=399)
        assert _walkforward_months(first, last, fold_count=2, train_pct=0.75) == (5, 2)
        assert _window_fits(first, last, 400, 5, 2)

    def test_thirty_days_cannot_fit_a_window(self) -> None:
        first = datetime(2025, 1, 1, tzinfo=UTC)
        last = first + timedelta(days=30)
        in_sample, out_of_sample = _walkforward_months(first, last, fold_count=2, train_pct=0.75)
        assert (in_sample, out_of_sample) == (1, 1)
        assert not _window_fits(first, last, 31, in_sample, out_of_sample)

    def test_weekly_bars_are_sized_at_their_real_density(self) -> None:
        first = datetime(2025, 1, 1, tzinfo=UTC)
        last = first + timedelta(weeks=56)
        assert _walkforward_months(first, last, fold_count=2, train_pct=0.75) == (4, 2)
        assert _window_fits(first, last, 57, 4, 2)
        assert not _window_fits(first, first + timedelta(weeks=4), 5, 1, 1)

    async def test_weekly_walkforward_windows_match_the_reported_months(self) -> None:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        bars = [
            OHLCV(
                open=Decimal(1000 + (i % 4) * 50),
                high=Decimal(1000 + (i % 4) * 50),
                low=Decimal(1000 + (i % 4) * 50),
                close=Decimal(1000 + (i % 4) * 50),
                volume=Decimal(10),
                timestamp=start + timedelta(weeks=i),
            )
            for i in range(57)
        ]
        config = StrategyConfig(id="wf-1w", name="WF 1w", strategy_class="x.Y", symbols=["BTCUSDT"])
        result = await WalkForwardAnalyzer().run_walkforward(
            bars=bars,
            strategy_class=_IdleStrategy,
            strategy_config=config,
            in_sample_months=4,
            out_of_sample_months=2,
            timeframe=Timeframe.W1,
        )
        # 57 bars over 392 days: 4 months = 17 weekly bars, 2 months = 8, stepping 8.
        assert len(result.folds) == 5
        for fold in result.folds:
            assert len(fold.in_sample_metrics.equity_curve) == 17 + 1
            assert len(fold.out_of_sample_metrics.equity_curve) == 8 + 1


# ---------------------------------------------------------------------------
# Persistence failure fails the task (happy path is in the integration test)
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


class _Pool:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


async def test_persistence_failure_fails_the_task(strategies_dir: Path) -> None:
    _write_strategy(strategies_dir, "saw")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        {
            "open": 1000 + (i % 20) * 5,
            "high": 1000 + (i % 20) * 5,
            "low": 1000 + (i % 20) * 5,
            "close": 1000 + (i % 20) * 5,
            "volume": 10,
            "timestamp": start + timedelta(hours=i),
        }
        for i in range(60)
    ]
    pool = _Pool(_BarsThenFailingWrites(rows))

    body = BacktestRunRequest(strategy_id="saw", start_date="2026-01-01", end_date="2026-01-03")
    pf = await _preflight(pool, "saw", None, body.start_date, body.end_date, require_data=False)
    _TASKS["task-p"] = {"task_id": "task-p", "status": "queued", "progress": 0.0}

    await _run_backtest_task("task-p", body, pf)

    assert _TASKS["task-p"]["status"] == "failed"
    assert _TASKS["task-p"]["error"].startswith("result not persisted")
    assert "result_id" not in _TASKS["task-p"]
    assert _RESULTS == {}


class _BarsThenRecordedWrites(_BarsThenFailingWrites):
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        super().__init__(rows)
        self.inserts: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.inserts.append((query, args))
        return "OK"

    async def executemany(self, query: str, args: Any) -> None:
        return None


async def test_curves_carry_their_own_timestamps_and_metrics_persist_as_cached(
    strategies_dir: Path,
) -> None:
    _write_strategy(strategies_dir, "saw")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    closes = [Decimal(1000 + (i % 20) * 5) for i in range(60)]
    rows = [
        {
            "open": c,
            "high": c,
            "low": c,
            "close": c,
            "volume": 10,
            "timestamp": start + timedelta(hours=i),
        }
        for i, c in enumerate(closes)
    ]
    conn = _BarsThenRecordedWrites(rows)
    body = BacktestRunRequest(strategy_id="saw", start_date="2026-01-01", end_date="2026-01-03")
    pf = await _preflight(
        _Pool(conn), "saw", None, body.start_date, body.end_date, require_data=False
    )
    _TASKS["task-t"] = {"task_id": "task-t", "status": "queued", "progress": 0.0}

    await _run_backtest_task("task-t", body, pf)

    assert _TASKS["task-t"]["status"] == "completed", _TASKS["task-t"]
    cached = _RESULTS["bt-task-t"]
    equity, bench = cached["equity_curve"], cached["benchmark_equity"]
    labels = [p["timestamp"] for p in equity]
    assert labels[-len(rows) :] == [r["timestamp"].isoformat() for r in rows]
    assert [p["timestamp"] for p in bench] == labels
    close_at = {r["timestamp"].isoformat(): r["close"] for r in rows}
    for point in bench:
        assert point["value"] == float(10000 * close_at[point["timestamp"]] / closes[0])

    query, args = conn.inserts[0]
    columns = re.search(r"INSERT INTO backtest_results\s*\(([^)]*)\)", query)
    assert columns is not None
    row = dict(zip([c.strip() for c in columns.group(1).split(",")], args, strict=True))
    assert summary_from_row(row)["metrics"] == cached["metrics"]
    assert cached["metrics"]["total_return"] == round(
        (equity[-1]["value"] / equity[0]["value"] - 1) * 100, 2
    )
    assert isinstance(cached["metrics"]["max_drawdown_duration_s"], int)


# ---------------------------------------------------------------------------
# Persisted detail: every field round-trips, nothing missing is shown as zero
# ---------------------------------------------------------------------------


def _db_row(result_id: str = "bt-task-db", **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": result_id,
        "strategy": "Saw",
        "period": "p",
        "status": "completed",
        "name": "",
        "total_trades": 3,
        "win_rate": 33.33,
        "total_pnl": -950.0,
        "max_drawdown": 95.0,
        "sharpe_ratio": -2.1,
        "benchmark_return": 4.2,
        "alpha": -0.31,
        "beta": 1.7,
        "max_consecutive_wins": 1,
        "max_consecutive_losses": 2,
        "expectancy": -316.67,
        "total_return": -95.0,
        "annualized_return": -100.0,
        "sortino_ratio": -3.1,
        "calmar_ratio": None,
        "profit_factor": 0.5,
        "max_drawdown_duration_s": 7200,
        "stopped_reason": "margin_call",
        "strategy_id": "saw",
        "initial_capital": 1000.0,
        "data_source": "db",
        "data_exchange": "binance",
        "data_symbol": "BTCUSDT",
        "data_timeframe": "1h",
        "bar_count": 25,
        "first_ts": datetime(2026, 1, 1, tzinfo=UTC),
        "last_ts": datetime(2026, 1, 2, tzinfo=UTC),
        "gap_count": 0,
        "requested_start": datetime(2026, 1, 1, tzinfo=UTC),
        "requested_end": datetime(2026, 1, 2, tzinfo=UTC),
        "equity_curve": '[{"timestamp": "2026-01-01T00:00:00+00:00", "value": 1000.0}]',
        "benchmark_equity": '[{"timestamp": "2026-01-01T00:00:00+00:00", "value": 1000.0}]',
        "transactions": "[]",
    }
    row.update(overrides)
    return row


class TestPersistedDetail:
    def test_stopped_reason_benchmark_and_extended_metrics_round_trip(self) -> None:
        detail = BacktestResultDetail(**detail_from_row(_db_row(), []))
        assert detail.stopped_reason == "margin_call"
        assert detail.benchmark_equity == [
            {"timestamp": "2026-01-01T00:00:00+00:00", "value": 1000.0}
        ]
        m = detail.metrics
        assert (m.benchmark_return, m.alpha, m.beta) == (4.2, -0.31, 1.7)
        assert (m.max_consecutive_wins, m.max_consecutive_losses) == (1, 2)
        assert m.expectancy == -316.67
        assert (m.total_return, m.annualized_return) == (-95.0, -100.0)
        assert (m.sortino_ratio, m.calmar_ratio, m.profit_factor) == (-3.1, None, 0.5)
        assert m.max_drawdown_duration_s == 7200

    def test_unrecorded_extended_metrics_are_null_not_zero(self) -> None:
        row = _db_row(
            benchmark_return=None,
            alpha=None,
            beta=None,
            max_consecutive_wins=None,
            max_consecutive_losses=None,
            expectancy=None,
            stopped_reason=None,
        )
        metrics = BacktestResultDetail(**detail_from_row(row, [])).model_dump()["metrics"]
        for name in ("benchmark_return", "alpha", "beta", "expectancy"):
            assert metrics[name] is None
        assert metrics["max_consecutive_losses"] is None


# ---------------------------------------------------------------------------
# List / rename / delete go to the database first
# ---------------------------------------------------------------------------


class _ResultsConn:
    def __init__(self, rows: dict[str, dict[str, Any]], *, down: bool = False) -> None:
        self.rows = rows
        self.down = down
        self.queries: list[str] = []

    def _check(self, query: str) -> None:
        self.queries.append(query)
        if self.down:
            raise OSError("database unreachable")

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self._check(query)
        return list(self.rows.values())

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self._check(query)
        name, result_id = args
        row = self.rows.get(result_id)
        if row is not None:
            row["name"] = name
        return row

    async def fetchval(self, query: str, *args: Any) -> str | None:
        self._check(query)
        removed = self.rows.pop(args[0], None)
        return None if removed is None else str(removed["id"])


def _results_client(conn: _ResultsConn) -> TestClient:
    return TestClient(_make_app(pool=_Pool(conn)))


class TestResultMutations:
    def test_delete_during_outage_is_503_and_keeps_row_and_cache(self) -> None:
        conn = _ResultsConn({"bt-task-db": _db_row()}, down=True)
        _RESULTS["bt-task-db"] = dict(_CACHED, id="bt-task-db")
        client = _results_client(conn)

        resp = client.delete("/api/backtest/results/bt-task-db")

        assert resp.status_code == 503
        assert resp.json()["detail"]["state"] == "unavailable"
        assert "bt-task-db" in conn.rows
        assert "bt-task-db" in _RESULTS

        conn.down = False
        assert client.delete("/api/backtest/results/bt-task-db").status_code == 204
        assert conn.rows == {}
        assert "bt-task-db" not in _RESULTS

    def test_uncached_row_can_be_renamed_and_deleted(self) -> None:
        conn = _ResultsConn({"bt-task-db": _db_row()})
        client = _results_client(conn)

        renamed = client.patch("/api/backtest/results/bt-task-db", json={"name": "Renamed"})
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Renamed"
        assert renamed.json()["provenance"]["bar_count"] == 25
        assert conn.rows["bt-task-db"]["name"] == "Renamed"

        assert client.delete("/api/backtest/results/bt-task-db").status_code == 204
        assert conn.rows == {}

    def test_rename_updates_the_cached_copy(self) -> None:
        conn = _ResultsConn({"bt-task-db": _db_row()})
        _RESULTS["bt-task-db"] = dict(_CACHED, id="bt-task-db")
        resp = _results_client(conn).patch(
            "/api/backtest/results/bt-task-db", json={"name": "Renamed"}
        )
        assert resp.status_code == 200
        assert _RESULTS["bt-task-db"]["name"] == "Renamed"

    def test_missing_row_is_404_envelope(self) -> None:
        client = _results_client(_ResultsConn({}))
        for resp in (
            client.delete("/api/backtest/results/bt-gone"),
            client.patch("/api/backtest/results/bt-gone", json={"name": "x"}),
        ):
            assert resp.status_code == 404
            assert resp.json()["detail"]["code"] == "result_not_found"

    def test_list_reads_summaries_without_curves(self) -> None:
        conn = _ResultsConn({"bt-task-db": _db_row()})
        resp = _results_client(conn).get("/api/backtest/results")
        assert resp.status_code == 200
        assert resp.json()[0]["metrics"]["max_consecutive_losses"] == 2
        (query,) = conn.queries
        select_list = query.split(" FROM ")[0]
        for column in ("equity_curve", "benchmark_equity", "transactions"):
            assert column not in select_list
