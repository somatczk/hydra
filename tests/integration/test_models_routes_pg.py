"""The models routes over the DB-backed registry on real TimescaleDB (P5-B).

Versions are immutable rows, promote/rollback are the registry's own
transactions, uploads register server-named artifacts, and a retrain submits a
real ``ml_train`` job whose worker round-trip (train -> ONNX export -> parity
gate -> register) runs against this database. XGBoost/onnxmltools are faked at
the ``sys.modules`` seam (CI installs only the dev extra); onnxruntime, the
registry, the queue and the routes are real.

The app is driven with httpx's ASGI transport on the pytest event loop — the
same loop the asyncpg pool lives on. A starlette TestClient would run the route
coroutines on its own portal loop and every pool operation would fail with
asyncpg's ``got Future attached to a different loop``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import asyncpg
import httpx
import pytest
import yaml
from fastapi import FastAPI

from hydra.backtest import worker
from hydra.core.config import load_config
from hydra.core.types import OHLCV, Timeframe
from hydra.dashboard import deps
from hydra.dashboard.routes._db import install_error_handlers
from hydra.dashboard.routes.models import router
from hydra.data.migrations.runner import upgrade_to_head
from hydra.data.storage import MarketDataRepository
from hydra.ml.registry import ModelRegistry
from tests.fixtures.ml_onnx import FakeModelProto, fake_onnxmltools, fake_xgboost, reduce_max_onnx

pytestmark = pytest.mark.integration

T0 = datetime(2026, 1, 1, tzinfo=UTC)
H = timedelta(hours=1)
N_1H = 1350
N_4H = 340
N_1D = 55

ONNX = reduce_max_onnx({"spec_version": "3", "feature_names": '["f0"]', "horizon": "4"})

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
    return Path(os.environ["HYDRA_ML__MODELS_DIR"])


@pytest.fixture()
async def client(pool: Any, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)
    app.state.db_pool = pool
    cfg = load_config()
    monkeypatch.setattr(deps, "_bound", cfg)
    app.state.config = cfg
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


@pytest.fixture()
def strategy(strategies_dir: Path) -> str:
    data = {
        "id": "saw",
        "name": "Saw",
        "strategy_class": "hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        "symbols": ["BTCUSDT"],
        "exchange": {"exchange_id": "binance", "market_type": "SPOT"},
        "timeframes": {"primary": "1h"},
        "parameters": {"rules": _RULES, "required_history": 10},
    }
    (strategies_dir / "saw.yaml").write_text(yaml.safe_dump(data))
    return "saw"


async def _seed_universe(pool: Any) -> None:
    value = {
        "exchange_id": "binance",
        "quote_asset": "USDT",
        "source": "test",
        "ranked_at": datetime.now(UTC).isoformat(),
        "count": 1,
        "symbols": [{"rank": 1, "symbol": "BTCUSDT", "quote_volume": 1.0}],
    }
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO system_config (key, value, updated_at, updated_by) "
            "VALUES ('universe', $1::jsonb, now(), 'test')",
            json.dumps(value),
        )


def _saw_bars(count: int, step: timedelta) -> list[OHLCV]:
    return [
        OHLCV(
            open=Decimal(1000 + (i % 20) * 5),
            high=Decimal(1000 + (i % 20) * 5),
            low=Decimal(1000 + (i % 20) * 5),
            close=Decimal(1000 + (i % 20) * 5),
            volume=Decimal(10),
            timestamp=T0 + i * step,
        )
        for i in range(count)
    ]


async def _seed_bars(pool: Any) -> None:
    repo = MarketDataRepository.from_pool(pool)
    await repo.store_bars([("binance", "BTCUSDT", Timeframe.H1, b) for b in _saw_bars(N_1H, H)])
    await repo.store_bars([("binance", "BTCUSDT", Timeframe.H4, b) for b in _saw_bars(N_4H, 4 * H)])
    await repo.store_bars(
        [("binance", "BTCUSDT", Timeframe.D1, b) for b in _saw_bars(N_1D, 24 * H)]
    )


async def _upload(
    client: httpx.AsyncClient, content: bytes = ONNX, name: str = "alpha.onnx", **params: str
) -> httpx.Response:
    return await client.post(
        "/api/models/upload",
        params=params,
        files={"file": (name, content, "application/octet-stream")},
    )


async def _stages(client: httpx.AsyncClient, name: str) -> dict[int, str]:
    listing = (await client.get("/api/models")).json()
    return {m["version"]: m["stage"] for m in listing if m["name"] == name}


# ---------------------------------------------------------------------------
# Upload: immutable versions, server-named artifacts
# ---------------------------------------------------------------------------


class TestUpload:
    async def test_each_upload_registers_the_next_immutable_version(
        self, client: httpx.AsyncClient, models_dir: Path
    ) -> None:
        first = await _upload(client)
        assert first.status_code == 201, first.text
        second = await _upload(client)
        assert second.status_code == 201, second.text
        assert first.json()["version"] == 1
        assert second.json()["version"] == 2

        # two distinct server-named artifacts, each hashing to its registered sha256
        names = {first.json()["file_name"], second.json()["file_name"]}
        assert len(names) == 2
        for name in names:
            assert name.removesuffix(".onnx").isalnum()
            assert (models_dir / name).read_bytes() == ONNX
        row = (await client.get(f"/api/models/{second.json()['id']}")).json()
        assert row["artifact_sha256"] == hashlib.sha256(ONNX).hexdigest()
        assert row["stage"] == "Staging"
        assert row["model_type"] == "onnx"
        assert row["feature_spec"] == {"spec_version": 3, "feature_names": ["f0"], "horizon": 4}
        assert await _stages(client, "alpha") == {1: "Staging", 2: "Staging"}

    async def test_a_sha256_pin_over_other_bytes_is_refused_and_stores_nothing(
        self, client: httpx.AsyncClient, models_dir: Path
    ) -> None:
        assert (await _upload(client)).status_code == 201
        before = set(models_dir.iterdir())
        resp = await _upload(client, sha256="0" * 64)
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "sha256_mismatch"
        assert set(models_dir.iterdir()) == before
        assert await _stages(client, "alpha") == {1: "Staging"}

    async def test_not_an_onnx_model_is_refused(self, client: httpx.AsyncClient) -> None:
        resp = await _upload(client, b"not a model")
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "invalid_onnx"
        assert (await client.get("/api/models")).json() == []


# ---------------------------------------------------------------------------
# Promote / rollback: the registry's stage transitions through the routes
# ---------------------------------------------------------------------------


class TestPromoteRollback:
    async def test_promote_demotes_and_rollback_restores_the_previous_production(
        self, client: httpx.AsyncClient
    ) -> None:
        ids = [(await _upload(client)).json()["id"] for _ in range(3)]
        assert await _stages(client, "alpha") == {1: "Staging", 2: "Staging", 3: "Staging"}

        resp = await client.post(f"/api/models/{ids[0]}/promote")
        assert resp.status_code == 200
        assert resp.json()["version"] == 1
        assert resp.json()["stage"] == "Production"
        assert await _stages(client, "alpha") == {1: "Production", 2: "Staging", 3: "Staging"}

        assert (await client.post(f"/api/models/{ids[1]}/promote")).status_code == 200
        assert await _stages(client, "alpha") == {1: "Archived", 2: "Production", 3: "Staging"}

        rolled = await client.post(f"/api/models/{ids[2]}/rollback")
        assert rolled.status_code == 200
        assert rolled.json()["restored_version"] == 1
        assert await _stages(client, "alpha") == {1: "Production", 2: "Archived", 3: "Staging"}

    async def test_a_rollback_with_nothing_archived_is_refused(
        self, client: httpx.AsyncClient
    ) -> None:
        model_id = (await _upload(client)).json()["id"]
        assert (await client.post(f"/api/models/{model_id}/promote")).status_code == 200
        resp = await client.post(f"/api/models/{model_id}/rollback")
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "rollback_refused"
        assert await _stages(client, "alpha") == {1: "Production"}

    async def test_an_unknown_uuid_is_404_and_a_non_uuid_is_400(
        self, client: httpx.AsyncClient
    ) -> None:
        await _upload(client)
        assert (
            await client.post("/api/models/00000000-0000-0000-0000-000000000000/promote")
        ).status_code == 404
        assert (await client.get("/api/models/not-a-uuid")).status_code == 400

    async def test_list_and_get_shapes_the_ui_consumes(self, client: httpx.AsyncClient) -> None:
        model_id = (await _upload(client)).json()["id"]
        [listed] = (await client.get("/api/models")).json()
        assert listed["id"] == model_id
        assert listed["name"] == "alpha"
        assert listed["drift"] is None
        assert listed["drift_status"] == "unknown"
        assert listed["file_size"] == len(ONNX)
        assert listed["file_name"].endswith(".onnx")
        detail = (await client.get(f"/api/models/{model_id}")).json()
        assert detail["metrics"] == {}
        assert detail["created_by"].startswith("upload:")


# ---------------------------------------------------------------------------
# The retrain round-trip: submit -> worker trains -> ONNX -> registry
# ---------------------------------------------------------------------------


class TestRetrainRoundTrip:
    async def test_submit_train_register_and_read_back(
        self,
        pool: Any,
        client: httpx.AsyncClient,
        strategy: str,
        models_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _seed_universe(pool)
        await _seed_bars(pool)
        monkeypatch.setattr(worker, "HEARTBEAT_PATH", str(tmp_path / "heartbeat"))

        resp = await client.post(
            "/api/models/champ/retrain",
            json={
                "strategy_id": strategy,
                "symbol": "BTCUSDT",
                "horizon": 1,
                "window_1h": N_1H,
                "window_4h": N_4H,
                "window_1d": N_1D,
            },
        )
        assert resp.status_code == 202, resp.text
        task_id = resp.json()["task_id"]
        assert task_id.startswith("ml-")

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT kind, status FROM backtest_jobs WHERE id = $1", task_id
            )
        assert (row["kind"], row["status"]) == ("ml_train", "queued")

        with patch.dict("sys.modules", {**fake_xgboost()[0], **fake_onnxmltools(FakeModelProto())}):
            claimed = await worker.run_once(pool)
        assert claimed == task_id

        status = (await client.get(f"/api/models/retrain/{task_id}")).json()
        assert status["status"] == "completed"
        assert status["progress"] == 100.0
        assert status["error"] is None
        result = status["result"]
        assert result["name"] == "champ"
        assert result["version"] == 1
        assert result["stage"] == "staging"
        assert result["artifact_file"] == f"{task_id}.onnx"
        assert result["training_job_id"] == task_id
        metrics = result["metrics"]
        assert metrics["folds"] >= 1
        assert 0.0 <= metrics["oos_accuracy"] <= 1.0
        assert status["metrics"] == metrics
        assert status["provenance"]["coverage"][0]["sources"] == {"feed": N_1H}
        assert status["provenance"]["coverage"][0]["bar_count"] == N_1H

        # the registry row and its artifact, addressed by registry identity
        [version] = (await client.get("/api/models")).json()
        assert version["name"] == "champ"
        assert version["stage"] == "Staging"
        assert version["training_job_id"] == task_id
        artifact = models_dir / version["artifact_file"]
        assert version["artifact_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()

        # the ONNX is real and its spec metadata reads back through onnxruntime
        import onnxruntime as ort

        session = ort.InferenceSession(str(artifact), providers=["CPUExecutionProvider"])
        metadata = session.get_modelmeta().custom_metadata_map
        assert metadata["feature_version"] == "3"
        assert metadata["horizon"] == "1"
        assert json.loads(metadata["feature_names"])

        # a second retrain is the next challenger version
        second = await client.post(
            "/api/models/champ/retrain",
            json={"strategy_id": strategy, "symbol": "BTCUSDT", "window_1h": N_1H},
        )
        assert second.status_code == 202
        with patch.dict("sys.modules", {**fake_xgboost()[0], **fake_onnxmltools(FakeModelProto())}):
            await worker.run_once(pool)
        assert await _stages(client, "champ") == {1: "Staging", 2: "Staging"}

    async def test_a_queued_retrain_cancels(
        self, pool: Any, client: httpx.AsyncClient, strategy: str
    ) -> None:
        await _seed_universe(pool)
        resp = await client.post(
            "/api/models/champ/retrain",
            json={"strategy_id": strategy, "symbol": "BTCUSDT"},
        )
        task_id = resp.json()["task_id"]

        cancelled = await client.post(f"/api/models/retrain/{task_id}/cancel")

        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        status = (await client.get(f"/api/models/retrain/{task_id}")).json()
        assert status["status"] == "cancelled"
        assert await ModelRegistry(pool).list("champ") == []

    async def test_the_bounded_payload_is_refused_at_submit(
        self, client: httpx.AsyncClient, strategy: str
    ) -> None:
        resp = await client.post(
            "/api/models/champ/retrain",
            json={"strategy_id": strategy, "symbol": "BTCUSDT", "window_1h": 10**9},
        )
        assert resp.status_code == 422
        resp = await client.post(
            "/api/models/champ/retrain",
            json={"strategy_id": strategy, "symbol": "BTCUSDT", "timeframe": "4h"},
        )
        assert resp.status_code == 422

    async def test_a_symbol_outside_the_universe_is_refused(
        self, pool: Any, client: httpx.AsyncClient, strategy: str
    ) -> None:
        await _seed_universe(pool)
        resp = await client.post(
            "/api/models/champ/retrain",
            json={"strategy_id": strategy, "symbol": "NOPEUSDT"},
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "symbol_not_in_universe"
        async with pool.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM backtest_jobs")
        assert count == 0
