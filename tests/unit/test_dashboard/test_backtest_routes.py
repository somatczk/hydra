"""Unit tests for the backtest routes: strategy resolution, pre-flight errors, state handling.

The happy path (real bars in, provenance and trades out) runs against TimescaleDB in
tests/integration/test_backtest_real_data.py.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hydra.backtest.hyperopt import ParamDef, ParameterSpace
from hydra.backtest.jobs import (
    MAX_RESULT_SUMMARIES,
    BacktestRunRequest,
    detail_from_row,
    walkforward_months,
    window_fits,
)
from hydra.backtest.walkforward import WalkForwardAnalyzer
from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import OHLCV, Direction, Timeframe
from hydra.dashboard.routes._db import install_error_handlers
from hydra.dashboard.routes.backtest import router
from hydra.services.backtest import (
    BacktestResultDetail,
    _load_backtest_strategy,
    _preflight,
)
from hydra.services.errors import ServiceError
from hydra.services.research import HyperoptProgressResponse
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


def _walkforward_body(strategy_id: str = "saw", **overrides: Any) -> dict[str, Any]:
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
            ("/api/backtest/walkforward", _walkforward_body("does_not_exist_xyz")),
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

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            ("/api/backtest/run", _run_body()),
            ("/api/backtest/hyperopt", _hyperopt_body()),
            ("/api/backtest/walkforward", _walkforward_body()),
        ],
    )
    def test_no_database_is_503_and_queues_nothing(
        self, client: TestClient, strategies_dir: Path, path: str, body: dict[str, Any]
    ) -> None:
        _write_strategy(strategies_dir, "saw")
        resp = client.post(path, json=body)
        assert resp.status_code == 503
        assert resp.json()["detail"]["state"] == "unavailable"

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

    def test_walkforward_without_param_space_is_422(self, client: TestClient) -> None:
        resp = client.post("/api/backtest/walkforward", json=_run_body())
        assert resp.status_code == 422
        assert "param_space" in resp.json()["detail"]["message"]

    def test_walkforward_bad_param_space_is_422(
        self, client: TestClient, strategies_dir: Path
    ) -> None:
        _write_strategy(strategies_dir, "saw")
        resp = client.post(
            "/api/backtest/walkforward",
            json=_walkforward_body(param_space=[{"name": "x", "type": "int"}]),
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "invalid_param_space"

    def test_hyperopt_unknown_param_name_is_422(
        self, client: TestClient, strategies_dir: Path
    ) -> None:
        _write_strategy(strategies_dir, "saw")
        resp = client.post(
            "/api/backtest/hyperopt",
            json=_hyperopt_body(
                param_space=[{"name": "sma_perod", "type": "int", "low": 3, "high": 8}]
            ),
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["code"] == "invalid_param_space"
        assert "sma_perod" in detail["message"]

    def test_walkforward_unknown_param_name_is_422(
        self, client: TestClient, strategies_dir: Path
    ) -> None:
        _write_strategy(strategies_dir, "saw")
        resp = client.post(
            "/api/backtest/walkforward",
            json=_walkforward_body(
                param_space=[{"name": "sma_perod", "type": "int", "low": 3, "high": 8}]
            ),
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["code"] == "invalid_param_space"
        assert "sma_perod" in detail["message"]

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


class _JobRowConn:
    """Serves one backtest_jobs row for the status read."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        assert "FROM backtest_jobs" in query
        return self.row


def _job_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "task-x",
        "kind": "backtest",
        "status": "failed",
        "progress": 0.0,
        "error": "no market data",
        "result_id": None,
        "detail": "{}",
    }
    row.update(overrides)
    return row


class _ResultDetailConn:
    """Serves one backtest_results row plus its trades for the detail and verify reads."""

    def __init__(self, row: dict[str, Any] | None, trades: list[dict[str, Any]]) -> None:
        self.row = row
        self.trades = trades

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        assert "FROM backtest_results WHERE id = $1" in query
        return self.row

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        assert "FROM backtest_result_trades" in query
        return self.trades


