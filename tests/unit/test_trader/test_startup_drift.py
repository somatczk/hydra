"""The trader's startup drift measurement (P5-D), on a fixture model over seeded bars.

The whole task runs for real — registry scan, training-job provenance, the
DatasetBuilder read, the quantile-PSI against the stored reference deciles,
the drift write and the gauges — with only the database canned: the fake pool
answers the exact SQL the real code issues, so the PSI numbers asserted here
are the numbers the trader would publish. Startup-only wiring is pinned on
the AST: one awaited call between ``mgr.startup()`` and ``_serve``, never
inside a loop, and ``SessionManager.startup`` never measures drift.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from prometheus_client import REGISTRY

from hydra.core import metrics
from hydra.core.types import OHLCV, Timeframe
from hydra.ml.features import DEFAULT_FEATURE_SPEC, FeatureSpec, reference_deciles
from hydra.ml.retraining import (
    PSI_ALERT_THRESHOLD,
    PSI_RETRAIN_THRESHOLD,
    feature_psi,
)
from hydra.trader import main as trader_main

T0 = datetime(2026, 2, 2, 10, tzinfo=UTC)
H = timedelta(hours=1)

SPEC = FeatureSpec(
    spec_version=DEFAULT_FEATURE_SPEC.spec_version,
    horizon=4,
    window_1h=1200,
    window_4h=400,
    window_1d=120,
    feature_names=DEFAULT_FEATURE_SPEC.feature_names,
)
"""A bounded spec on the pinned feature names (the job's own request shape). The
higher-TF windows reach far enough back that every feature is warm across the
whole matrix, like the default spec does over its 2000-bar read."""

_WALK_HOURS = 1200 + 24 * 120
"""One underlying hourly series per market: long enough to carry the 1200-bar 1h
window plus the 120-day 1d and 400-bar 4h histories ending at the same instant."""


def _stationary_closes(n: int, seed: int) -> np.ndarray:
    """A mean-reverting level: every window draws the same feature distributions."""
    rng = np.random.default_rng(seed)
    x = np.zeros(n)
    for i in range(1, n):
        x[i] = 0.98 * x[i - 1] + rng.normal(0.0, 0.002)
    return 100.0 * np.exp(x)


def _ramp_closes(n: int) -> np.ndarray:
    """A monotonic +0.5%-per-bar ramp: trend features land far outside a flat reference."""
    return 100.0 * np.power(1.005, np.arange(n))


def _oscillating_closes(n: int) -> np.ndarray:
    """A strict +/-1% alternation: the two-point reference the ramp blows past."""
    return 100.0 * np.cumprod(np.where(np.arange(n) % 2 == 0, 1.01, 0.99))


def _market(closes: np.ndarray, *, seed: int = 42) -> tuple[list[OHLCV], list[OHLCV], list[OHLCV]]:
    """One hourly close series served as the 1h/4h/1d streams the builder reads.

    All three grids end at the same instant; the higher-TF streams are sampled
    off the same series, so every feature is warm across the whole matrix.
    """
    rng = np.random.default_rng(seed)
    volumes = 10.0 + rng.uniform(0.0, 10.0, len(closes))
    hourly = _bars_from_closes(closes, volumes)
    end = hourly[-1].timestamp

    def _sampled(step_hours: int, count: int) -> list[OHLCV]:
        step = timedelta(hours=step_hours)
        end_idx = len(hourly) - 1
        bars = []
        for k in range(count - 1, -1, -1):
            b = hourly[end_idx - k * step_hours]
            bars.append(
                OHLCV(
                    open=b.open,
                    high=b.high,
                    low=b.low,
                    close=b.close,
                    volume=b.volume,
                    timestamp=end - k * step,
                )
            )
        return bars

    return hourly[-SPEC.window_1h :], _sampled(4, SPEC.window_4h), _sampled(24, SPEC.window_1d)


def _bars_from_closes(closes: np.ndarray, volumes: np.ndarray) -> list[OHLCV]:
    bars: list[OHLCV] = []
    for i, close in enumerate(closes):
        open_ = closes[i - 1] if i else float(close) / 1.001
        high = max(open_, float(close)) * 1.001
        low = min(open_, float(close)) * 0.999
        bars.append(
            OHLCV(
                open=Decimal(str(round(open_, 8))),
                high=Decimal(str(round(high, 8))),
                low=Decimal(str(round(low, 8))),
                close=Decimal(str(round(float(close), 8))),
                volume=Decimal(str(round(float(volumes[i]), 8))),
                timestamp=T0 + i * H,
            )
        )
    return bars


def _reference_quantiles(
    market: tuple[list[OHLCV], list[OHLCV], list[OHLCV]],
) -> tuple[tuple[float, ...] | None, ...]:
    """The deciles the trainer stores: one per feature, off the same builder path."""
    from hydra.ml.features import build_features

    matrix = build_features(SPEC, *market)
    return tuple(reference_deciles(matrix.features[:, i]) for i in range(matrix.features.shape[1]))


def _model_row(
    name: str,
    *,
    training_job_id: str | None,
    reference_quantiles: tuple[tuple[float, ...] | None, ...] | None,
) -> dict[str, Any]:
    spec = dict(SPEC.to_dict())
    spec["reference_quantiles"] = (
        None
        if reference_quantiles is None
        else [list(q) if q is not None else None for q in reference_quantiles]
    )
    return {
        "id": f"00000000-0000-0000-0000-{len(name):012d}",
        "name": name,
        "version": 1,
        "stage": "production",
        "model_type": "xgboost",
        "metrics": {"oos_accuracy": 0.51},
        "feature_spec": json.dumps(spec),
        "drift": None,
        "artifact_file": f"{name}.onnx",
        "artifact_sha256": "0" * 64,
        "trained_at": T0,
        "created_by": f"job:{training_job_id}" if training_job_id else "upload:dev",
        "training_job_id": training_job_id,
        "archived_at": None,
    }


def _job_row(symbol: str = "BTCUSDT", exchange: str = "binance") -> dict[str, Any]:
    return {
        "id": "task-drift-0001",
        "kind": "ml_train",
        "request": json.dumps({"name": "alpha", "strategy_id": "saw", "symbol": symbol}),
        "strategy_config": json.dumps({"exchange": {"exchange_id": exchange}}),
    }


class _FakeConn:
    """Answers the drift task's SQL from seeded rows; records the drift writes."""

    def __init__(
        self,
        models: list[dict[str, Any]],
        jobs: dict[str, dict[str, Any]],
        bars: dict[tuple[str, str, str], list[dict[str, Any]]],
    ) -> None:
        self._models = models
        self._jobs = jobs
        self._bars = bars
        self.drift_writes: list[tuple[str, int, dict[str, Any]]] = []
        self.fail_job_ids: set[str] = set()

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM ml_models WHERE stage = 'production'" in query:
            return self._models
        if "GROUP BY source" in query:
            key = (str(args[0]), str(args[1]), str(args[2]))
            return [{"source": "feed", "n": len(self._bars.get(key, []))}]
        if "FROM ts.ohlcv_1m" in query:
            # the SQL reads ORDER BY timestamp DESC LIMIT n; the rows are already newest-first
            key = (str(args[0]), str(args[1]), str(args[2]))
            return self._bars.get(key, [])[: int(args[4])]
        raise AssertionError(f"unexpected fetch: {query[:60]!r} {args!r}")

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "FROM backtest_jobs WHERE id = $1 AND kind = $2" in query:
            if str(args[0]) in self.fail_job_ids:
                raise RuntimeError("the job row exploded")
            return self._jobs.get(str(args[0]))
        if "UPDATE ml_models SET drift" in query:
            drift = json.loads(str(args[2]))
            self.drift_writes.append((str(args[0]), int(args[1]), drift))
            row = next(m for m in self._models if m["name"] == args[0])
            return {**row, "drift": json.dumps(drift)}
        if "SELECT MIN(timestamp)" in query:
            return None
        raise AssertionError(f"unexpected fetchrow: {query[:60]!r} {args!r}")


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> Any:
        return self

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


def _bar_rows(bars: list[OHLCV]) -> list[dict[str, Any]]:
    return [
        {
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
            "timestamp": b.timestamp,
        }
        for b in reversed(bars)  # get_latest_bars reads DESC and reverses
    ]


def _bars_table(
    market: tuple[list[OHLCV], list[OHLCV], list[OHLCV]],
    symbol: str = "BTCUSDT",
    exchange: str = "binance",
) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    table: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for tf, bars in zip((Timeframe.H1, Timeframe.H4, Timeframe.D1), market, strict=True):
        table[(exchange, symbol, str(tf))] = _bar_rows(bars)
    return table


@pytest.fixture(autouse=True)
def _fresh_registry() -> Iterator[None]:
    metrics._reset_metrics()
    yield
    metrics._reset_metrics()


def _gauge(name: str, model_name: str) -> float | None:
    return REGISTRY.get_sample_value(name, {"model_name": model_name})


# ---------------------------------------------------------------------------
# The measurement: fixture model + seeded bars -> the PSI on the row and gauge
# ---------------------------------------------------------------------------


class TestStartupDriftMeasurement:
    async def test_a_stationary_market_scores_low_and_writes_the_row(self) -> None:
        """The reference the trainer stored is the same market the bars carry: PSI low."""
        walk = _market(_stationary_closes(_WALK_HOURS, seed=42))
        quantiles = _reference_quantiles(walk)
        conn = _FakeConn(
            [_model_row("alpha", training_job_id="task-drift-0001", reference_quantiles=quantiles)],
            {"task-drift-0001": _job_row()},
            _bars_table(walk),
        )

        await trader_main.measure_startup_drift(_FakePool(conn))

        [(name, version, drift)] = conn.drift_writes
        assert (name, version) == ("alpha", 1)
        assert drift["max_psi"] < PSI_ALERT_THRESHOLD
        assert drift["status"] == "low"
        assert drift["computed_at"]
        # the NaN rate is the true worst per-feature fraction of the matrix: the
        # rolling-window indicators' structural warm-up rows, identical in the
        # training reference, never read as drift
        from hydra.ml.features import build_features

        matrix = build_features(SPEC, *walk)
        expected_nan = float(
            max(np.isnan(matrix.features[:, i]).mean() for i in range(len(SPEC.feature_names)))
        )
        assert drift["max_nan_rate"] == pytest.approx(expected_nan)
        assert 0.0 < drift["max_nan_rate"] < 0.1
        assert drift["window"] == {
            "rows": SPEC.window_1h,
            "first_ts": walk[0][0].timestamp.isoformat(),
            "last_ts": walk[0][-1].timestamp.isoformat(),
            "exchange": "binance",
            "symbol": "BTCUSDT",
        }
        # the sparsely-sampled 1d block and the discrete temporal encodings are
        # disclosed as unscored, never silently read as drift
        assert drift["scored_features"] > 0
        assert drift["unscored_features"] > 0
        assert drift["scored_features"] + drift["unscored_features"] == len(SPEC.feature_names)
        assert _gauge("hydra_ml_drift_psi", "alpha") == pytest.approx(drift["max_psi"])
        assert _gauge("hydra_ml_drift_nan_ratio", "alpha") == pytest.approx(expected_nan)

    async def test_a_hard_trend_against_a_flat_reference_scores_high(self) -> None:
        """The ramp's trend features land outside the oscillating reference deciles."""
        oscillating = _market(_oscillating_closes(_WALK_HOURS))
        ramp = _market(_ramp_closes(_WALK_HOURS))
        reference = _reference_quantiles(oscillating)
        conn = _FakeConn(
            [_model_row("alpha", training_job_id="task-drift-0001", reference_quantiles=reference)],
            {"task-drift-0001": _job_row()},
            _bars_table(ramp),
        )

        await trader_main.measure_startup_drift(_FakePool(conn))

        [(name, version, drift)] = conn.drift_writes
        assert (name, version) == ("alpha", 1)
        assert drift["max_psi"] > PSI_RETRAIN_THRESHOLD
        assert drift["status"] == "high"
        assert _gauge("hydra_ml_drift_psi", "alpha") == pytest.approx(drift["max_psi"])

    async def test_the_psi_is_scored_against_the_stored_deciles(self) -> None:
        """The written PSI equals scoring the matrix against the stored deciles directly."""
        walk = _market(_stationary_closes(_WALK_HOURS, seed=7))
        quantiles = _reference_quantiles(walk)
        conn = _FakeConn(
            [_model_row("alpha", training_job_id="task-drift-0001", reference_quantiles=quantiles)],
            {"task-drift-0001": _job_row()},
            _bars_table(walk),
        )

        await trader_main.measure_startup_drift(_FakePool(conn))

        from hydra.ml.features import build_features

        matrix = build_features(SPEC, *walk)
        scored = [
            i
            for i in range(len(SPEC.feature_names))
            if quantiles[i] is not None
            and len(set(quantiles[i])) == len(quantiles[i])
            and len(set(matrix.features[:, i][~np.isnan(matrix.features[:, i])].tolist()))
            >= trader_main.DRIFT_MIN_DISTINCT_VALUES
        ]
        assert scored, "the fixture must leave features that can be scored"
        expected = max(feature_psi(matrix.features[:, i], quantiles[i]).psi for i in scored)
        [(_, _, drift)] = conn.drift_writes
        assert drift["scored_features"] == len(scored)
        assert drift["max_psi"] == pytest.approx(expected, rel=1e-9)

    async def test_every_model_is_measured_and_one_failure_stops_none(self) -> None:
        walk = _market(_stationary_closes(_WALK_HOURS, seed=42))
        quantiles = _reference_quantiles(walk)
        conn = _FakeConn(
            [
                _model_row("alpha", training_job_id="task-broken", reference_quantiles=quantiles),
                _model_row(
                    "beta", training_job_id="task-drift-0001", reference_quantiles=quantiles
                ),
            ],
            {"task-drift-0001": _job_row()},
            _bars_table(walk),
        )
        conn.fail_job_ids.add("task-broken")

        await trader_main.measure_startup_drift(_FakePool(conn))

        assert [name for name, _, _ in conn.drift_writes] == ["beta"]
        assert _gauge("hydra_ml_drift_psi", "beta") is not None
        assert _gauge("hydra_ml_drift_psi", "alpha") is None

    async def test_a_model_without_a_training_job_keeps_drift_unknown(self) -> None:
        """An uploaded version has no provenance market: nothing is written or gauged."""
        walk = _market(_stationary_closes(_WALK_HOURS, seed=42))
        conn = _FakeConn(
            [_model_row("alpha", training_job_id=None, reference_quantiles=None)],
            {},
            _bars_table(walk),
        )

        await trader_main.measure_startup_drift(_FakePool(conn))

        assert conn.drift_writes == []
        assert _gauge("hydra_ml_drift_psi", "alpha") is None

    async def test_a_model_registered_without_reference_quantiles_stays_unknown(self) -> None:
        walk = _market(_stationary_closes(_WALK_HOURS, seed=42))
        conn = _FakeConn(
            [_model_row("alpha", training_job_id="task-drift-0001", reference_quantiles=None)],
            {"task-drift-0001": _job_row()},
            _bars_table(walk),
        )

        await trader_main.measure_startup_drift(_FakePool(conn))

        assert conn.drift_writes == []
        assert _gauge("hydra_ml_drift_psi", "alpha") is None

    async def test_a_model_whose_symbol_has_no_stored_bars_stays_unknown(self) -> None:
        walk = _market(_stationary_closes(_WALK_HOURS, seed=42))
        quantiles = _reference_quantiles(walk)
        conn = _FakeConn(
            [_model_row("alpha", training_job_id="task-drift-0001", reference_quantiles=quantiles)],
            {"task-drift-0001": _job_row(symbol="NOPEUSDT")},
            _bars_table(walk),
        )

        await trader_main.measure_startup_drift(_FakePool(conn))

        assert conn.drift_writes == []
        assert _gauge("hydra_ml_drift_psi", "alpha") is None

    async def test_a_model_with_nothing_left_to_score_stays_unknown(self) -> None:
        """Every feature below the distinct-value floor: no honest PSI, no write."""
        walk = _market(_stationary_closes(_WALK_HOURS, seed=42))
        quantiles = _reference_quantiles(walk)
        # a spec whose tiny windows leave every feature with far too few distinct
        # values to score, while the stored reference deciles stay real
        tiny = FeatureSpec(
            spec_version=SPEC.spec_version,
            horizon=SPEC.horizon,
            window_1h=50,
            window_4h=10,
            window_1d=5,
            feature_names=SPEC.feature_names,
        )
        tiny_spec = dict(tiny.to_dict())
        tiny_spec["reference_quantiles"] = [list(q) if q is not None else None for q in quantiles]
        conn = _FakeConn(
            [
                {
                    **_model_row(
                        "alpha", training_job_id="task-drift-0001", reference_quantiles=quantiles
                    ),
                    "feature_spec": json.dumps(tiny_spec),
                }
            ],
            {"task-drift-0001": _job_row()},
            _bars_table(walk),
        )

        await trader_main.measure_startup_drift(_FakePool(conn))

        assert conn.drift_writes == []
        assert _gauge("hydra_ml_drift_psi", "alpha") is None

    async def test_no_production_models_is_a_quiet_no_op(self) -> None:
        conn = _FakeConn([], {}, {})

        await trader_main.measure_startup_drift(_FakePool(conn))

        assert conn.drift_writes == []

    async def test_a_registry_failure_never_raises(self) -> None:
        class _ExplodingPool(_FakePool):
            def acquire(self) -> Any:
                raise RuntimeError("the database is gone")

        await trader_main.measure_startup_drift(_ExplodingPool(_FakeConn([], {}, {})))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The wiring: startup-only, in trader main, never SessionManager.startup
# ---------------------------------------------------------------------------


def _awaited_calls(tree: ast.Module, name: str) -> list[ast.Await]:
    def _called_name(func: ast.expr) -> str | None:
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return None

    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and _called_name(node.value.func) == name
    ]


