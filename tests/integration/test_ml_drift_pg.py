"""The trader's startup drift measurement on real TimescaleDB (P5-D).

Needs HYDRA_DB_* (see tests/infra.py): a fixture Production model registered
and promoted over committed ``ml_models`` rows, its training job's
symbol/exchange provenance in ``backtest_jobs``, and real seeded bars in
``ts.ohlcv_1m`` — then ``measure_startup_drift`` runs against the true
registry, repository and drift-write paths, and the drift jsonb and the
gauges are read back from the committed row and the live registry.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from prometheus_client import REGISTRY

from hydra.core import metrics
from hydra.core.types import Timeframe
from hydra.data.migrations.runner import upgrade_to_head
from hydra.data.storage import MarketDataRepository
from hydra.ml.registry import ModelRegistration, ModelRegistry
from hydra.trader.main import measure_startup_drift
from tests.unit.test_trader.test_startup_drift import (
    _WALK_HOURS,
    SPEC,
    _market,
    _oscillating_closes,
    _ramp_closes,
    _reference_quantiles,
    _stationary_closes,
)

pytestmark = pytest.mark.integration

TRAINED_AT = datetime(2026, 10, 28, 12, tzinfo=UTC)


@pytest.fixture()
async def pool(fresh_database: str) -> AsyncIterator[Any]:
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    db_pool = await asyncpg.create_pool(fresh_database, min_size=1, max_size=4)
    try:
        yield db_pool
    finally:
        await db_pool.close()


@pytest.fixture(autouse=True)
def _fresh_registry() -> AsyncIterator[None]:
    metrics._reset_metrics()
    yield
    metrics._reset_metrics()


async def _register(
    pool: Any,
    tmp_path: Path,
    *,
    name: str,
    job_id: str,
    quantiles: tuple[tuple[float, ...] | None, ...],
) -> Any:
    artifact = tmp_path / f"{name}-v1.onnx"
    artifact.write_bytes(f"model-bytes-{name}".encode())
    spec = dict(SPEC.to_dict())
    spec["reference_quantiles"] = [list(q) if q is not None else None for q in quantiles]
    version = await ModelRegistry(pool).register(
        ModelRegistration(
            name=name,
            model_type="xgboost",
            metrics={"oos_accuracy": 0.51, "folds": 5},
            feature_spec=spec,
            artifact_path=artifact,
            artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
            trained_at=TRAINED_AT,
            created_by=f"job:{job_id}",
            training_job_id=job_id,
        )
    )
    return await ModelRegistry(pool).promote(name, version.version)


async def test_the_drift_task_writes_the_row_and_sets_the_gauges(pool: Any, tmp_path: Path) -> None:
    """A Production model over seeded bars: the PSI lands on the committed row."""
    walk = _market(_stationary_closes(_WALK_HOURS, seed=42))
    quantiles = _reference_quantiles(walk)
    repo = MarketDataRepository.from_pool(pool)
    rows = [
        ("binance", "BTCUSDT", tf, bar)
        for tf, bars in zip((Timeframe.H1, Timeframe.H4, Timeframe.D1), walk, strict=True)
        for bar in bars
    ]
    assert await repo.store_bars(rows) == len(rows)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO backtest_jobs (id, kind, request, strategy_config) "
            "VALUES ($1, 'ml_train', $2, $3)",
            "task-drift-0001",
            '{"name": "alpha", "strategy_id": "saw", "symbol": "BTCUSDT"}',
            '{"exchange": {"exchange_id": "binance"}}',
        )
    promoted = await _register(
        pool, tmp_path, name="alpha", job_id="task-drift-0001", quantiles=quantiles
    )

    await measure_startup_drift(pool)

    measured = await ModelRegistry(pool).get("alpha", promoted.version)
    assert measured is not None and measured.drift is not None
    assert measured.drift["max_psi"] < 0.2
    assert measured.drift["status"] == "low"
    assert measured.drift["scored_features"] > 0
    assert measured.drift["unscored_features"] > 0
    assert measured.drift["window"]["rows"] == SPEC.window_1h
    assert measured.drift["window"]["symbol"] == "BTCUSDT"
    assert measured.drift["window"]["exchange"] == "binance"
    assert measured.drift["computed_at"]
    assert REGISTRY.get_sample_value(
        "hydra_ml_drift_psi", {"model_name": "alpha"}
    ) == pytest.approx(measured.drift["max_psi"])
    assert REGISTRY.get_sample_value(
        "hydra_ml_drift_nan_ratio", {"model_name": "alpha"}
    ) == pytest.approx(measured.drift["max_nan_rate"])


async def test_a_shifted_market_reads_high_and_uploads_stay_unknown(
    pool: Any, tmp_path: Path
) -> None:
    """The ramp against the oscillating reference scores high on the committed row;
    an uploaded version with no training job keeps drift unknown, in the same run."""
    oscillating = _market(_oscillating_closes(_WALK_HOURS))
    ramp = _market(_ramp_closes(_WALK_HOURS))
    reference = _reference_quantiles(oscillating)
    repo = MarketDataRepository.from_pool(pool)
    rows = [
        ("binance", "RAMPUSDT", tf, bar)
        for tf, bars in zip((Timeframe.H1, Timeframe.H4, Timeframe.D1), ramp, strict=True)
        for bar in bars
    ]
    assert await repo.store_bars(rows) == len(rows)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO backtest_jobs (id, kind, request, strategy_config) "
            "VALUES ($1, 'ml_train', $2, $3)",
            "task-drift-0002",
            '{"name": "beta", "strategy_id": "saw", "symbol": "RAMPUSDT"}',
            '{"exchange": {"exchange_id": "binance"}}',
        )
    await _register(pool, tmp_path, name="beta", job_id="task-drift-0002", quantiles=reference)
    artifact = tmp_path / "gamma-v1.onnx"
    artifact.write_bytes(b"model-bytes-gamma")
    await ModelRegistry(pool).register(
        ModelRegistration(
            name="gamma",
            model_type="onnx",
            metrics={},
            feature_spec={},
            artifact_path=artifact,
            artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
            trained_at=TRAINED_AT,
            created_by="upload:dev",
        )
    )
    await ModelRegistry(pool).promote("gamma", 1)

    await measure_startup_drift(pool)

    beta = await ModelRegistry(pool).get_production("beta")
    assert beta is not None and beta.drift is not None
    assert beta.drift["max_psi"] > 0.25
    assert beta.drift["status"] == "high"
    gamma = await ModelRegistry(pool).get_production("gamma")
    assert gamma is not None and gamma.drift is None
    assert REGISTRY.get_sample_value("hydra_ml_drift_psi", {"model_name": "gamma"}) is None


async def test_record_drift_on_a_missing_version_writes_nothing(pool: Any) -> None:
    assert await ModelRegistry(pool).record_drift("nobody", 7, {"max_psi": 0.1}) is None


async def test_list_production_returns_each_names_current_version(
    pool: Any, tmp_path: Path
) -> None:
    walk = _market(_stationary_closes(_WALK_HOURS, seed=42))
    quantiles = _reference_quantiles(walk)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO backtest_jobs (id, kind, request, strategy_config) "
            "VALUES ($1, 'ml_train', $2, $3)",
            "task-drift-0003",
            '{"name": "alpha", "strategy_id": "saw", "symbol": "BTCUSDT"}',
            '{"exchange": {"exchange_id": "binance"}}',
        )
    await _register(pool, tmp_path, name="alpha", job_id="task-drift-0003", quantiles=quantiles)

    productions = await ModelRegistry(pool).list_production()

    assert [v.name for v in productions] == ["alpha"]
    assert productions[0].stage == "production"
