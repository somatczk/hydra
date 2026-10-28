"""The ml_train job: dataset -> trainer -> ONNX export -> registry (the P5-B bridge).

XGBoost and onnxmltools are faked at the ``sys.modules`` seam (CI installs only
the dev extra), but the fake converter serialises a REAL ONNX model, so the
artifact on disk is loaded, validated and read back by real onnxruntime, and the
registry INSERT is the real :class:`~hydra.ml.registry.ModelRegistry` over a
recording fake pool. The bars are fixture rows; nothing egresses.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest

from hydra.backtest import jobs, worker
from hydra.backtest.jobs import MlTrainRunRequest, ml_feature_spec
from hydra.core.types import Timeframe
from hydra.data.backfill import GapRange
from hydra.data.bars import timeframe_seconds
from hydra.data.errors import DataFetchError, NoMarketDataError
from hydra.strategy.config import StrategyConfig
from tests.fixtures.ml_onnx import FakeModelProto, fake_onnxmltools, fake_xgboost

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_H = timedelta(hours=1)

# Enough multi-timeframe history that the matrix's final rows are past every
# feature warm-up (the parity sample is the most recent fully-warmed window).
N_1H = 1350
N_4H = 340
N_1D = 55

RULES = {
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


def _request(**overrides: Any) -> MlTrainRunRequest:
    return MlTrainRunRequest(
        name="alpha",
        strategy_id="saw",
        symbol="BTCUSDT",
        horizon=1,
        window_1h=N_1H,
        window_4h=N_4H,
        window_1d=N_1D,
        **overrides,
    )


def _config() -> StrategyConfig:
    return StrategyConfig(
        id="saw",
        name="Saw",
        strategy_class="hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        parameters={"rules": RULES, "required_history": 10},
    )


def _ml_job(body: MlTrainRunRequest, config: StrategyConfig) -> dict[str, Any]:
    return {
        "id": "ml-unit0001",
        "kind": "ml_train",
        "request": json.dumps(body.model_dump(mode="json")),
        "strategy_config": json.dumps(config.model_dump(mode="json")),
        "detail": "{}",
    }


def _rows(start: datetime, count: int, step: timedelta) -> list[dict[str, Any]]:
    """Saw-tooth bars; the repo's fetch returns them newest first."""
    closes = [Decimal(1000 + (i % 20) * 5) for i in range(count)]
    rows = [
        {
            "open": c,
            "high": c,
            "low": c,
            "close": c,
            "volume": Decimal(10),
            "timestamp": start + i * step,
        }
        for i, c in enumerate(closes)
    ]
    return list(reversed(rows))


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


