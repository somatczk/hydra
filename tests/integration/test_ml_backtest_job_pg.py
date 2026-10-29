"""The ML-consuming backtest job on real TimescaleDB: the worker attaches before it runs.

The session-start gate (P5-C) also guards the operator-facing backtest path:
``run_backtest_job`` — where the pool is — validates and attaches every model
the config consumes (an enabled ``ml_overlay`` or the ML-native strategy)
before the runner replays a single bar, so the model the backtest serves is
the one the registry pins, exactly as a session start would. A
registered-and-promoted fixture model completes with REAL trades (the native
strategy's own entries) and REAL veto decisions (the overlay's
``shadow_veto`` — the model answered, it did not error); an un-promoted name
or a Production row whose artifact is not on the models mount fails the job
loudly with the model named — never the pre-fix shape, a green run with every
entry vetoed ``error`` (zero trades, flat equity).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import yaml
from fastapi import FastAPI

from hydra.backtest import worker
from hydra.core.logging import setup_logging
from hydra.core.types import OHLCV, Timeframe
from hydra.dashboard.routes import backtest as backtest_routes
from hydra.dashboard.routes._db import install_error_handlers
from hydra.data.migrations.runner import upgrade_to_head
from hydra.data.storage import MarketDataRepository
from hydra.ml.consumers import _reset_overlays
from hydra.ml.features import DEFAULT_FEATURE_SPEC
from hydra.ml.registry import ModelRegistration, ModelRegistry
from hydra.ml.serving import _reset_attached
from hydra.strategy.ml_access import _reset_providers
from tests.fixtures.ml_onnx import confidence_onnx, frozen_spec_metadata

pytestmark = pytest.mark.integration

H1_START = datetime(2026, 1, 1, tzinfo=UTC)
H = timedelta(hours=1)
BARS = 2400  # the frozen spec's 1h window (2000) plus traded bars on both sides
N_FEATURES = len(DEFAULT_FEATURE_SPEC.feature_names)
MODEL = "alpha"
ML_NATIVE = "hydra.strategy.builtin.ml_ensemble.MLEnsembleStrategy"
TRAINED_AT = datetime(2026, 10, 28, 12, tzinfo=UTC)

_SAW_RULES = {
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _heartbeat_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "worker.heartbeat"
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", str(path))
    return path


@pytest.fixture(autouse=True)
def _isolate_ml_state() -> AsyncIterator[None]:
    """The worker runs in-process here: its attach cache resets per test."""
    _reset_attached()
    _reset_providers()
    _reset_overlays()
    yield
    _reset_attached()
    _reset_providers()
    _reset_overlays()


@pytest.fixture()
async def pool(fresh_database: str) -> AsyncIterator[Any]:
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    db_pool = await asyncpg.create_pool(fresh_database, min_size=1, max_size=4)
    try:
        yield db_pool
    finally:
        await db_pool.close()


@pytest.fixture()
def models_dir() -> Path:
    """The models mount the worker reads (HYDRA_ML__MODELS_DIR, set by conftest)."""
    return Path(os.environ["HYDRA_ML__MODELS_DIR"])


@pytest.fixture()
async def client(pool: Any) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(backtest_routes.router)
    install_error_handlers(app)
    app.state.db_pool = pool
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _bar(ts: datetime, close: float) -> OHLCV:
    price = str(close)
    return OHLCV(
        open=Decimal(price),
        high=Decimal(price),
        low=Decimal(price),
        close=Decimal(price),
        volume=Decimal("10"),
        timestamp=ts,
    )


def _rising() -> list[OHLCV]:
    """Strictly rising closes: RSI(14) = 100, so the permissive model answers 1.0."""
    return [_bar(H1_START + i * H, 1000.0 + 5.0 * i) for i in range(BARS)]


def _sawtooth() -> list[OHLCV]:
    """The deterministic saw: close crosses sma:5 once per 20-bar tooth."""
    return [_bar(H1_START + i * H, 1000.0 + (i % 20) * 5.0) for i in range(BARS)]


async def _seed(pool: Any, bars: list[OHLCV]) -> None:
    repo = MarketDataRepository.from_pool(pool)
    await repo.store_bars([("binance", "BTCUSDT", Timeframe.H1, b) for b in bars])


async def _register(
    pool: Any, models_dir: Path, name: str, *, scale: float = 0.01, promote: bool = True
) -> None:
    meta = frozen_spec_metadata(list(DEFAULT_FEATURE_SPEC.feature_names), 3, 4)
    blob = confidence_onnx(meta, N_FEATURES, scale=scale)
    models_dir.mkdir(parents=True, exist_ok=True)
    artifact = models_dir / f"{name}.onnx"
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
    if promote:
        await ModelRegistry(pool).promote(name, version.version)


def _write_native(strategies_dir: Path) -> str:
    data = {
        "id": "native",
        "name": "Native",
        "strategy_class": ML_NATIVE,
        "symbols": ["BTCUSDT"],
        "exchange": {"exchange_id": "binance", "market_type": "SPOT"},
        "timeframes": {"primary": "1h", "confirmation": "4h", "entry": "1d"},
        "parameters": {
            "model_name": MODEL,
            "entry_threshold": 0.6,
            "exit_threshold": 0.4,
            "required_history": 2000,
        },
    }
    (strategies_dir / "native.yaml").write_text(yaml.safe_dump(data))
    return "native"


def _write_gated(strategies_dir: Path, *, shadow: bool) -> str:
    data = {
        "id": "gated",
        "name": "Gated",
        "strategy_class": "hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        "symbols": ["BTCUSDT"],
        "exchange": {"exchange_id": "binance", "market_type": "SPOT"},
        "timeframes": {"primary": "1h", "confirmation": "4h", "entry": "1d"},
        "parameters": {"rules": _SAW_RULES, "required_history": 2000},
        "ml_overlay": {
            "enabled": True,
            "model_name": MODEL,
            "confidence_threshold": 0.6,
            "shadow": shadow,
        },
    }
    (strategies_dir / "gated.yaml").write_text(yaml.safe_dump(data))
    return "gated"


async def _run_job(client: httpx.AsyncClient, pool: Any, strategy_id: str) -> dict[str, Any]:
    """Submit one backtest, run it on the worker, and return its terminal status."""
    resp = await client.post(
        "/api/backtest/run",
        json={"strategy_id": strategy_id, "start_date": "2026-01-01", "end_date": "2026-04-01"},
    )
    assert resp.status_code == 202, resp.text
    task_id = resp.json()["task_id"]
    assert await worker.run_once(pool) == task_id
    status = (await client.get(f"/api/backtest/status/{task_id}")).json()
    assert status["status"] in ("completed", "failed"), status
    return status


# ---------------------------------------------------------------------------
# The attach: a served model trades (and vetoes) for real
# ---------------------------------------------------------------------------


class TestTheWorkerAttaches:
    async def test_a_native_backtest_attaches_and_completes_with_real_trades(
        self, client: httpx.AsyncClient, pool: Any, models_dir: Path, strategies_dir: Path
    ) -> None:
        await _seed(pool, _rising())
        await _register(pool, models_dir, MODEL)  # scale 0.01: rising -> P(up) = 1.0
        strategy_id = _write_native(strategies_dir)

        status = await _run_job(client, pool, strategy_id)

        assert status["status"] == "completed", status["error"]
        detail = (await client.get(f"/api/backtest/results/{status['result_id']}")).json()
        # the model's own entry fired and filled: P(up) = 1.0 >= 0.6 -> LONG
        assert detail["metrics"]["total_trades"] >= 1
        assert detail["trades"][0]["side"] == "LONG"

    async def test_an_overlay_backtest_decides_with_the_model_never_errors(
        self,
        client: httpx.AsyncClient,
        pool: Any,
        models_dir: Path,
        strategies_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        setup_logging("INFO", "json")
        await _seed(pool, _sawtooth())
        # scale 0.001: the saw's RSI answers ~0.09, below the 0.6 threshold —
        # every entry is a would-veto; shadow lets it through so the run itself
        # proves the model decided (an error veto would suppress even in shadow)
        await _register(pool, models_dir, MODEL, scale=0.001)
        strategy_id = _write_gated(strategies_dir, shadow=True)

        status = await _run_job(client, pool, strategy_id)

        assert status["status"] == "completed", status["error"]
        detail = (await client.get(f"/api/backtest/results/{status['result_id']}")).json()
        assert detail["metrics"]["total_trades"] >= 1
        out = capsys.readouterr().out
        assert '"event": "ml_overlay_decision"' in out
        assert '"decision": "shadow_veto"' in out
        assert '"decision": "error"' not in out

    async def test_an_unpromoted_overlay_backtest_fails_the_job(
        self, client: httpx.AsyncClient, pool: Any, models_dir: Path, strategies_dir: Path
    ) -> None:
        await _seed(pool, _sawtooth())
        await _register(pool, models_dir, MODEL, promote=False)  # staging only
        strategy_id = _write_gated(strategies_dir, shadow=False)

        status = await _run_job(client, pool, strategy_id)

        # the loud refusal, never a completed zero-trade run
        assert status["status"] == "failed"
        assert status["result_id"] is None
        assert MODEL in (status["error"] or "")
        assert "no Production version" in (status["error"] or "")

    async def test_a_missing_artifact_native_backtest_fails_loudly(
        self, client: httpx.AsyncClient, pool: Any, models_dir: Path, strategies_dir: Path
    ) -> None:
        await _seed(pool, _rising())
        await _register(pool, models_dir, MODEL)
        strategy_id = _write_native(strategies_dir)
        # the mount loses the artifact between the promote and the job
        (models_dir / f"{MODEL}.onnx").unlink()

        status = await _run_job(client, pool, strategy_id)

        # the §7 deploy-risk shape: the job fails with the model named, never
        # the pre-fix MlModelNotAttachedError at on_start
        assert status["status"] == "failed"
        assert status["result_id"] is None
        assert f"{MODEL}.onnx" in (status["error"] or "")
        assert "refuses rather than trade without the model" in (status["error"] or "")