def test_the_drift_task_is_awaited_once_between_startup_and_serving() -> None:
    tree = ast.parse(Path(trader_main.__file__).read_text())
    drift = _awaited_calls(tree, "measure_startup_drift")
    assert len(drift) == 1, "main() must await measure_startup_drift exactly once"
    startup = _awaited_calls(tree, "startup")
    serve = _awaited_calls(tree, "_serve")
    assert startup and serve
    assert startup[0].lineno < drift[0].lineno < serve[0].lineno


def test_the_drift_task_is_never_wired_into_a_loop() -> None:
    """Startup-only: no periodic task, by construction and by test."""
    tree = ast.parse(Path(trader_main.__file__).read_text())
    (drift,) = _awaited_calls(tree, "measure_startup_drift")
    for node in ast.walk(tree):
        if isinstance(node, (ast.While, ast.For, ast.AsyncFor)):
            for child in ast.walk(node):
                if child is drift:
                    pytest.fail("the drift task is scheduled inside a loop")


def test_session_manager_startup_never_measures_drift() -> None:
    """The critic's §2 rule: P5's drift wires into trader/main.py only."""
    source = (
        Path(trader_main.__file__).parents[1] / "execution" / "session_manager.py"
    ).read_text()
    assert "measure_startup_drift" not in source
    assert "hydra_ml_drift" not in source


def test_the_distinct_value_floor_knows_the_bin_count() -> None:
    """Ten decile bins need ~30 samples each before the chi-square noise floor
    (9/n) sits safely under the 0.2 alert threshold."""
    assert trader_main.DRIFT_MIN_DISTINCT_VALUES == 300
    assert 9 / trader_main.DRIFT_MIN_DISTINCT_VALUES < PSI_ALERT_THRESHOLD / 2
