"""Model routes: drift is unknown without a measurement, retrain is not implemented."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
