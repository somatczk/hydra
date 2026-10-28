"""Model routes over the registry: ids are UUIDs, names are plain identifiers, uploads are
capped, validated by onnxruntime and never compose a path from request data.

The registry's own semantics (versions, promote/rollback, the round-trip through a
real ml_train job) run against TimescaleDB in tests/integration/test_models_routes_pg.py;
these unit tests pin the validation, refusal and transport behaviour.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hydra.dashboard.routes import models as routes_models
from hydra.dashboard.routes._db import install_error_handlers
from hydra.dashboard.routes.models import (
    _drift_fields,
    _version_response,
    router,
)
from hydra.ml.registry import ModelVersion
from tests.fixtures.ml_onnx import reduce_max_onnx

ONNX = reduce_max_onnx({"spec_version": "3", "feature_names": '["f0"]', "horizon": "4"})


# ---------------------------------------------------------------------------
# Fakes: an app whose pool answers SELECTs with nothing and echoes INSERTs
# ---------------------------------------------------------------------------


class _Acquire:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _NullConn:
    """SELECTs find nothing (except the universe row); INSERTs are echoed/recorded."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.universe: dict[str, Any] | None = {
            "exchange_id": "binance",
            "quote_asset": "USDT",
            "source": "test",
            "ranked_at": "2026-10-28T00:00:00+00:00",
            "count": 1,
            "symbols": [{"rank": 1, "symbol": "BTCUSDT", "quote_volume": 1.0}],
        }

    async def fetchrow(self, query: str, *args: Any) -> Any:
        if "INSERT INTO ml_models" in query:
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
        if "FROM system_config" in query:
            return {"value": json.dumps(self.universe)} if self.universe else None
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return []

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append((query, args))
        return "OK"


class _NullPool:
    def __init__(self) -> None:
        self.conn = _NullConn()

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


@pytest.fixture()
def pool() -> _NullPool:
    return _NullPool()


@pytest.fixture()
def client(pool: _NullPool) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)
    app.state.db_pool = pool
    return TestClient(app)


def _no_pool_client() -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)
    return TestClient(app)


@pytest.fixture()
def models_dir(tmp_path: Path) -> Path:
    path = tmp_path / "models"
    path.mkdir()
    return path


def _upload(client: TestClient, content: bytes, name: str = "alpha.onnx", **params: str) -> Any:
    return client.post(
        "/api/models/upload",
        params=params,
        files={"file": (name, content, "application/octet-stream")},
    )


def _files(models_dir: Path) -> list[str]:
    return sorted(p.name for p in models_dir.iterdir())


def _version(
    **overrides: Any,
) -> ModelVersion:
    base: dict[str, Any] = dict(
        id=uuid4(),
        name="alpha",
        version=1,
        stage="staging",
        model_type="xgboost",
        metrics={"folds": 5, "oos_accuracy": 0.51},
        feature_spec={"spec_version": 3},
        drift=None,
        artifact_file="abc123.onnx",
        artifact_sha256="0" * 64,
        trained_at=datetime(2026, 10, 28, 12, tzinfo=UTC),
        created_by="job:ml-abc12345",
        training_job_id="ml-abc12345",
        archived_at=None,
    )
    base.update(overrides)
    return ModelVersion(**base)


# ---------------------------------------------------------------------------
# The drift label rule and the stage mapping (the response shape the UI reads)
# ---------------------------------------------------------------------------


class TestDriftFields:
    def test_no_measurement_is_unknown(self) -> None:
        assert _drift_fields(_version(drift=None)) == (None, "unknown")
        assert _drift_fields(_version(drift={})) == (None, "unknown")
        assert _drift_fields(_version(drift={"status": "high"})) == (None, "unknown")

    def test_a_label_is_only_trusted_when_a_value_backs_it(self) -> None:
        assert _drift_fields(_version(drift={"max_psi": 0.42, "status": "high"})) == (0.42, "high")
        assert _drift_fields(_version(drift={"max_psi": 0.1, "status": "fine"})) == (0.1, "unknown")
        assert _drift_fields(_version(drift={"max_psi": "lots", "status": "low"})) == (
            None,
            "unknown",
        )

    def test_the_alternative_label_key_is_read(self) -> None:
        assert _drift_fields(_version(drift={"max_psi": 0.3, "drift_status": "moderate"})) == (
            0.3,
            "moderate",
        )


class TestVersionResponse:
    def test_the_stage_and_drift_mapping_the_ui_expects(self, tmp_path: Path) -> None:
        response = _version_response(_version())
        assert response["stage"] == "Staging"
        assert response["drift"] is None
        assert response["drift_status"] == "unknown"
        assert response["file_name"] == "abc123.onnx"
        assert response["file_size"] == 0  # the artifact is not on this node's mount
        assert response["trained_at"] == "2026-10-28T12:00:00+00:00"
        assert response["training_job_id"] == "ml-abc12345"

    def test_every_stage_has_its_label(self) -> None:
        assert _version_response(_version(stage="production"))["stage"] == "Production"
        assert _version_response(_version(stage="archived"))["stage"] == "Archived"

    def test_the_artifact_size_is_read_from_a_validated_file_only(self, tmp_path: Path) -> None:
        artifact = tmp_path / "abc123.onnx"
        artifact.write_bytes(b"0123456789")
        with patch.object(routes_models, "_models_dir", return_value=tmp_path):
            assert _version_response(_version())["file_size"] == 10
            # a stored filename that is not a plain .onnx name never joins the dir
            assert _version_response(_version(artifact_file="../../etc/passwd"))["file_size"] == 0
            assert _version_response(_version(artifact_file="nope.txt"))["file_size"] == 0