_DETAIL_TRADES = [
    {
        "entry_time": datetime(2026, 1, 1, 1, tzinfo=UTC),
        "exit_time": datetime(2026, 1, 1, 2, tzinfo=UTC),
        "side": "LONG",
        "entry_price": 100.0,
        "exit_price": 110.0,
        "pnl": 10.0,
    },
    {
        "entry_time": datetime(2026, 1, 1, 3, tzinfo=UTC),
        "exit_time": datetime(2026, 1, 1, 4, tzinfo=UTC),
        "side": "LONG",
        "entry_price": 110.0,
        "exit_price": 105.0,
        "pnl": -5.0,
    },
]

_HYPEROPT_RESULT = {
    "best_params": {"sma_period": 5},
    "best_metric": 1.25,
    "trials": [
        {
            "trial_number": 0,
            "params": {"sma_period": 4},
            "sharpe": 0.5,
            "total_return": 0.02,
            "max_drawdown": 0.01,
            "total_trades": 6,
            "status": "ok",
            "error": None,
        },
        {
            "trial_number": 1,
            "params": {"sma_period": 5},
            "sharpe": 1.25,
            "total_return": 0.05,
            "max_drawdown": 0.01,
            "total_trades": 9,
            "status": "ok",
            "error": None,
        },
        {
            "trial_number": 2,
            "params": {"sma_period": 6},
            "sharpe": 0.0,
            "total_return": 0.0,
            "max_drawdown": 0.0,
            "total_trades": 0,
            "status": "no_trades",
            "error": "strategy made no trades",
        },
    ],
    "total_trials": 3,
    "completed_trials": 3,
    "n_trials": 2,
    "failed_trials": 0,
    "seed": 42,
    "train_start": "2026-01-01T00:00:00+00:00",
    "train_end": "2026-01-24T00:00:00+00:00",
    "holdout_start": "2026-01-25T00:00:00+00:00",
    "holdout_end": "2026-01-31T00:00:00+00:00",
    "holdout": {"sharpe": 0.9, "total_return": 0.01, "max_drawdown": 0.005, "total_trades": 2},
    "holdout_psr": 0.88,
    "dsr": 0.77,
    "pbo": None,
    "provenance": {"exchange": "binance"},
}


