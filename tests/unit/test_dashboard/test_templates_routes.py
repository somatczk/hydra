"""Tests for the templates dashboard routes: listing and importing (P2-E2 validation)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hydra.dashboard.routes.templates import _TEMPLATES_DIR, router
from hydra.strategy.engine import validate_strategy_config

TEMPLATE_IDS = sorted(p.stem for p in _TEMPLATES_DIR.glob("*.yaml"))


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_import_each_shipped_template_is_201_and_valid(
    template_id: str, strategies_dir: Path
) -> None:
    resp = _client().post(f"/api/templates/{template_id}/import")
    assert resp.status_code == 201
    data = resp.json()
    written = strategies_dir / f"{data['id']}.yaml"
    assert written.is_file()
    cfg_data = yaml.safe_load(written.read_text())
    validate_strategy_config(cfg_data)


def test_import_unknown_template_is_404() -> None:
    resp = _client().post("/api/templates/no-such-template/import")
    assert resp.status_code == 404


def test_import_rejects_invalid_template(strategies_dir: Path) -> None:
    bad_template = _TEMPLATES_DIR / "broken_test_only.yaml"
    bad_template.write_text(yaml.safe_dump({"name": "Broken", "strategy_class": "antigravity.Foo"}))
    try:
        resp = _client().post("/api/templates/broken_test_only/import")
        assert resp.status_code == 422
        assert list(strategies_dir.glob("*.yaml")) == []
    finally:
        bad_template.unlink()
