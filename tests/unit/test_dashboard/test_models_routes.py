"""Model routes: drift is unknown without a measurement, retrain is not implemented, uploads
are capped, validated by onnxruntime and never silently replace a model."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hydra.dashboard.routes import models
from hydra.dashboard.routes._db import install_error_handlers
from hydra.dashboard.routes.models import router


@pytest.fixture()
def models_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "models"
    path.mkdir()
    monkeypatch.setattr("hydra.dashboard.routes.models._models_dir", lambda: path)
    return path


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)
    return TestClient(app)


def _add_model(models_dir: Path, name: str, meta: dict[str, object] | None) -> None:
    (models_dir / f"{name}.onnx").write_bytes(b"onnx")
    if meta is not None:
        (models_dir / f"{name}.meta.json").write_text(json.dumps(meta))


class TestDrift:
    def test_drift_unknown_without_meta(self, models_dir: Path, client: TestClient) -> None:
        _add_model(models_dir, "x", None)
        [model] = client.get("/api/models").json()
        assert model["drift"] is None
        assert model["drift_status"] == "unknown"

    def test_drift_label_without_value_is_unknown(
        self, models_dir: Path, client: TestClient
    ) -> None:
        _add_model(models_dir, "x", {"drift_status": "low"})
        assert client.get("/api/models").json()[0]["drift_status"] == "unknown"
        assert client.get("/api/models/x").json()["drift_status"] == "unknown"

    def test_measured_drift_keeps_its_label(self, models_dir: Path, client: TestClient) -> None:
        _add_model(models_dir, "x", {"drift": 0.42, "drift_status": "high"})
        model = client.get("/api/models/x").json()
        assert model["drift"] == 0.42
        assert model["drift_status"] == "high"

    def test_invalid_label_is_unknown(self, models_dir: Path, client: TestClient) -> None:
        _add_model(models_dir, "x", {"drift": 0.1, "drift_status": "fine"})
        assert client.get("/api/models/x").json()["drift_status"] == "unknown"


class TestRetrain:
    def test_retrain_501_for_existing_model(self, models_dir: Path, client: TestClient) -> None:
        _add_model(models_dir, "x", None)
        resp = client.post("/api/models/x/retrain")
        assert resp.status_code == 501
        detail = resp.json()["detail"]
        assert detail["state"] == "not_implemented"
        assert "task_id" not in resp.text

    def test_retrain_501_for_missing_model(self, models_dir: Path, client: TestClient) -> None:
        assert client.post("/api/models/nonexistent/retrain").status_code == 501


class TestModelLookup:
    def test_list_models_empty(self, models_dir: Path, client: TestClient) -> None:
        resp = client.get("/api/models")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/api/models/nonexistent"),
            ("post", "/api/models/nonexistent/promote"),
            ("post", "/api/models/nonexistent/rollback"),
        ],
    )
    def test_not_found(self, models_dir: Path, client: TestClient, method: str, path: str) -> None:
        resp = getattr(client, method)(path)
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert detail["state"] == "not_found"
        assert detail["code"] == "model_not_found"

    def test_invalid_model_id_is_400(self, models_dir: Path, client: TestClient) -> None:
        resp = client.get("/api/models/.hidden")
        assert resp.status_code == 400
        assert resp.json()["detail"]["state"] == "invalid"


# ---------------------------------------------------------------------------
# Upload (DB-16)
# ---------------------------------------------------------------------------


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        low, n = n & 0x7F, n >> 7
        out.append(low | 0x80 if n else low)
        if not n:
            return bytes(out)


def _field(num: int, wire: int, payload: bytes | int) -> bytes:
    key = _varint((num << 3) | wire)
    if isinstance(payload, int):
        return key + _varint(payload)
    return key + _varint(len(payload)) + payload


def _identity_onnx() -> bytes:
    """A real ONNX model (y = Identity(x), float[1]) encoded by hand; onnxruntime loads it."""
    tensor = _field(1, 2, _field(1, 0, 1) + _field(2, 2, _field(1, 2, _field(1, 0, 1))))

    def value(name: str) -> bytes:
        return _field(1, 2, name.encode()) + _field(2, 2, tensor)

    node = _field(1, 2, b"x") + _field(2, 2, b"y") + _field(4, 2, b"Identity")
    graph = (
        _field(1, 2, node)
        + _field(2, 2, b"g")
        + _field(11, 2, value("x"))
        + _field(12, 2, value("y"))
    )
    return _field(1, 0, 8) + _field(8, 2, _field(2, 0, 13)) + _field(7, 2, graph)


ONNX = _identity_onnx()


def _upload(client: TestClient, content: bytes, name: str = "alpha.onnx", **params: str) -> Any:
    return client.post(
        "/api/models/upload",
        params=params,
        files={"file": (name, content, "application/octet-stream")},
    )


def _files(models_dir: Path) -> list[str]:
    return sorted(p.name for p in models_dir.iterdir())


class TestUpload:
    def test_valid_model_is_stored_as_staging(self, models_dir: Path, client: TestClient) -> None:
        resp = _upload(client, ONNX)
        assert resp.status_code == 201
        assert resp.json()["file_size"] == len(ONNX)
        assert resp.json()["stage"] == "Staging"
        assert (models_dir / "alpha.onnx").read_bytes() == ONNX
        assert json.loads((models_dir / "alpha.meta.json").read_text())["stage"] == "Staging"
        assert _files(models_dir) == ["alpha.meta.json", "alpha.onnx"]

    def test_over_the_cap_is_413_and_writes_nothing(
        self, models_dir: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    def test_existing_model_needs_replace_and_is_untouched(
        self, models_dir: Path, client: TestClient
    ) -> None:
        _add_model(models_dir, "alpha", {"stage": "Staging", "version": "v3"})
        resp = _upload(client, ONNX)
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "model_exists"
        assert (models_dir / "alpha.onnx").read_bytes() == b"onnx"
        assert json.loads((models_dir / "alpha.meta.json").read_text())["version"] == "v3"

    def test_replace_true_overwrites_a_staging_model(
        self, models_dir: Path, client: TestClient
    ) -> None:
        _add_model(models_dir, "alpha", {"stage": "Staging"})
        resp = _upload(client, ONNX, replace="true")
        assert resp.status_code == 201
        assert (models_dir / "alpha.onnx").read_bytes() == ONNX

    def test_production_model_is_never_replaced_by_upload(
        self, models_dir: Path, client: TestClient
    ) -> None:
        _add_model(models_dir, "alpha", {"stage": "Production"})
        resp = _upload(client, ONNX, replace="true")
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "model_in_production"
        assert (models_dir / "alpha.onnx").read_bytes() == b"onnx"
        assert json.loads((models_dir / "alpha.meta.json").read_text())["stage"] == "Production"

    def test_invalid_replacement_keeps_the_old_model(
        self, models_dir: Path, client: TestClient
    ) -> None:
        _add_model(models_dir, "alpha", {"stage": "Staging"})
        assert _upload(client, b"garbage", replace="true").status_code == 422
        assert (models_dir / "alpha.onnx").read_bytes() == b"onnx"
        assert _files(models_dir) == ["alpha.meta.json", "alpha.onnx"]

    @pytest.mark.parametrize("name", ["model.txt", "..onnx", ".hidden.onnx"])
    def test_bad_names_are_rejected(self, models_dir: Path, client: TestClient, name: str) -> None:
        assert _upload(client, ONNX, name=name).status_code in (400, 422)
        assert _files(models_dir) == []