# ---------------------------------------------------------------------------
# Lookup: the only address is the registry UUID
# ---------------------------------------------------------------------------


class TestModelLookup:
    def test_list_without_a_pool_is_503(self) -> None:
        resp = _no_pool_client().get("/api/models")
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "database_unavailable"

    def test_list_over_an_empty_registry(self, client: TestClient) -> None:
        assert client.get("/api/models").json() == []

    @pytest.mark.parametrize(
        "model_id",
        [
            "not-a-uuid",
            "abc123.onnx",
            "alpha",
        ],
    )
    def test_a_non_uuid_id_is_400_and_never_reaches_the_fileystem(
        self, client: TestClient, model_id: str
    ) -> None:
        for method, suffix in (("get", ""), ("post", "/promote"), ("post", "/rollback")):
            resp = getattr(client, method)(f"/api/models/{model_id}{suffix}")
            assert resp.status_code == 400, (model_id, suffix, resp.text)
            assert resp.json()["detail"]["state"] == "invalid"

    def test_an_unknown_uuid_is_404(self, client: TestClient) -> None:
        model_id = str(uuid4())
        resp = client.get(f"/api/models/{model_id}")
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert detail["code"] == "model_not_found"
        assert detail["state"] == "not_found"
        assert client.post(f"/api/models/{model_id}/promote").status_code == 404
        assert client.post(f"/api/models/{model_id}/rollback").status_code == 404


# ---------------------------------------------------------------------------
# Retrain: the submit surface's validation
# ---------------------------------------------------------------------------

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


