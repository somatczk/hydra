"""Tests that dashboard writes never touch the repo's config directory."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from hydra.dashboard.api import app
from hydra.dashboard.routes.models import _models_dir

REPO = Path(__file__).resolve().parents[3]
REPO_STRATEGIES = REPO / "config" / "strategies"
SRC = REPO / "src"

_RULES = {
    "entry_long": {
        "operator": "AND",
        "conditions": [{"indicator": "rsi", "comparator": "crosses_below", "value": 30}],
    },
    "exit_long": {
        "operator": "AND",
        "conditions": [{"indicator": "rsi", "comparator": "crosses_above", "value": 70}],
    },
}


def _snapshot(directory: Path) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.iterdir())
        if p.is_file()
    }


def test_strategy_writes_land_in_isolated_dir(strategies_dir: Path) -> None:
    before = _snapshot(REPO_STRATEGIES)
    client = TestClient(app)

    saved = client.post("/api/strategies/save", json={"name": "Iso Check", "rules": _RULES})
    assert saved.status_code == 201
    saved_id = saved.json()["id"]
    saved_path = strategies_dir / f"{saved_id}.yaml"
    assert Path(saved.json()["config_path"]).resolve() == saved_path.resolve()
    assert yaml.safe_load(saved_path.read_text())["name"] == "Iso Check"

    toggled = client.post(f"/api/strategies/{saved_id}/toggle")
    assert toggled.status_code == 200
    assert toggled.json() == {"id": saved_id, "enabled": True}
    assert yaml.safe_load(saved_path.read_text())["enabled"] is True

    upload = yaml.safe_dump({"name": "Uploaded Iso", "strategy_class": "x.Y"}).encode()
    imported = client.post(
        "/api/strategies/import",
        files={"file": ("uploaded.yaml", upload, "application/x-yaml")},
    )
    assert imported.status_code == 201
    imported_id = imported.json()["id"]
    assert (strategies_dir / f"{imported_id}.yaml").is_file()

    from_template = client.post("/api/templates/momentum_rsi/import")
    assert from_template.status_code == 201
    template_id = from_template.json()["id"]
    assert (strategies_dir / f"{template_id}.yaml").is_file()

    deleted = client.delete(f"/api/strategies/{saved_id}")
    assert deleted.status_code == 204
    assert not saved_path.exists()

    assert sorted(p.name for p in strategies_dir.iterdir()) == sorted(
        [f"{imported_id}.yaml", f"{template_id}.yaml"]
    )
    assert _snapshot(REPO_STRATEGIES) == before


def test_models_dir_isolated(tmp_path: Path) -> None:
    assert _models_dir() == tmp_path / "models"


def test_no_committed_test_strategy_yaml() -> None:
    assert sorted(REPO_STRATEGIES.glob("test_strategy_*.yaml")) == []


def _file_derived_constants() -> set[str]:
    found: set[str] = set()
    for path in sorted((SRC / "hydra").rglob("*.py")):
        module = ".".join(path.relative_to(SRC).with_suffix("").parts)
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            if any(isinstance(n, ast.Name) and n.id == "__file__" for n in ast.walk(value)):
                found.update(f"{module}.{t.id}" for t in targets if isinstance(t, ast.Name))
    return found


def test_repo_relative_dir_constants_are_known() -> None:
    assert _file_derived_constants() == {
        "hydra.core.config._DEFAULT_CONFIG_DIR",
        "hydra.dashboard.routes.strategies._CONFIG_DIR",
        "hydra.dashboard.routes.templates._TEMPLATES_DIR",
        "hydra.dashboard.routes.templates._STRATEGIES_DIR",
        "hydra.execution.session_manager._CONFIG_DIR",
    }
