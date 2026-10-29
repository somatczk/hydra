"""The P5-C consumption layer: the veto overlay, the session-start gate.

Real behaviour at the real seams throughout: the fixture is a real ONNX model
(``tests.fixtures.ml_onnx.confidence_onnx``: P(up) = scale * feature[column])
loaded by real onnxruntime through the real attach pipeline, the feature
vector comes from the frozen spec's real builder, and the counters are read
from the real Prometheus registry. Only the registry lookup (a database read)
is stubbed where a test needs it — the registry's own semantics are covered on
real Postgres by ``test_ml_registry_pg.py`` and
``tests/integration/test_ml_session_start_pg.py``.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from prometheus_client import REGISTRY

from hydra.core.config import HydraConfig
from hydra.core.types import OHLCV, Symbol, Timeframe
from hydra.ml.consumers import (
    LiveFeaturesPredictor,
    _reset_overlays,
    live_vector,
    validate_session_ml,
    veto_entry,
)
from hydra.ml.features import DEFAULT_FEATURE_SPEC, build_features
from hydra.ml.serving import (
    MlServingError,
    _reset_attached,
    attach_artifact,
    attach_production,
)
from hydra.strategy.builtin.ml_ensemble import MLEnsembleStrategy
from hydra.strategy.config import MLOverlayConfig, StrategyConfig
from hydra.strategy.context import StrategyContext
from hydra.strategy.ml_access import MlModelNotAttachedError, _reset_providers, provider_for
from tests.fixtures.ml_onnx import confidence_onnx, frozen_spec_metadata

T0 = datetime(2026, 1, 1, tzinfo=UTC)
H = timedelta(hours=1)
N_FEATURES = len(DEFAULT_FEATURE_SPEC.feature_names)
MODEL = "alpha"

PINNED = {
    "rising": 1.0,  # RSI(14) of a strictly rising series = 100 -> 100 * 0.01f
    "alternating": 0.519178,  # Wilder RSI(14) of the pinned +1/-1 alternation, x 0.01f
    "falling": 0.0,  # RSI = 0
}


@pytest.fixture(autouse=True)
def _isolate_ml_state() -> Any:
    from hydra.core import metrics as metrics_module

    _reset_attached()
    _reset_overlays()
    _reset_providers()
    metrics_module._reset_metrics()
    yield
    _reset_attached()
    _reset_overlays()
    _reset_providers()
    metrics_module._reset_metrics()


def _bar(ts: datetime, close: float) -> OHLCV:
    return OHLCV(
        open=Decimal(str(close)),
        high=Decimal(str(close)),
        low=Decimal(str(close)),
        close=Decimal(str(close)),
        volume=Decimal("10"),
        timestamp=ts,
    )


def _series(closes: list[float]) -> list[OHLCV]:
    return [_bar(T0 + i * H, c) for i, c in enumerate(closes)]


def _write_fixture(models_dir: Path, *, name: str = "fixture.onnx", scale: float = 0.01) -> bytes:
    meta = frozen_spec_metadata(list(DEFAULT_FEATURE_SPEC.feature_names), 3, 4)
    blob = confidence_onnx(meta, N_FEATURES, scale=scale)
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / name).write_bytes(blob)
    return blob


def _overlay_cfg(
    *,
    threshold: float = 0.6,
    shadow: bool = False,
    model: str = MODEL,
    required_history: int = 2000,
    primary: str = "1h",
) -> StrategyConfig:
    return StrategyConfig(
        id="gated",
        name="gated",
        strategy_class="hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        symbols=["BTCUSDT"],
        timeframes={"primary": primary, "confirmation": "4h", "entry": "1d"},
        parameters={"required_history": required_history},
        ml_overlay=MLOverlayConfig(
            enabled=True, model_name=model, confidence_threshold=threshold, shadow=shadow
        ),
    )


class _Host:
    """The SignalHost duck type: a strategy config plus a real context."""

    def __init__(self, cfg: StrategyConfig, context: StrategyContext) -> None:
        self.cfg = cfg
        self.context = context


def _host_with(closes: list[float], cfg: StrategyConfig) -> tuple[_Host, list[OHLCV]]:
    bars = _series(closes)
    context = StrategyContext()
    for bar in bars:
        context.add_bar("BTCUSDT", Timeframe.H1, bar)
    return _Host(cfg, context), bars


def _decision_value(strategy: str, decision: str) -> float:
    return REGISTRY.get_sample_value(
        "hydra_ml_overlay_decisions_total",
        {"strategy_id": strategy, "model_name": MODEL, "decision": decision},
    )


def _entry(direction: str = "LONG") -> Any:
    from hydra.core.events import EntrySignal
    from hydra.core.types import Direction

    return EntrySignal(
        symbol=Symbol("BTCUSDT"),
        direction=Direction(direction),
        strategy_id="gated",
        exchange_id="binance",
    )


class TestVetoEntry:
    """The seam's decision logic: below the threshold vetoed, above or at it passed."""

    async def _pinned_p_up(self, tmp_path: Path, closes: list[float]) -> float:
        """P(up) through the real path: attach, build the live vector, predict."""
        attached = await attach_artifact(tmp_path, "fixture.onnx", MODEL)
        return attached.predict_up(live_vector(_series(closes), [], []))

    async def test_no_overlay_config_passes_at_zero_cost(self, tmp_path: Path) -> None:
        cfg = _overlay_cfg()
        cfg.ml_overlay = None
        host, _ = _host_with([100.0 + i for i in range(60)], cfg)
        assert await veto_entry(host, None, HydraConfig.model_construct(), _entry()) is False

    async def test_disabled_overlay_passes(self, tmp_path: Path) -> None:
        cfg = _overlay_cfg()
        cfg.ml_overlay = MLOverlayConfig(enabled=False, model_name=MODEL)
        host, _ = _host_with([100.0 + i for i in range(60)], cfg)
        assert await veto_entry(host, None, HydraConfig.model_construct(), _entry()) is False

    async def test_below_threshold_is_vetoed(self, tmp_path: Path) -> None:
        _write_fixture(tmp_path)
        await attach_artifact(tmp_path, "fixture.onnx", MODEL)
        # alternating closes: RSI = 51.9178 (pinned) -> below threshold 0.6
        closes = [100.0 + (i % 2) for i in range(60)]
        assert await self._pinned_p_up(tmp_path, closes) == pytest.approx(
            PINNED["alternating"], abs=1e-6
        )
        cfg = _overlay_cfg(threshold=0.6)
        host, _ = _host_with(closes, cfg)
        assert await veto_entry(host, None, HydraConfig.model_construct(), _entry()) is True
        assert _decision_value("gated", "veto") == 1.0
        assert _decision_value("gated", "pass") is None

    async def test_above_threshold_passes(self, tmp_path: Path) -> None:
        _write_fixture(tmp_path)
        await attach_artifact(tmp_path, "fixture.onnx", MODEL)
        closes = [100.0 + (i % 2) for i in range(60)]
        cfg = _overlay_cfg(threshold=0.4)
        host, _ = _host_with(closes, cfg)
        assert await veto_entry(host, None, HydraConfig.model_construct(), _entry()) is False
        assert _decision_value("gated", "pass") == 1.0
        assert _decision_value("gated", "veto") is None

    async def test_exactly_at_the_threshold_passes(self, tmp_path: Path) -> None:
        _write_fixture(tmp_path)
        await attach_artifact(tmp_path, "fixture.onnx", MODEL)
        # the boundary: the confidence itself as the threshold — the entry is
        # vetoed only BELOW it, so the pinned P(up) passes exactly
        closes = [100.0 + (i % 2) for i in range(60)]
        p_up = await self._pinned_p_up(tmp_path, closes)
        cfg = _overlay_cfg(threshold=p_up)
        host, _ = _host_with(closes, cfg)
        assert await veto_entry(host, None, HydraConfig.model_construct(), _entry()) is False
        assert _decision_value("gated", "pass") == 1.0
        assert _decision_value("gated", "veto") is None

    async def test_short_confidence_is_the_inverse(self, tmp_path: Path) -> None:
        _write_fixture(tmp_path)
        await attach_artifact(tmp_path, "fixture.onnx", MODEL)
        # strictly rising bars: RSI(14) = 100 -> P(up) = 1.0 (pinned), so a
        # SHORT's confidence for its own direction is 0.0
        closes = [100.0 + i for i in range(60)]
        assert await self._pinned_p_up(tmp_path, closes) == pytest.approx(
            PINNED["rising"], abs=1e-9
        )
        cfg = _overlay_cfg(threshold=0.6)
        host, _ = _host_with(closes, cfg)
        assert await veto_entry(host, None, HydraConfig.model_construct(), _entry("LONG")) is False
        assert await veto_entry(host, None, HydraConfig.model_construct(), _entry("SHORT")) is True
        assert _decision_value("gated", "pass") == 1.0
        assert _decision_value("gated", "veto") == 1.0

    async def test_shadow_records_the_would_veto_without_suppressing(self, tmp_path: Path) -> None:
        _write_fixture(tmp_path)
        await attach_artifact(tmp_path, "fixture.onnx", MODEL)
        closes = [100.0 + (i % 2) for i in range(60)]
        cfg = _overlay_cfg(threshold=0.6, shadow=True)
        host, _ = _host_with(closes, cfg)
        # False means the signal is NOT suppressed at the seam
        assert await veto_entry(host, None, HydraConfig.model_construct(), _entry()) is False
        assert _decision_value("gated", "shadow_veto") == 1.0
        assert _decision_value("gated", "veto") is None

    async def test_a_model_that_cannot_attach_fails_closed_and_loud(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # nothing attached, no pool: the attach refuses, the entry is vetoed
        from hydra.core.logging import setup_logging

        setup_logging("INFO", "json")
        cfg = _overlay_cfg(threshold=0.6, model="ghost")
        host, _ = _host_with([100.0 + (i % 2) for i in range(60)], cfg)
        assert await veto_entry(host, None, HydraConfig.model_construct(), _entry()) is True
        assert (
            REGISTRY.get_sample_value(
                "hydra_ml_overlay_decisions_total",
                {"strategy_id": "gated", "model_name": "ghost", "decision": "error"},
            )
            == 1.0
        )
        # the refusal is loud: the structured error line names the failed model
        out = capsys.readouterr().out
        assert '"event": "ml_overlay_failed"' in out
        assert "ghost" in out

    async def test_the_overlay_pins_one_model_per_session(self, tmp_path: Path) -> None:
        _write_fixture(tmp_path)
        await attach_artifact(tmp_path, "fixture.onnx", MODEL)
        cfg = _overlay_cfg(threshold=0.4)
        host, _ = _host_with([100.0 + (i % 2) for i in range(60)], cfg)
        assert await veto_entry(host, None, HydraConfig.model_construct(), _entry()) is False
        # a second signal reuses the pinned overlay: still two passes, no re-attach
        assert await veto_entry(host, None, HydraConfig.model_construct(), _entry()) is False
        assert _decision_value("gated", "pass") == 2.0


class TestLiveVectorParity:
    """The SM-14 parity extension: the overlay's vector IS the training last row."""

    def test_live_vector_equals_the_training_matrix_last_row(self) -> None:
        bars_1h = _series([100.0 + (i % 3) for i in range(120)])
        bars_4h = _series([100.0 + (i % 5) for i in range(30)])[::1]
        bars_1d = _series([100.0 + (i % 7) for i in range(10)])
        matrix = build_features(DEFAULT_FEATURE_SPEC, bars_1h, bars_4h, bars_1d)
        live = live_vector(bars_1h, bars_4h, bars_1d)
        assert live.shape == (N_FEATURES,)
        from numpy.testing import assert_allclose

        assert_allclose(matrix.features[-1], live, rtol=0.0, atol=0.0)


class TestAttachPipeline:
    """The fail-closed attach refusals (registry rows stubbed; real files, real ORT)."""

    class _Conn:
        def __init__(self, row: Any) -> None:
            self._row = row

        async def fetchrow(self, sql: str, *args: Any) -> Any:
            return self._row

    class _Acquire:
        def __init__(self, conn: TestAttachPipeline._Conn) -> None:
            self._conn = conn

        async def __aenter__(self) -> TestAttachPipeline._Conn:
            return self._conn

        async def __aexit__(self, *exc: Any) -> None:
            return None

    class _Pool:
        def __init__(self, row: Any) -> None:
            self._row = row

        def acquire(self) -> TestAttachPipeline._Acquire:
            return TestAttachPipeline._Acquire(TestAttachPipeline._Conn(self._row))

    def _row(self, artifact_file: str, sha256: str) -> dict[str, Any]:
        return {
            "id": uuid.uuid4(),
            "name": MODEL,
            "version": 1,
            "stage": "production",
            "model_type": "onnx",
            "metrics": "{}",
            "feature_spec": "{}",
            "drift": None,
            "artifact_file": artifact_file,
            "artifact_sha256": sha256,
            "trained_at": datetime(2026, 10, 28, tzinfo=UTC),
            "created_by": "test",
            "training_job_id": None,
            "archived_at": None,
        }

    async def test_no_production_version_refuses(self, tmp_path: Path) -> None:
        with pytest.raises(MlServingError) as exc:
            await attach_production(self._Pool(None), tmp_path, MODEL)
        assert exc.value.kind == "not_promoted"
        assert "no Production version" in str(exc.value)

    async def test_missing_artifact_refuses_loudly_never_silently(self, tmp_path: Path) -> None:
        row = self._row("not-on-the-mount.onnx", "0" * 64)
        with pytest.raises(MlServingError) as exc:
            await attach_production(self._Pool(row), tmp_path, MODEL)
        assert exc.value.kind == "artifact_missing"
        assert "not mounted or the artifact was removed" in str(exc.value)

    async def test_a_swapped_artifact_fails_the_pinned_sha(self, tmp_path: Path) -> None:
        blob = _write_fixture(tmp_path)
        wrong = hashlib.sha256(b"different-bytes").hexdigest()
        row = self._row("fixture.onnx", wrong)
        with pytest.raises(MlServingError) as exc:
            await attach_production(self._Pool(row), tmp_path, MODEL)
        assert exc.value.kind == "invalid_model"
        assert "registry pinned" in str(exc.value)
        del blob

    async def test_unsafe_artifact_names_refuse(self, tmp_path: Path) -> None:
        row = self._row("../../etc/passwd.onnx", "0" * 64)
        with pytest.raises(MlServingError) as exc:
            await attach_production(self._Pool(row), tmp_path, MODEL)
        assert exc.value.kind == "invalid_model"

    async def test_a_spec_mismatch_refuses(self, tmp_path: Path) -> None:
        meta = frozen_spec_metadata(["wrong", "names"], 3, 4)
        (tmp_path / "mismatched.onnx").write_bytes(confidence_onnx(meta, N_FEATURES))
        with pytest.raises(MlServingError) as exc:
            await attach_artifact(tmp_path, "mismatched.onnx", MODEL)
        assert exc.value.kind == "spec_mismatch"
        assert "frozen spec" in str(exc.value)

    async def test_a_spec_less_artifact_refuses(self, tmp_path: Path) -> None:
        (tmp_path / "specless.onnx").write_bytes(confidence_onnx({}, N_FEATURES))
        with pytest.raises(MlServingError) as exc:
            await attach_artifact(tmp_path, "specless.onnx", MODEL)
        assert exc.value.kind == "spec_mismatch"

    async def test_a_warm_attach_needs_no_pool(self, tmp_path: Path) -> None:
        blob = _write_fixture(tmp_path)
        sha = hashlib.sha256(blob).hexdigest()
        first = await attach_production(self._Pool(self._row("fixture.onnx", sha)), tmp_path, MODEL)
        warm = await attach_production(None, tmp_path, MODEL)
        assert warm is first

    async def test_a_cold_attach_without_a_pool_refuses(self) -> None:
        with pytest.raises(MlServingError) as exc:
            await attach_production(None, Path("/app/models"), MODEL)
        assert exc.value.kind == "unavailable"

    async def test_a_two_output_classifier_reads_the_probabilities_output(
        self, tmp_path: Path
    ) -> None:
        import numpy as np

        from tests.fixtures.ml_onnx import two_output_classifier_onnx

        meta = frozen_spec_metadata(list(DEFAULT_FEATURE_SPEC.feature_names), 3, 4)
        (tmp_path / "xgb.onnx").write_bytes(
            two_output_classifier_onnx(meta, N_FEATURES, scale=0.01)
        )
        attached = await attach_artifact(tmp_path, "xgb.onnx", MODEL)
        # a real xgboost export answers [label, probabilities]: the up
        # confidence is the SECOND output's class-1 column (0.01 * 77), not
        # the first output (which would read 100.0 here)
        vector = np.zeros(N_FEATURES, dtype=np.float64)
        vector[0] = 100.0
        vector[1] = 77.0
        assert attached.predict_up(vector) == pytest.approx(0.77, abs=1e-6)  # float32 inference

    async def test_a_single_output_model_answers_its_one_confidence(self, tmp_path: Path) -> None:
        import numpy as np

        _write_fixture(tmp_path)
        attached = await attach_artifact(tmp_path, "fixture.onnx", MODEL)
        vector = np.zeros(N_FEATURES, dtype=np.float64)
        vector[0] = 100.0  # the RSI column: 100 * 0.01
        assert attached.predict_up(vector) == pytest.approx(1.0, abs=1e-9)


class TestMidSessionPromote:
    """A mid-session promote never swaps the model a running consumer pinned.

    The pin is three-way: the overlay's ``_OVERLAYS`` entry (pinned at the
    session's first signal), the native strategy's ``_predictor`` (pinned at
    ``on_start``) and the attach cache's ``(name, registry version)`` key. The
    registry row the stub pool serves flips from v1 to v2 mid-test — the
    promote — and a running consumer must keep answering v1's numbers while a
    fresh consumer attaches v2. v1 is the permissive scale (rising bars ->
    P(up) = 1.0, passes the 0.6 threshold) and v2 the strict one (P(up) =
    0.1, vetoed), so a regression to per-signal registry reads or a name-only
    cache key flips the running session's decision, not just its metadata.
    """

    class _PromotePool:
        """Serves whatever Production row it currently holds (the promote flips it)."""

        def __init__(self) -> None:
            self.row: Any = None

        def acquire(self) -> Any:
            pool = self

            class _Conn:
                async def fetchrow(self, sql: str, *args: Any) -> Any:
                    return pool.row

            class _Acquire:
                async def __aenter__(self) -> Any:
                    return _Conn()

                async def __aexit__(self, *exc: Any) -> None:
                    return None

            return _Acquire()

    def _write_version(self, models_dir: Path, name: str, scale: float) -> tuple[str, str]:
        meta = frozen_spec_metadata(list(DEFAULT_FEATURE_SPEC.feature_names), 3, 4)
        blob = confidence_onnx(meta, N_FEATURES, scale=scale)
        (models_dir / f"{name}.onnx").write_bytes(blob)
        return f"{name}.onnx", hashlib.sha256(blob).hexdigest()

    def _production_row(self, artifact_file: str, sha256: str, version: int) -> dict[str, Any]:
        return {
            "id": uuid4(),
            "name": MODEL,
            "version": version,
            "stage": "production",
            "model_type": "onnx",
            "metrics": "{}",
            "feature_spec": "{}",
            "drift": None,
            "artifact_file": artifact_file,
            "artifact_sha256": sha256,
            "trained_at": datetime(2026, 10, 28, tzinfo=UTC),
            "created_by": "test",
            "training_job_id": None,
            "archived_at": None,
        }

    def _native_cfg(self) -> StrategyConfig:
        return StrategyConfig(
            id="native",
            name="native",
            strategy_class="hydra.strategy.builtin.ml_ensemble.MLEnsembleStrategy",
            symbols=["BTCUSDT"],
            timeframes={"primary": "1h", "confirmation": "4h", "entry": "1d"},
            parameters={"model_name": MODEL, "required_history": 2000},
        )

    async def test_a_running_overlay_keeps_v1_across_a_promote(self, tmp_path: Path) -> None:
        from hydra.core.config import MlConfig
        from hydra.ml.consumers import _OVERLAYS

        v1_file, v1_sha = self._write_version(tmp_path, "alpha", 0.01)
        v2_file, v2_sha = self._write_version(tmp_path, "alpha-v2", 0.001)
        pool = self._PromotePool()
        pool.row = self._production_row(v1_file, v1_sha, 1)
        closes = [100.0 + i for i in range(60)]  # rising: v1 answers 1.0, v2 answers 0.1
        hydra_config = HydraConfig.model_construct(ml=MlConfig(models_dir=str(tmp_path)))

        # the session's first signal pins v1: the permissive model passes the entry
        host, _ = _host_with(closes, _overlay_cfg(threshold=0.6))
        assert await veto_entry(host, pool, hydra_config, _entry()) is False
        assert _OVERLAYS[host].model.version == 1
        assert _decision_value("gated", "pass") == 1.0

        # the promote: the registry now serves v2 as Production
        pool.row = self._production_row(v2_file, v2_sha, 2)

        # the RUNNING session keeps its pinned v1: the same signal still passes —
        # a per-signal registry read (or a name-only cache key) would attach v2
        # here and veto the entry the session already blessed
        assert await veto_entry(host, pool, hydra_config, _entry()) is False
        assert _OVERLAYS[host].model.version == 1
        assert _decision_value("gated", "pass") == 2.0

        # a NEW session attaches v2: the strict scale vetoes what v1 blessed
        host2, _ = _host_with(closes, _overlay_cfg(threshold=0.6))
        assert await veto_entry(host2, pool, hydra_config, _entry()) is True
        assert _OVERLAYS[host2].model.version == 2
        assert _decision_value("gated", "veto") == 1.0

    async def test_a_running_native_strategy_keeps_its_pinned_predictor(
        self, tmp_path: Path
    ) -> None:
        v1_file, v1_sha = self._write_version(tmp_path, "alpha", 0.01)
        v2_file, v2_sha = self._write_version(tmp_path, "alpha-v2", 0.001)
        pool = self._PromotePool()
        pool.row = self._production_row(v1_file, v1_sha, 1)
        cfg = self._native_cfg()

        await validate_session_ml(pool, tmp_path, cfg)
        strategy = MLEnsembleStrategy(cfg, StrategyContext())
        await strategy.on_start()
        pinned = strategy._predictor
        assert pinned is not None

        # the promote mid-session
        pool.row = self._production_row(v2_file, v2_sha, 2)
        bars = _series([100.0 + i for i in range(60)])
        assert pinned.predict_up(bars, [], []) == pytest.approx(1.0, abs=1e-9)

        # a NEW session's validation attaches v2 and answers its number; the
        # running strategy's pin is untouched
        await validate_session_ml(pool, tmp_path, cfg)
        fresh = provider_for(MODEL)
        assert fresh is not pinned
        assert fresh.predict_up(bars, [], []) == pytest.approx(0.1, abs=1e-7)
        assert strategy._predictor is pinned
        assert pinned.predict_up(bars, [], []) == pytest.approx(1.0, abs=1e-9)


class TestValidateSessionMl:
    """The session-start gate: pure config refusals first, then the attach."""

    def _native_cfg(self, **parameters: Any) -> StrategyConfig:
        return StrategyConfig(
            id="native",
            name="native",
            strategy_class="hydra.strategy.builtin.ml_ensemble.MLEnsembleStrategy",
            symbols=["BTCUSDT"],
            timeframes={"primary": "1h", "confirmation": "4h", "entry": "1d"},
            parameters={"model_name": MODEL, **parameters},
        )

    async def test_a_strategy_without_ml_validates_nothing(self, tmp_path: Path) -> None:
        cfg = StrategyConfig(
            id="plain",
            name="plain",
            strategy_class="hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        )
        await validate_session_ml(None, tmp_path, cfg)  # no pool, no models dir, no raise

    async def test_an_off_the_frozen_grid_strategy_refuses(self, tmp_path: Path) -> None:
        cfg = _overlay_cfg(primary="15m")
        with pytest.raises(ValueError, match="frozen ML feature grid"):
            await validate_session_ml(None, tmp_path, cfg)

    async def test_a_short_history_refuses(self, tmp_path: Path) -> None:
        cfg = _overlay_cfg(required_history=50)
        with pytest.raises(ValueError, match="required_history"):
            await validate_session_ml(None, tmp_path, cfg)

    async def test_an_overlay_without_a_pool_refuses_fail_closed(self, tmp_path: Path) -> None:
        cfg = _overlay_cfg()
        with pytest.raises(MlServingError) as exc:
            await validate_session_ml(None, tmp_path, cfg)
        assert exc.value.kind == "unavailable"

    async def test_a_native_strategy_without_a_pool_refuses_fail_closed(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(MlServingError) as exc:
            await validate_session_ml(None, tmp_path, self._native_cfg())
        assert exc.value.kind == "unavailable"

    async def test_the_native_provider_is_attached_on_a_passing_validation(
        self, tmp_path: Path
    ) -> None:
        blob = _write_fixture(tmp_path)
        sha = hashlib.sha256(blob).hexdigest()
        pool = self._pool_with_production(tmp_path, "fixture.onnx", sha)
        cfg = self._native_cfg()
        await validate_session_ml(pool, tmp_path, cfg)
        predictor = provider_for(MODEL)
        assert isinstance(predictor, LiveFeaturesPredictor)
        bars = _series([100.0 + i for i in range(60)])
        assert predictor.predict_up(bars, [], []) == pytest.approx(1.0, abs=1e-9)

    def _pool_with_production(self, models_dir: Path, artifact: str, sha: str) -> Any:
        row = {
            "id": uuid4(),
            "name": MODEL,
            "version": 1,
            "stage": "production",
            "model_type": "onnx",
            "metrics": "{}",
            "feature_spec": "{}",
            "drift": None,
            "artifact_file": artifact,
            "artifact_sha256": sha,
            "trained_at": datetime(2026, 10, 28, tzinfo=UTC),
            "created_by": "test",
            "training_job_id": None,
            "archived_at": None,
        }
        return TestAttachPipeline._Pool(row)

    async def test_the_native_strategy_pins_its_predictor_at_start(self, tmp_path: Path) -> None:
        blob = _write_fixture(tmp_path)
        sha = hashlib.sha256(blob).hexdigest()
        await validate_session_ml(
            self._pool_with_production(tmp_path, "fixture.onnx", sha), tmp_path, self._native_cfg()
        )
        strategy = MLEnsembleStrategy(
            self._native_cfg(),
            StrategyContext(),
        )
        await strategy.on_start()
        assert strategy._predictor is not None

    async def test_an_unattached_model_refuses_the_native_start(self) -> None:
        _reset_providers()
        strategy = MLEnsembleStrategy(self._native_cfg(), StrategyContext())
        with pytest.raises(MlModelNotAttachedError, match="attached"):
            await strategy.on_start()


class TestConfigRoundTrip:
    """ml_overlay serialises, round-trips and validates as part of the config."""

    def test_model_dump_and_reload_preserve_the_overlay(self) -> None:
        cfg = _overlay_cfg(threshold=0.55, shadow=True)
        data = cfg.model_dump(mode="json")
        again = StrategyConfig.model_validate(data)
        assert again.ml_overlay == cfg.ml_overlay
        assert again.ml_overlay is not None
        assert again.ml_overlay.model_name == MODEL
        assert again.ml_overlay.confidence_threshold == 0.55
        assert again.ml_overlay.shadow is True

    def test_yaml_round_trip_through_the_loader(self, tmp_path: Path) -> None:
        import yaml

        cfg = _overlay_cfg()
        path = tmp_path / "gated.yaml"
        path.write_text(yaml.safe_dump(cfg.model_dump(mode="json")))
        from hydra.strategy.config import load_strategy_config

        loaded = load_strategy_config(path)
        assert loaded.ml_overlay is not None
        assert loaded.ml_overlay.enabled is True
        assert loaded.ml_overlay.model_name == MODEL

    def test_a_stale_features_key_still_loads(self) -> None:
        data = _overlay_cfg().model_dump(mode="json")
        data["ml_overlay"]["features"] = ["stale"]
        cfg = StrategyConfig.model_validate(data)
        assert cfg.ml_overlay is not None
        assert cfg.ml_overlay.enabled is True

    def test_an_enabled_overlay_must_name_a_model(self) -> None:
        with pytest.raises(ValueError, match="must name a model"):
            StrategyConfig(
                id="x",
                name="x",
                strategy_class="hydra.strategy.builtin.rule_based.RuleBasedStrategy",
                ml_overlay={"enabled": True, "model_name": ""},
            )

    def test_the_threshold_is_bounded(self) -> None:
        with pytest.raises(ValueError):
            MLOverlayConfig(enabled=True, model_name=MODEL, confidence_threshold=1.5)


class TestResearchPin:
    """A research job serves a pinned registry version (any stage) for its run only.

    v1 is Production with the strict scale (rising bars -> P(up) = 0.1, vetoed
    at 0.6) and v2 is Staging with the permissive one (P(up) = 1.0, passes), so
    which version answered is visible in the decision, not just the metadata.
    The pin rides a context binding: inside it both consumers answer v2; outside
    it — where every session lives — nothing resolves to v2, and a session's
    gate attaches Production v1 in the same process.
    """

    class _RegistryPool:
        """Rows by id (the pin lookup) and the name's Production row."""

        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self.rows = rows

        def acquire(self) -> Any:
            rows = self.rows

            class _Conn:
                async def fetchrow(self, sql: str, *args: Any) -> Any:
                    if "WHERE id = $1" in sql:
                        return next((r for r in rows if r["id"] == args[0]), None)
                    if "stage = 'production'" in sql:
                        return next(
                            (
                                r
                                for r in rows
                                if r["name"] == args[0] and r["stage"] == "production"
                            ),
                            None,
                        )
                    raise AssertionError(f"unexpected registry read: {sql}")

            class _Acquire:
                async def __aenter__(self) -> Any:
                    return _Conn()

                async def __aexit__(self, *exc: Any) -> None:
                    return None

            return _Acquire()

    def _row(
        self, models_dir: Path, version: int, stage: str, scale: float, name: str = MODEL
    ) -> dict[str, Any]:
        meta = frozen_spec_metadata(list(DEFAULT_FEATURE_SPEC.feature_names), 3, 4)
        blob = confidence_onnx(meta, N_FEATURES, scale=scale)
        artifact = f"{name}-v{version}.onnx"
        (models_dir / artifact).write_bytes(blob)
        return {
            "id": uuid4(),
            "name": name,
            "version": version,
            "stage": stage,
            "model_type": "onnx",
            "metrics": "{}",
            "feature_spec": "{}",
            "drift": None,
            "artifact_file": artifact,
            "artifact_sha256": hashlib.sha256(blob).hexdigest(),
            "trained_at": datetime(2026, 10, 28, tzinfo=UTC),
            "created_by": "test",
            "training_job_id": None,
            "archived_at": None,
        }

    def _native_cfg(self) -> StrategyConfig:
        return StrategyConfig(
            id="native",
            name="native",
            strategy_class="hydra.strategy.builtin.ml_ensemble.MLEnsembleStrategy",
            symbols=["BTCUSDT"],
            timeframes={"primary": "1h", "confirmation": "4h", "entry": "1d"},
            parameters={"model_name": MODEL, "required_history": 2000},
        )

    async def test_a_pinned_staging_version_serves_the_run_and_never_a_session(
        self, tmp_path: Path
    ) -> None:
        from hydra.core.config import MlConfig
        from hydra.ml.consumers import _OVERLAYS, attach_research_ml
        from hydra.strategy.ml_access import bound_providers

        production = self._row(tmp_path, 1, "production", 0.001)
        staging = self._row(tmp_path, 2, "staging", 0.01)
        pool = self._RegistryPool([production, staging])
        cfg = self._native_cfg()
        closes = [100.0 + i for i in range(60)]
        bars = _series(closes)
        hydra_config = HydraConfig.model_construct(ml=MlConfig(models_dir=str(tmp_path)))

        # a session already serves Production v1 in this process
        await validate_session_ml(pool, tmp_path, cfg)
        assert provider_for(MODEL).predict_up(bars, [], []) == pytest.approx(0.1, abs=1e-7)

        providers, served = await attach_research_ml(pool, tmp_path, cfg, staging["id"])

        assert served == [
            {
                "id": str(staging["id"]),
                "name": MODEL,
                "version": 2,
                "stage": "staging",
                "pinned": True,
            }
        ]
        with bound_providers(providers):
            strategy = MLEnsembleStrategy(cfg, StrategyContext())
            await strategy.on_start()
            assert strategy._predictor is not None
            assert strategy._predictor.predict_up(bars, [], []) == pytest.approx(1.0, abs=1e-9)
            # the backtest overlay (no pool) reads the same binding, not the attach cache
            # where v1 sits first: v2 passes what Production vetoes
            host, _ = _host_with(closes, _overlay_cfg(threshold=0.6))
            assert await veto_entry(host, None, HydraConfig.model_construct(), _entry()) is False
            assert _OVERLAYS[host].model.version == 2

        # outside the binding, where sessions live, the pin resolves nowhere
        assert provider_for(MODEL).predict_up(bars, [], []) == pytest.approx(0.1, abs=1e-7)
        await validate_session_ml(pool, tmp_path, cfg)
        assert provider_for(MODEL).predict_up(bars, [], []) == pytest.approx(0.1, abs=1e-7)
        session_host, _ = _host_with(closes, _overlay_cfg(threshold=0.6))
        assert await veto_entry(session_host, pool, hydra_config, _entry()) is True
        assert _OVERLAYS[session_host].model.version == 1

    async def test_without_a_pin_the_job_serves_production(self, tmp_path: Path) -> None:
        from hydra.ml.consumers import attach_research_ml

        production = self._row(tmp_path, 1, "production", 0.001)
        pool = self._RegistryPool([production, self._row(tmp_path, 2, "staging", 0.01)])
        bars = _series([100.0 + i for i in range(60)])

        providers, served = await attach_research_ml(pool, tmp_path, self._native_cfg())

        assert [(m["version"], m["stage"], m["pinned"]) for m in served] == [
            (1, "production", False)
        ]
        assert providers[MODEL].predict_up(bars, [], []) == pytest.approx(0.1, abs=1e-7)

    async def test_a_pin_must_name_a_model_the_strategy_consumes(self, tmp_path: Path) -> None:
        from hydra.ml.consumers import attach_research_ml

        other = self._row(tmp_path, 1, "staging", 0.01, name="beta")
        pool = self._RegistryPool([other])

        with pytest.raises(ValueError, match="'beta' v1 but strategy native consumes alpha"):
            await attach_research_ml(pool, tmp_path, self._native_cfg(), other["id"])
        with pytest.raises(MlServingError) as unknown:
            await attach_research_ml(pool, tmp_path, self._native_cfg(), uuid4())
        assert unknown.value.kind == "not_found"
        rules_only = _overlay_cfg()
        rules_only.ml_overlay = None
        with pytest.raises(ValueError, match="consumes no model"):
            await attach_research_ml(pool, tmp_path, rules_only, other["id"])