class _MlConn:
    """Serves the dataset builder's reads, the tick UPDATE and the registry INSERT."""

    def __init__(self) -> None:
        self.bars: dict[str, list[dict[str, Any]]] = {
            str(Timeframe.H1): _rows(_T0, N_1H, _H),
            str(Timeframe.H4): _rows(_T0, N_4H, 4 * _H),
            str(Timeframe.D1): _rows(_T0, N_1D, 24 * _H),
        }
        self.inserts: list[tuple[str, tuple[Any, ...]]] = []
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.ticks: list[tuple[float, dict[str, Any]]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "GROUP BY source" in query:
            return [{"source": "feed", "n": len(self.bars[str(args[2])])}]
        assert "ORDER BY timestamp DESC" in query, query
        return self.bars[str(args[2])]

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        if "RETURNING cancel_requested" in query:
            self.ticks.append((args[1], json.loads(args[2])))
            return {"cancel_requested": False}
        assert "INSERT INTO ml_models" in query, query
        self.inserts.append((query, args))
        # echo the registration as the row the INSERT ... RETURNING produces
        return {
            "id": args[0],
            "name": args[1],
            "version": 1,
            "stage": "staging",
            "model_type": args[2],
            "metrics": args[3],
            "feature_spec": args[4],
            "drift": None,
            "artifact_file": args[5],
            "artifact_sha256": args[6],
            "trained_at": args[7],
            "created_by": args[8],
            "training_job_id": args[9],
            "archived_at": None,
        }

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append((query, args))
        return "OK"

    def transaction(self) -> _Transaction:
        return _Transaction()


class _Pool:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


class _FakeEngine:
    """Stands in for OnDemandFetchService, recording every ensure_available call."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._error = error

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
        if on_progress is not None:
            on_progress(
                {
                    "state": "fetching",
                    "ranges": [[start.isoformat(), end.isoformat()]],
                    "bars_fetched": 5,
                    "weight_spent": 2,
                    "elapsed_ms": 3,
                }
            )
        if self._error is not None:
            raise self._error


@pytest.fixture()
def models_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "models"
    monkeypatch.setattr(worker, "_ml_models_dir", lambda: path)
    return path


async def _run_with_fakes(
    conn: _MlConn, engine: Any, job: dict[str, Any], run: worker.JobRun
) -> Any:
    """run_ml_train_job with xgboost/onnxmltools faked; onnxruntime stays real."""
    with patch.dict("sys.modules", {**fake_xgboost()[0], **fake_onnxmltools(FakeModelProto())}):
        return await worker.run_ml_train_job(_Pool(conn), engine, job, run)


# ---------------------------------------------------------------------------
# The request bounds (the job can never run unbounded)
# ---------------------------------------------------------------------------


class TestRequestBounds:
    def test_the_windows_and_horizon_are_bounded(self) -> None:
        with pytest.raises(Exception, match="window_1h"):
            MlTrainRunRequest(name="a", strategy_id="s", symbol="BTCUSDT", window_1h=10**9)
        with pytest.raises(Exception, match="window_4h"):
            MlTrainRunRequest(name="a", strategy_id="s", symbol="BTCUSDT", window_4h=10**6)
        with pytest.raises(Exception, match="window_1d"):
            MlTrainRunRequest(name="a", strategy_id="s", symbol="BTCUSDT", window_1d=10**6)
        with pytest.raises(Exception, match="horizon"):
            MlTrainRunRequest(name="a", strategy_id="s", symbol="BTCUSDT", horizon=0)
        with pytest.raises(Exception, match="horizon"):
            MlTrainRunRequest(name="a", strategy_id="s", symbol="BTCUSDT", horizon=10_000)

    def test_the_name_is_a_plain_identifier_never_a_path(self) -> None:
        for bad in ("../evil", "a/b", ".hidden", "", "..", "x\\y"):
            with pytest.raises(Exception, match="name"):
                MlTrainRunRequest(name=bad, strategy_id="s", symbol="BTCUSDT")

    def test_the_grid_is_the_pinned_primary_timeframe(self) -> None:
        with pytest.raises(Exception, match="1h grid"):
            MlTrainRunRequest(name="a", strategy_id="s", symbol="BTCUSDT", timeframe=Timeframe.H4)

    def test_the_spec_keeps_the_pinned_names_and_version(self) -> None:
        spec = ml_feature_spec(_request())
        assert spec.spec_version == jobs.DEFAULT_FEATURE_SPEC.spec_version
        assert spec.feature_names == jobs.DEFAULT_FEATURE_SPEC.feature_names
        assert (spec.horizon, spec.window_1h, spec.window_4h, spec.window_1d) == (
            1,
            N_1H,
            N_4H,
            N_1D,
        )


# ---------------------------------------------------------------------------
# The fetch phase: each window read through the one engine, budgeted
# ---------------------------------------------------------------------------


async def test_each_window_is_fetched_through_the_engine_over_its_own_range() -> None:
    spec = ml_feature_spec(_request())
    engine = _FakeEngine()
    run = worker.JobRun()

    await worker._ensure_ml_bars(_Pool(_MlConn()), engine, "binance", "BTCUSDT", spec, run)

    windows = {
        Timeframe.H1: spec.window_1h,
        Timeframe.H4: spec.window_4h,
        Timeframe.D1: spec.window_1d,
    }
    assert [c["timeframe"] for c in engine.calls] == [Timeframe.H1, Timeframe.H4, Timeframe.D1]
    for call in engine.calls:
        step = timedelta(seconds=timeframe_seconds(call["timeframe"]))
        assert call["exchange"] == "binance"
        assert call["symbol"] == "BTCUSDT"
        assert call["end"] - call["start"] == step * windows[call["timeframe"]]
    assert run.progress == 20.0
    assert run.detail["fetch"]["timeframe"] == "1d"  # the last read's emit rides the detail


async def test_mid_fetch_progress_maps_onto_its_band_slice() -> None:
    spec = ml_feature_spec(_request())
    run = worker.JobRun()

    class _SpyEngine(_FakeEngine):
        def __init__(self) -> None:
            super().__init__()
            self.seen: list[float] = []

        async def ensure_available(self, *args: Any, **kw: Any) -> None:
            await super().ensure_available(*args, **kw)
            self.seen.append(run.progress)

    engine = _SpyEngine()
    await worker._ensure_ml_bars(_Pool(_MlConn()), engine, "binance", "BTCUSDT", spec, run)

    # 5 of 1350 1h bars owed lands inside the first third of the 0-20 band
    assert engine.seen[0] <= 20.0 / 3
    assert engine.seen[1] <= 40.0 / 3
    assert engine.seen[2] <= 20.0
    assert engine.seen == sorted(engine.seen)


async def test_a_data_fetch_error_fails_the_job_retryable_with_the_reason() -> None:
    error = DataFetchError(
        "rate_limited",
        "binance",
        "BTCUSDT",
        Timeframe.H1,
        weight_spent=2,
        elapsed_s=31.2,
        remaining_ranges=[GapRange(start=_T0, end=_T0 + _H)],
    )
    run = worker.JobRun()

    with pytest.raises(worker.JobError) as excinfo:
        await worker._ensure_ml_bars(
            _Pool(_MlConn()),
            _FakeEngine(error=error),
            "binance",
            "BTCUSDT",
            ml_feature_spec(_request()),
            run,
        )

    assert str(excinfo.value) == error.message
    assert run.detail["fetch"]["state"] == "failed"
    assert run.detail["fetch"]["reason"] == "rate_limited"


async def test_a_range_the_exchange_lacks_keeps_the_no_market_data_vocabulary() -> None:
    error = NoMarketDataError("binance", "BTCUSDT", Timeframe.H1, None, None, None, None)
    run = worker.JobRun()

    with pytest.raises(NoMarketDataError) as excinfo:
        await worker._ensure_ml_bars(
            _Pool(_MlConn()),
            _FakeEngine(error=error),
            "binance",
            "BTCUSDT",
            ml_feature_spec(_request()),
            run,
        )

    assert excinfo.value is error
    assert run.detail["fetch"]["state"] == "failed"


# ---------------------------------------------------------------------------
# The handler round-trip on fixtures
# ---------------------------------------------------------------------------


async def test_the_round_trip_trains_exports_and_registers(models_dir: Path) -> None:
    conn = _MlConn()
    run = worker.JobRun()

    outcome = await _run_with_fakes(conn, None, _ml_job(_request(), _config()), run)

    # the job completed with the real fold metrics
    assert outcome.result is not None
    assert outcome.result["name"] == "alpha"
    assert outcome.result["version"] == 1
    assert outcome.result["stage"] == "staging"
    metrics = outcome.result["metrics"]
    assert metrics["folds"] >= 1
    assert metrics["embargo"] == 1
    assert 0.0 <= metrics["oos_accuracy"] <= 1.0
    assert len(metrics["fold_accuracy"]) == metrics["folds"]
    assert all(0.0 <= a <= 1.0 for a in metrics["fold_accuracy"])

    # the artifact is the job's own file, a real ONNX model with the spec in it
    artifact = models_dir / "ml-unit0001.onnx"
    assert artifact.exists()
    assert outcome.result["artifact_file"] == artifact.name
    assert outcome.result["artifact_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()

    import onnxruntime as ort

    session = ort.InferenceSession(str(artifact), providers=["CPUExecutionProvider"])
    metadata = session.get_modelmeta().custom_metadata_map
    assert metadata["feature_version"] == "3"
    assert metadata["spec_version"] == "3"
    assert metadata["horizon"] == "1"
    assert metadata["feature_names"] == json.dumps(list(jobs.DEFAULT_FEATURE_SPEC.feature_names))
    x = np.linspace(-1.0, 1.0, 128, dtype=np.float32).reshape(4, 32)
    assert np.allclose(session.run(None, {"x": x})[0], x.max(axis=1))

    # the provenance of what it trained on rides the detail
    coverage = run.detail["provenance"]["coverage"]
    assert coverage[0]["bar_count"] == N_1H
    assert coverage[0]["sources"] == {"feed": N_1H}
    assert coverage[1]["bar_count"] == N_4H
    assert coverage[2]["bar_count"] == N_1D
    assert run.detail["metrics"] == metrics
    assert run.progress == 90.0

    # the registry row: the real INSERT through ModelRegistry, under the job id
    ((query, args),) = conn.inserts
    assert "INSERT INTO ml_models" in query
    assert args[1] == "alpha"
    assert args[8] == "job:ml-unit0001"
    assert args[9] == "ml-unit0001"
    assert json.loads(args[3])["folds"] == metrics["folds"]
    assert json.loads(args[4])["spec_version"] == 3
    assert json.loads(args[4])["horizon"] == 1


async def test_a_parity_failure_fails_the_job_and_registers_nothing(models_dir: Path) -> None:
    """A model whose predictions the ONNX cannot reproduce never reaches the registry."""
    conn = _MlConn()
    xgb_patch, model = fake_xgboost()
    model.predict = lambda x: np.asarray(x, dtype=np.float64).max(axis=1) + 100.0
    run = worker.JobRun()

    with (
        patch.dict("sys.modules", {**xgb_patch, **fake_onnxmltools(FakeModelProto())}),
        pytest.raises(worker.JobError, match="does not reproduce"),
    ):
        await worker.run_ml_train_job(_Pool(conn), None, _ml_job(_request(), _config()), run)

    assert conn.inserts == []


async def test_a_specless_artifact_is_refused_before_registration(models_dir: Path) -> None:
    """An export that does not carry the feature-spec metadata never registers."""
    conn = _MlConn()
    run = worker.JobRun()

    with (
        patch.dict("sys.modules", {**fake_xgboost()[0], **fake_onnxmltools(FakeModelProto(False))}),
        pytest.raises(worker.JobError, match="does not carry the trained feature spec"),
    ):
        await worker.run_ml_train_job(_Pool(conn), None, _ml_job(_request(), _config()), run)

    assert conn.inserts == []


async def test_the_timeout_vocabulary_fails_an_overrunning_ml_train(
    tmp_path: Path, models_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ml_train job that overruns its timeout fails with the queue's vocabulary."""
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", str(tmp_path / "heartbeat"))
    conn = _MlConn()
    xgb_patch, model = fake_xgboost()

    def slow_fit(*args: Any, **kw: Any) -> None:
        time.sleep(0.05)

    model.fit = slow_fit

    with patch.dict("sys.modules", {**xgb_patch, **fake_onnxmltools(FakeModelProto())}):
        await worker._run_claimed(
            _Pool(conn), _ml_job(_request(), _config()), timeout_s=0.05, tick_s=0.01
        )

    finishes = [(q, a) for q, a in conn.executed if "finished_at = now()" in q]
    assert len(finishes) == 1
    _, args = finishes[0]
    assert args[:2] == ("ml-unit0001", "failed")
    assert args[4] is not None and args[4].startswith("timed out after")
    assert conn.inserts == []  # nothing registered mid-flight


async def test_the_job_dispatches_and_completes_through_the_queue(
    tmp_path: Path, models_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ml_train kind rides the same _HANDLERS dispatch and terminal write."""
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", str(tmp_path / "heartbeat"))
    assert worker._HANDLERS["ml_train"] is worker.run_ml_train_job
    conn = _MlConn()

    with patch.dict("sys.modules", {**fake_xgboost()[0], **fake_onnxmltools(FakeModelProto())}):
        await worker._run_claimed(_Pool(conn), _ml_job(_request(), _config()), tick_s=0.01)

    finishes = [(q, a) for q, a in conn.executed if "finished_at = now()" in q]
    assert len(finishes) == 1
    _, args = finishes[0]
    assert args[:2] == ("ml-unit0001", "completed")
    result = json.loads(args[2])
    assert result["name"] == "alpha"
    assert result["metrics"]["folds"] >= 1
    assert args[3] is None  # no backtest result rows to point at
    assert len(conn.inserts) == 1  # the registry INSERT happened exactly once
