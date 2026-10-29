"""The P5-C session-start validation on real TimescaleDB.

The registry lookups run against the real ``ml_models`` table (migrations at
head): a registered-and-promoted fixture artifact attaches and serves a real
prediction; a staging-only or unknown name refuses ``not_promoted``; a
Production row whose artifact is not on the models mount refuses
``artifact_missing`` — the §7 deploy-risk shape, loud and fail-closed, never a
silent shadow no-op; swapped bytes fail the pinned sha256. The restart half
proves the recovery gate on the real SessionManager: a running session whose
strategy carries an enabled overlay resumes while its Production model is
promoted and errors (with the model named) once it is not.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from hydra.core.config import HydraConfig
from hydra.data.migrations.runner import upgrade_to_head
from hydra.ml.consumers import (
    LiveFeaturesPredictor,
    _reset_overlays,
    validate_session_ml,
)
from hydra.ml.features import DEFAULT_FEATURE_SPEC
from hydra.ml.registry import ModelRegistration, ModelRegistry
from hydra.ml.serving import MlServingError, _reset_attached
from hydra.strategy.config import MLOverlayConfig, StrategyConfig
from hydra.strategy.context import StrategyContext
from tests.fixtures.ml_onnx import confidence_onnx, frozen_spec_metadata

pytestmark = pytest.mark.integration

TRAINED_AT = datetime(2026, 10, 28, 12, tzinfo=UTC)
N_FEATURES = len(DEFAULT_FEATURE_SPEC.feature_names)
MODEL = "alpha"
ML_NATIVE = "hydra.strategy.builtin.ml_ensemble.MLEnsembleStrategy"


@pytest.fixture(autouse=True)
def _isolate_ml_state() -> AsyncIterator[None]:
    _reset_attached()
    _reset_overlays()
    yield
    _reset_attached()
    _reset_overlays()


@pytest.fixture()
async def pool(fresh_database: str) -> AsyncIterator[Any]:
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    db_pool = await asyncpg.create_pool(fresh_database, min_size=1, max_size=4)
    try:
        yield db_pool
    finally:
        await db_pool.close()


def _fixture_bytes() -> bytes:
    meta = frozen_spec_metadata(list(DEFAULT_FEATURE_SPEC.feature_names), 3, 4)
    return confidence_onnx(meta, N_FEATURES)


def _overlay_cfg(model: str = MODEL) -> StrategyConfig:
    return StrategyConfig(
        id="gated",
        name="gated",
        strategy_class="hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        symbols=["BTCUSDT"],
        timeframes={"primary": "1h", "confirmation": "4h", "entry": "1d"},
        parameters={"required_history": 2000},
        ml_overlay=MLOverlayConfig(enabled=True, model_name=model, confidence_threshold=0.6),
    )


def _native_cfg(model: str = MODEL) -> StrategyConfig:
    return StrategyConfig(
        id="native",
        name="native",
        strategy_class=ML_NATIVE,
        symbols=["BTCUSDT"],
        timeframes={"primary": "1h", "confirmation": "4h", "entry": "1d"},
        parameters={"model_name": model, "required_history": 2000},
    )


async def _register(pool: Any, models_dir: Path, name: str, *, promote_version: bool = True) -> int:
    blob = _fixture_bytes()
    artifact = models_dir / f"{name}.onnx"
    models_dir.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(blob)
    version = await ModelRegistry(pool).register(
        ModelRegistration(
            name=name,
            model_type="onnx",
            metrics={},
            feature_spec={"spec_version": 3},
            artifact_path=artifact,
            artifact_sha256=hashlib.sha256(blob).hexdigest(),
            trained_at=TRAINED_AT,
            created_by="integration-test",
        )
    )
    if promote_version:
        await ModelRegistry(pool).promote(name, version.version)
    return version.version


class TestSessionStartValidation:
    async def test_the_registry_lookups_and_the_attach_on_real_rows(
        self, pool: Any, tmp_path: Path
    ) -> None:
        models_dir = tmp_path / "models"
        await _register(pool, models_dir, MODEL)
        cfg = _overlay_cfg()

        # a name with nothing registered refuses not_promoted
        with pytest.raises(MlServingError) as raised:
            await validate_session_ml(pool, models_dir, _overlay_cfg("ghost"))
        assert raised.value.kind == "not_promoted"

        # the promoted Production version attaches and answers a real number
        attached = await validate_session_ml(pool, models_dir, cfg)  # no raise = attached
        assert attached is None
        from hydra.ml.serving import attach_production

        model = await attach_production(pool, models_dir, MODEL)
        import numpy as np

        vector = np.zeros(N_FEATURES, dtype=np.float64)
        vector[0] = 100.0
        assert model.predict_up(vector) == pytest.approx(1.0, abs=1e-9)

        # the warm cache answers without the registry
        warm = await attach_production(None, models_dir, MODEL)
        assert warm is model

    async def test_a_staging_only_model_is_not_promoted(self, pool: Any, tmp_path: Path) -> None:
        models_dir = tmp_path / "models"
        await _register(pool, models_dir, "beta", promote_version=False)
        with pytest.raises(MlServingError) as raised:
            await validate_session_ml(pool, models_dir, _overlay_cfg("beta"))
        assert raised.value.kind == "not_promoted"
        assert "promote" in str(raised.value)

    async def test_a_missing_artifact_refuses_loudly_the_mount_case(
        self, pool: Any, tmp_path: Path
    ) -> None:
        models_dir = tmp_path / "models"
        await _register(pool, models_dir, MODEL)
        # the mount loses the artifact between the promote and the session start
        (models_dir / f"{MODEL}.onnx").unlink()
        with pytest.raises(MlServingError) as raised:
            await validate_session_ml(pool, models_dir, _overlay_cfg())
        assert raised.value.kind == "artifact_missing"
        assert "refuses rather than trade without the model" in str(raised.value)

    async def test_swapped_bytes_fail_the_pinned_sha(self, pool: Any, tmp_path: Path) -> None:
        models_dir = tmp_path / "models"
        await _register(pool, models_dir, MODEL)
        (models_dir / f"{MODEL}.onnx").write_bytes(b"swapped-bytes-not-a-model")
        with pytest.raises(MlServingError) as raised:
            await validate_session_ml(pool, models_dir, _overlay_cfg())
        assert raised.value.kind in ("invalid_model", "spec_mismatch")

    async def test_the_native_strategy_provider_attaches_from_real_rows(
        self, pool: Any, tmp_path: Path
    ) -> None:
        from hydra.strategy.ml_access import _reset_providers, provider_for

        _reset_providers()
        models_dir = tmp_path / "models"
        await _register(pool, models_dir, MODEL)
        cfg = _native_cfg()
        await validate_session_ml(pool, models_dir, cfg)
        predictor = provider_for(MODEL)
        assert isinstance(predictor, LiveFeaturesPredictor)

        from hydra.strategy.builtin.ml_ensemble import MLEnsembleStrategy

        strategy = MLEnsembleStrategy(cfg, StrategyContext())
        await strategy.on_start()
        assert strategy._predictor is predictor


# ---------------------------------------------------------------------------
# The recovery gate on the real SessionManager
# ---------------------------------------------------------------------------


async def _restart_scenario(pool: Any, strategies_dir: Path, models_dir: Path) -> tuple[Any, Any]:
    """Start an overlay session, restart with a fresh SessionManager, return
    the recovered session and its persisted row."""
    import yaml

    from hydra.execution.client_factory import ClientFactory, bars_repository
    from hydra.execution.session_manager import SessionManager
    from hydra.risk.state import InMemoryRiskStateStore
    from tests.unit.test_execution.session_harness import Harness, write_strategy

    config = HydraConfig(ml={"models_dir": str(models_dir)})

    def clients(h: Harness) -> ClientFactory:
        fake = h.clients()
        return ClientFactory(
            ws_factory=fake.ws_factory,
            rest_pool=fake.rest_pool,
            markets_loader=fake.markets_loader,
            repository_factory=bars_repository,
            history_window=2100,
        )

    def manager(h: Harness) -> SessionManager:
        return SessionManager(
            db_pool=pool,
            config=config,
            clients=clients(h),
            risk_store=InMemoryRiskStateStore(paper_capital=Decimal("1000")),
        )

    h = Harness(strategies_dir, history_bars=2100)
    sid = write_strategy(
        strategies_dir,
        "pg-ml",
        timeframes={"primary": "1h", "confirmation": "4h", "entry": "1d"},
        entry_long={"operator": "AND", "conditions": []},
    )
    # the overlay rides the strategy's YAML like every other field
    path = strategies_dir / f"{sid}.yaml"
    data = yaml.safe_load(path.read_text())
    data["parameters"]["required_history"] = 2000
    data["ml_overlay"] = {
        "enabled": True,
        "model_name": MODEL,
        "confidence_threshold": 0.6,
    }
    path.write_text(yaml.safe_dump(data))

    first = manager(h)
    await first.startup()
    session_id = await first.start_session(sid, "paper", Decimal("1000"))
    await first.graceful_shutdown()

    h2 = Harness(strategies_dir, history_bars=2100)
    second = manager(h2)
    await second.startup()
    recovered = second.get_session(session_id)
    row = await pool.fetchrow(
        "SELECT status, error_message FROM trading_sessions WHERE id = $1", session_id
    )
    await second.graceful_shutdown()
    return recovered, row


class TestRecoveryGate:
    async def test_a_session_with_its_model_promoted_resumes(
        self, pool: Any, strategies_dir: Path, tmp_path: Path
    ) -> None:
        from hydra.strategy.ml_access import _reset_providers

        models_dir = tmp_path / "models"
        await _register(pool, models_dir, MODEL)
        _reset_providers()
        recovered, row = await _restart_scenario(pool, strategies_dir, models_dir)
        assert recovered is not None
        assert recovered.status == "running"
        assert row is not None
        assert row["status"] == "running"

    async def test_a_session_whose_model_was_unpromoted_errors_loudly(
        self, pool: Any, strategies_dir: Path, tmp_path: Path
    ) -> None:
        from hydra.strategy.ml_access import _reset_providers

        models_dir = tmp_path / "models"
        await _register(pool, models_dir, MODEL)
        # the model is un-promoted while the process is down
        await pool.execute("UPDATE ml_models SET stage = 'archived' WHERE name = $1", MODEL)
        _reset_providers()
        recovered, row = await _restart_scenario(pool, strategies_dir, models_dir)
        assert recovered is not None
        assert recovered.status == "error"
        assert row is not None
        assert row["status"] == "error"
        assert "no Production version" in (row["error_message"] or "")