class TestStatusAndResults:
    def test_status_reports_error_and_result_id(self) -> None:
        client = TestClient(_make_app(pool=_Pool(_JobRowConn(_job_row()))))
        resp = client.get("/api/backtest/status/task-x")
        assert resp.json() == {
            "task_id": "task-x",
            "status": "failed",
            "progress": 0.0,
            "error": "no market data",
            "result_id": None,
            "fetch_error": None,
        }

    def test_status_reports_fetch_error_from_the_detail(self) -> None:
        row = _job_row(detail='{"fetch_error": "backfill failed"}')
        client = TestClient(_make_app(pool=_Pool(_JobRowConn(row))))
        resp = client.get("/api/backtest/status/task-x")
        assert resp.json()["fetch_error"] == "backfill failed"

    def test_hyperopt_progress_created_at_stays_optional(self) -> None:
        """D1's OpenAPI shape is unchanged: created_at is always served, never required."""
        assert "created_at" not in HyperoptProgressResponse.model_json_schema()["required"]

    def test_hyperopt_results_carry_status_holdout_and_validation(self) -> None:
        """The stored payload round-trips: per-trial status, the holdout, the DSR/PBO."""
        row = {
            "id": "ho-x",
            "kind": "hyperopt",
            "status": "completed",
            "error": None,
            "detail": "{}",
            "result": json.dumps(_HYPEROPT_RESULT),
        }
        client = TestClient(_make_app(pool=_Pool(_JobRowConn(row))))
        resp = client.get("/api/backtest/hyperopt/ho-x/results")
        assert resp.status_code == 200
        body = resp.json()
        assert [t["status"] for t in body["trials"]] == ["ok", "ok", "no_trades"]
        assert body["trials"][2]["error"] == "strategy made no trades"
        assert body["n_trials"] == 2
        assert body["failed_trials"] == 0
        assert body["seed"] == 42
        assert body["holdout"] == {
            "sharpe": 0.9,
            "total_return": 0.01,
            "max_drawdown": 0.005,
            "total_trades": 2,
        }
        assert body["holdout_psr"] == 0.88
        assert body["dsr"] == 0.77
        assert body["pbo"] is None
        assert body["train_start"].startswith("2026-01-01T00:00:00")
        assert body["holdout_start"].startswith("2026-01-25T00:00:00")
        assert body["best_metric"] == 1.25

    def test_unknown_task_is_404(self) -> None:
        client = TestClient(_make_app(pool=_Pool(_JobRowConn(None))))
        resp = client.get("/api/backtest/status/nope")
        assert resp.status_code == 404
        assert resp.json()["detail"]["state"] == "not_found"

    def test_list_without_database_is_503(self, client: TestClient) -> None:
        resp = client.get("/api/backtest/results")
        assert resp.status_code == 503

    def test_detail_carries_provenance(self) -> None:
        client = TestClient(
            _make_app(pool=_Pool(_ResultDetailConn(_db_row("bt-task-db"), _DETAIL_TRADES)))
        )
        resp = client.get("/api/backtest/results/bt-task-db")
        assert resp.status_code == 200
        body = resp.json()
        assert body["provenance"]["bar_count"] == 25
        assert body["strategy_id"] == "saw"
        assert body["initial_capital"] == 1000.0
        assert [t["pnl"] for t in body["trades"]] == [10.0, -5.0]

    def test_detail_without_database_is_503(self, client: TestClient) -> None:
        resp = client.get("/api/backtest/results/bt-1")
        assert resp.status_code == 503

    def test_verify_recomputes_from_trades(self) -> None:
        row = _db_row("bt-task-db", total_trades=2, win_rate=50.0, total_pnl=5.0, max_drawdown=1.0)
        client = TestClient(_make_app(pool=_Pool(_ResultDetailConn(row, _DETAIL_TRADES))))
        body = client.get("/api/backtest/results/bt-task-db/verify").json()
        assert body["computed_trade_count"] == 2
        assert body["computed_total_pnl"] == 5.0
        assert body["computed_win_rate"] == 50.0
        assert body["all_passed"] is True

    def test_rename_and_delete_without_database_are_503(self, client: TestClient) -> None:
        assert client.patch("/api/backtest/results/bt-x", json={"name": "x"}).status_code == 503
        assert client.delete("/api/backtest/results/bt-x").status_code == 503


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


class _AlternatingStrategy(BaseStrategy):
    """Enters when flat and exits on the next bar, so every window trades."""

    @property
    def required_history(self) -> int:
        return 1

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        if self.context.position(str(bar.symbol)) is None:
            return [
                EntrySignal(
                    symbol=bar.symbol,
                    direction=Direction.LONG,
                    strength=Decimal("1"),
                    strategy_id=self.strategy_id,
                )
            ]
        return [ExitSignal(symbol=bar.symbol, strategy_id=self.strategy_id)]