class TestRetrainSubmit:
    def test_a_valid_submit_queues_the_ml_train_job(
        self, strategies_dir: Path, client: TestClient, pool: _NullPool
    ) -> None:
        _write_strategy(strategies_dir, "saw")
        resp = client.post(
            "/api/models/alpha/retrain",
            json={
                "strategy_id": "saw",
                "symbol": "BTCUSDT",
                "horizon": 2,
                "window_1h": 500,
            },
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "queued"
        assert body["task_id"].startswith("ml-")
        ((query, args),) = pool.conn.executed
        assert "INSERT INTO backtest_jobs" in query
        assert args[1] == "ml_train"
        request = json.loads(args[2])
        assert request["name"] == "alpha"
        assert request["strategy_id"] == "saw"
        assert request["symbol"] == "BTCUSDT"
        assert request["horizon"] == 2
        assert request["window_1h"] == 500
        assert json.loads(args[3])["exchange"]["exchange_id"] == "binance"

    def test_a_symbol_outside_the_universe_is_422(
        self, strategies_dir: Path, client: TestClient
    ) -> None:
        _write_strategy(strategies_dir, "saw")
        resp = client.post(
            "/api/models/alpha/retrain",
            json={"strategy_id": "saw", "symbol": "NOPEUSDT"},
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "symbol_not_in_universe"

    def test_without_a_universe_row_it_fails_closed_503(
        self, strategies_dir: Path, client: TestClient, pool: _NullPool
    ) -> None:
        _write_strategy(strategies_dir, "saw")
        pool.conn.universe = None
        resp = client.post(
            "/api/models/alpha/retrain",
            json={"strategy_id": "saw", "symbol": "BTCUSDT"},
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "universe_unavailable"

    def test_an_unknown_strategy_is_404(self, client: TestClient) -> None:
        resp = client.post(
            "/api/models/alpha/retrain",
            json={"strategy_id": "nobody", "symbol": "BTCUSDT"},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "strategy_not_found"

    def test_a_bad_model_name_is_400_before_anything(self, client: TestClient) -> None:
        resp = client.post(
            "/api/models/.hidden/retrain",
            json={"strategy_id": "saw", "symbol": "BTCUSDT"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "invalid_model_name"

    def test_a_broken_strategy_config_is_422(self, strategies_dir: Path) -> None:
        (strategies_dir / "broken.yaml").write_text("id: broken\nname: Broken\n")
        resp = _no_pool_client().post(
            "/api/models/alpha/retrain",
            json={"strategy_id": "broken", "symbol": "BTCUSDT"},
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "invalid_strategy"

    def test_the_status_of_an_unknown_task_is_404(self, client: TestClient) -> None:
        resp = client.get("/api/models/retrain/ml-nobody00")
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "task_not_found"


# ---------------------------------------------------------------------------
# Upload (DB-16): capped, onnxruntime-validated, sha256-pinned, path-free
# ---------------------------------------------------------------------------


class TestUpload:
    def test_over_the_cap_is_413_and_writes_nothing(
        self,
        models_dir: Path,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from hydra.dashboard.routes import models

        monkeypatch.setattr(models, "MAX_MODEL_BYTES", 1024)
        monkeypatch.setattr(models, "_CHUNK_BYTES", 256)
        resp = _upload(client, b"\0" * 2048)
        assert resp.status_code == 413
        assert resp.json()["detail"]["code"] == "model_too_large"
        assert resp.json()["detail"]["max_bytes"] == 1024
        assert _files(models_dir) == []

    def test_not_an_onnx_model_is_422_and_writes_nothing(
        self, models_dir: Path, client: TestClient
    ) -> None:
        resp = _upload(client, b"not a model")
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "invalid_onnx"
        assert _files(models_dir) == []

    def test_empty_file_is_422(self, models_dir: Path, client: TestClient) -> None:
        assert _upload(client, b"").status_code == 422
        assert _files(models_dir) == []

    def test_a_sha256_pin_over_other_bytes_is_422_and_writes_nothing(
        self, models_dir: Path, client: TestClient
    ) -> None:
        resp = _upload(client, ONNX, sha256="0" * 64)
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["code"] == "sha256_mismatch"
        assert detail["actual_sha256"] != "0" * 64
        assert _files(models_dir) == []

    @pytest.mark.parametrize("name", ["model.txt", "..onnx", ".hidden.onnx"])
    def test_bad_names_are_rejected_before_any_file_is_written(
        self, models_dir: Path, client: TestClient, name: str
    ) -> None:
        resp = _upload(client, ONNX, name=name)
        assert resp.status_code in (400, 422), name
        assert _files(models_dir) == []

    def test_a_traversal_filename_never_names_the_artifact(
        self, models_dir: Path, client: TestClient
    ) -> None:
        """The basename is taken for the registry NAME; the artifact stays server-named."""
        resp = _upload(client, ONNX, name="../../etc/passwd.onnx")
        assert resp.status_code == 201, resp.text
        assert _files(models_dir) and all(
            f.endswith(".onnx") and f.removesuffix(".onnx").isalnum() for f in _files(models_dir)
        )
        assert not (models_dir / "passwd.onnx").exists()

    def test_a_valid_model_registers_the_next_version(
        self, models_dir: Path, client: TestClient
    ) -> None:
        resp = _upload(client, ONNX)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["name"] == "alpha"
        assert body["version"] == 1
        assert body["stage"] == "Staging"
        assert body["file_size"] == len(ONNX)
        artifact_name = body["file_name"]
        assert artifact_name.removesuffix(".onnx").isalnum()  # a server-generated name
        assert (models_dir / artifact_name).read_bytes() == ONNX


_BOUNDARY = "hydra-test-boundary"


def _multipart(content: bytes, name: str = "alpha.onnx") -> bytes:
    head = (
        f'--{_BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    return head + content + f"\r\n--{_BOUNDARY}--\r\n".encode()


class TestUploadBodyBound:
    """The cap holds at the transport: the multipart parser never spools an oversized body."""

    @pytest.fixture(autouse=True)
    def small_cap(self, monkeypatch: pytest.MonkeyPatch) -> list[int]:
        from hydra.dashboard.routes import models

        monkeypatch.setattr(models, "MAX_MODEL_BYTES", 1024)
        monkeypatch.setattr(models, "MULTIPART_OVERHEAD_BYTES", 512)
        spooled: list[int] = []

        async def spy(file: Any, dest: Path) -> int:
            spooled.append(1)
            raise AssertionError("the handler ran on an oversized body")

        monkeypatch.setattr(models, "_spool", spy)
        return spooled

    def test_declared_length_over_the_bound_is_413_before_parsing(
        self, models_dir: Path, client: TestClient, small_cap: list[int]
    ) -> None:
        body = _multipart(b"\0" * 4096)
        resp = client.post(
            "/api/models/upload",
            content=body,
            headers={"content-type": f"multipart/form-data; boundary={_BOUNDARY}"},
        )
        assert resp.status_code == 413
        assert resp.json()["detail"]["code"] == "model_too_large"
        assert small_cap == []
        assert _files(models_dir) == []

    def test_chunked_body_is_counted_and_cut_off(
        self, models_dir: Path, client: TestClient, small_cap: list[int]
    ) -> None:
        body = _multipart(b"\0" * 4096)
        sent: list[int] = []

        def chunks() -> Iterator[bytes]:
            for i in range(0, len(body), 256):
                sent.append(i)
                yield body[i : i + 256]

        resp = client.post(
            "/api/models/upload",
            content=chunks(),
            headers={"content-type": f"multipart/form-data; boundary={_BOUNDARY}"},
        )
        assert resp.status_code == 413
        assert resp.json()["detail"]["code"] == "model_too_large"
        assert small_cap == []
        assert _files(models_dir) == []