class TestWalkForwardMonths:
    def test_months_from_span(self) -> None:
        first = datetime(2025, 1, 1, tzinfo=UTC)
        last = first + timedelta(days=399)
        assert walkforward_months(first, last, fold_count=2, train_pct=0.75) == (5, 2)
        assert window_fits(first, last, 400, 5, 2)

    def test_thirty_days_cannot_fit_a_window(self) -> None:
        first = datetime(2025, 1, 1, tzinfo=UTC)
        last = first + timedelta(days=30)
        in_sample, out_of_sample = walkforward_months(first, last, fold_count=2, train_pct=0.75)
        assert (in_sample, out_of_sample) == (1, 1)
        assert not window_fits(first, last, 31, in_sample, out_of_sample)

    def test_weekly_bars_are_sized_at_their_real_density(self) -> None:
        first = datetime(2025, 1, 1, tzinfo=UTC)
        last = first + timedelta(weeks=56)
        assert walkforward_months(first, last, fold_count=2, train_pct=0.75) == (4, 2)
        assert window_fits(first, last, 57, 4, 2)
        assert not window_fits(first, first + timedelta(weeks=4), 5, 1, 1)

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
        config = StrategyConfig(
            id="wf-1w",
            name="WF 1w",
            strategy_class="x.Y",
            symbols=["BTCUSDT"],
            parameters={"level": 1},
        )
        result = await WalkForwardAnalyzer().run_walkforward(
            bars=bars,
            strategy_class=_AlternatingStrategy,
            strategy_config=config,
            param_space=ParameterSpace(
                params=[ParamDef(name="level", type="categorical", choices=[1])]
            ),
            in_sample_months=4,
            out_of_sample_months=2,
            timeframe=Timeframe.W1,
        )
        # 57 bars over 392 days: 4 months = 17 weekly bars, 2 months = 8, stepping 8.
        assert len(result.folds) == 5
        for fold in result.folds:
            assert len(fold.in_sample_metrics.equity_curve) == 17
            assert len(fold.out_of_sample_metrics.equity_curve) == 8
            assert fold.selected_params == {"level": 1}
            assert fold.n_trials == 1
            assert fold.out_of_sample_metrics.total_trades >= 1


# ---------------------------------------------------------------------------
# Fake pool for the row-serving connections below
# ---------------------------------------------------------------------------


class _Acquire:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Pool:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


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
        "stopped_reason": "paper ledger halted",
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
        "created_at": datetime(2026, 1, 2, 12, tzinfo=UTC),
        "equity_curve": '[{"timestamp": "2026-01-01T00:00:00+00:00", "value": 1000.0}]',
        "benchmark_equity": '[{"timestamp": "2026-01-01T00:00:00+00:00", "value": 1000.0}]',
        "transactions": "[]",
    }
    row.update(overrides)
    return row


class TestPersistedDetail:
    def test_stopped_reason_benchmark_and_extended_metrics_round_trip(self) -> None:
        detail = BacktestResultDetail(**detail_from_row(_db_row(), []))
        assert detail.stopped_reason == "paper ledger halted"
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
    """Serves backtest_results rows; the list read emulates the SELECT faithfully:
    newest first, and a query that carries LIMIT never yields more rows than its bound."""

    def __init__(self, rows: dict[str, dict[str, Any]], *, down: bool = False) -> None:
        self.rows = rows
        self.down = down
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def _check(self, query: str) -> None:
        if self.down:
            raise OSError("database unreachable")

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self._check(query)
        assert "FROM backtest_results" in query
        assert "LIMIT" in query
        self.calls.append((query, args))
        newest_first = sorted(self.rows.values(), key=lambda row: row["created_at"], reverse=True)
        return newest_first[: args[0]]

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
    def test_delete_during_outage_is_503_and_keeps_the_row(self) -> None:
        conn = _ResultsConn({"bt-task-db": _db_row()}, down=True)
        client = _results_client(conn)

        resp = client.delete("/api/backtest/results/bt-task-db")

        assert resp.status_code == 503
        assert resp.json()["detail"]["state"] == "unavailable"
        assert "bt-task-db" in conn.rows

        conn.down = False
        assert client.delete("/api/backtest/results/bt-task-db").status_code == 204
        assert conn.rows == {}

    def test_a_row_can_be_renamed_and_deleted(self) -> None:
        conn = _ResultsConn({"bt-task-db": _db_row()})
        client = _results_client(conn)

        renamed = client.patch("/api/backtest/results/bt-task-db", json={"name": "Renamed"})
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Renamed"
        assert renamed.json()["provenance"]["bar_count"] == 25
        assert conn.rows["bt-task-db"]["name"] == "Renamed"

        assert client.delete("/api/backtest/results/bt-task-db").status_code == 204
        assert conn.rows == {}

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
        ((query, _limit),) = conn.calls
        select_list = query.split(" FROM ")[0]
        for column in ("equity_curve", "benchmark_equity", "transactions"):
            assert column not in select_list


def _seeded_results(total: int) -> dict[str, dict[str, Any]]:
    """Result rows with distinct created_at stamps, oldest first."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    return {
        f"bt-{i}": _db_row(f"bt-{i}", created_at=base + timedelta(hours=i)) for i in range(total)
    }


class TestResultListBounding:
    """A6 (audit #174): the shared results read stops at the cap, newest first."""

    def test_the_read_stops_at_the_cap(self) -> None:
        total = MAX_RESULT_SUMMARIES + 30
        conn = _ResultsConn(_seeded_results(total))
        resp = _results_client(conn).get("/api/backtest/results")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == MAX_RESULT_SUMMARIES
        assert [s["id"] for s in body] == [f"bt-{i}" for i in range(total - 1, 29, -1)]
        ((query, args),) = conn.calls
        assert "LIMIT" in query
        assert args == (MAX_RESULT_SUMMARIES,)

    def test_a_realistic_count_returns_every_row_newest_first(self) -> None:
        conn = _ResultsConn(_seeded_results(7))
        resp = _results_client(conn).get("/api/backtest/results")
        assert resp.status_code == 200
        assert [s["id"] for s in resp.json()] == [f"bt-{i}" for i in range(6, -1, -1)]


# ---------------------------------------------------------------------------
# The durable jobs list
# ---------------------------------------------------------------------------

JOB_LIST_FIELDS = {
    "task_id",
    "kind",
    "status",
    "progress",
    "created_at",
    "finished_at",
    "strategy_id",
    "name",
    "error",
    "result_id",
    "completed_trials",
    "total_trials",
}


class _JobsConn:
    """Serves backtest_jobs rows per kind for the jobs-list read.

    Emulates the SELECT faithfully: newest first per kind, and a query that
    carries LIMIT never yields more rows than its bound.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.kinds: list[str] = []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.served: list[int] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        assert "FROM backtest_jobs" in query
        self.calls.append((query, args))
        self.kinds.append(args[0])
        matching = sorted(
            (r for r in self.rows if r["kind"] == args[0]),
            key=lambda r: r["created_at"],
            reverse=True,
        )
        if "LIMIT" in query:
            matching = matching[: args[1]]
        self.served.append(len(matching))
        return matching


def _jobs_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "task-x",
        "kind": "backtest",
        "status": "running",
        "progress": 42.0,
        "error": None,
        "result_id": None,
        "request": '{"strategy_id": "saw", "name": "Q1 run"}',
        "detail": "{}",
        "created_at": datetime(2026, 10, 25, 12, tzinfo=UTC),
        "finished_at": None,
    }
    row.update(overrides)
    return row


def _jobs_client(conn: _JobsConn) -> TestClient:
    return TestClient(_make_app(pool=_Pool(conn)))


class TestJobListRoute:
    def test_lists_every_kind_newest_first_with_hyperopt_trials(self) -> None:
        conn = _JobsConn(
            [
                _jobs_row(id="task-old", created_at=datetime(2026, 10, 24, tzinfo=UTC)),
                _jobs_row(
                    id="ho-1",
                    kind="hyperopt",
                    status="completed",
                    progress=100.0,
                    created_at=datetime(2026, 10, 25, 10, tzinfo=UTC),
                    finished_at=datetime(2026, 10, 25, 13, tzinfo=UTC),
                    request=(
                        '{"strategy_id": "saw", "name": "ho", "max_trials": 50, '
                        '"start_date": "2026-01-01", "end_date": "2026-02-01", '
                        '"param_space": [{"name": "period", "type": "int", "low": 2, "high": 80}], '
                        '"method": "bayesian"}'
                    ),
                    detail='{"completed_trials": 37, "total_trials": 50}',
                ),
                _jobs_row(
                    id="wf-1",
                    kind="walkforward",
                    status="failed",
                    error="fold 3 made no trades",
                    finished_at=datetime(2026, 10, 25, 14, tzinfo=UTC),
                ),
                _jobs_row(
                    id="task-new",
                    status="queued",
                    created_at=datetime(2026, 10, 26, tzinfo=UTC),
                ),
            ]
        )
        resp = _jobs_client(conn).get("/api/backtest/jobs")
        assert resp.status_code == 200
        body = resp.json()
        assert [e["task_id"] for e in body] == ["task-new", "wf-1", "ho-1", "task-old"]
        assert conn.kinds == ["backtest", "hyperopt", "walkforward"]
        assert body[0] == {
            "task_id": "task-new",
            "kind": "backtest",
            "status": "queued",
            "progress": 42.0,
            "created_at": "2026-10-26T00:00:00+00:00",
            "finished_at": None,
            "strategy_id": "saw",
            "name": "Q1 run",
            "error": None,
            "result_id": None,
            "completed_trials": None,
            "total_trials": None,
        }
        hyperopt = body[2]
        assert hyperopt["completed_trials"] == 37
        assert hyperopt["total_trials"] == 50
        # pinned seam: the request/strategy_config JSONB never crosses the wire
        assert all(set(e) == JOB_LIST_FIELDS for e in body)
        assert all("param_space" not in json.dumps(e) for e in body)

    def test_kind_filter_reads_only_that_kind(self) -> None:
        conn = _JobsConn([_jobs_row(), _jobs_row(id="ho-1", kind="hyperopt")])
        resp = _jobs_client(conn).get("/api/backtest/jobs?kind=hyperopt")
        assert resp.status_code == 200
        assert [e["task_id"] for e in resp.json()] == ["ho-1"]
        assert conn.kinds == ["hyperopt"]

    def test_limit_is_enforced_and_bounded(self) -> None:
        rows = [
            _jobs_row(id=f"task-{i}", created_at=datetime(2026, 10, 20 + i, tzinfo=UTC))
            for i in range(3)
        ]
        client = _jobs_client(_JobsConn(rows))
        assert [e["task_id"] for e in client.get("/api/backtest/jobs?limit=2").json()] == [
            "task-2",
            "task-1",
        ]
        assert client.get("/api/backtest/jobs?limit=501").status_code == 422

    def test_per_kind_reads_are_bounded_in_sql(self) -> None:
        """Each per-kind SELECT carries the limit; Python only keeps the cross-kind order."""
        rows = [
            _jobs_row(id=f"bt-{i}", created_at=datetime(2026, 10, 20 + i, tzinfo=UTC))
            for i in range(3)
        ] + [
            _jobs_row(id="ho-1", kind="hyperopt", created_at=datetime(2026, 10, 25, 9, tzinfo=UTC))
        ]
        conn = _JobsConn(rows)
        resp = _jobs_client(conn).get("/api/backtest/jobs?limit=2")
        assert resp.status_code == 200
        assert [e["task_id"] for e in resp.json()] == ["ho-1", "bt-2"]
        for query, args in conn.calls:
            assert "LIMIT" in query
            assert args[1] == 2
        assert conn.served == [2, 1, 0]

        default_conn = _JobsConn(rows)
        assert _jobs_client(default_conn).get("/api/backtest/jobs").status_code == 200
        for _, args in default_conn.calls:
            assert args[1] == 100

    def test_empty_table_is_an_empty_list(self) -> None:
        resp = _jobs_client(_JobsConn([])).get("/api/backtest/jobs")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_without_database_is_503(self, client: TestClient) -> None:
        resp = client.get("/api/backtest/jobs")
        assert resp.status_code == 503
        assert resp.json()["detail"]["state"] == "unavailable"
