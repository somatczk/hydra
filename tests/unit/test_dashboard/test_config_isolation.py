"""Tests that dashboard writes never touch the repo's config directory."""

from __future__ import annotations

import hashlib
import importlib
import os
import pkgutil
import sys
import types
from pathlib import Path, PurePath

import pytest
import yaml
from fastapi.testclient import TestClient

import hydra
import hydra.core.config as config_module
from hydra.dashboard.api import app
from hydra.dashboard.routes.models import _models_dir
from tests.fixtures.universe import seeded_universe_pool

REPO = Path(__file__).resolve().parents[3]
REPO_STRATEGIES = REPO / "config" / "strategies"

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


def test_strategy_writes_land_in_isolated_dir(
    strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _snapshot(REPO_STRATEGIES)
    monkeypatch.setattr(app.state, "db_pool", seeded_universe_pool())
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

    upload = yaml.safe_dump(
        {
            "name": "Uploaded Iso",
            "strategy_class": "hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        }
    ).encode()
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


_READ_ONLY_REPO_PATHS = {
    ("hydra.core.config", "_DEFAULT_CONFIG_DIR"),
    ("hydra.dashboard.routes.templates", "_TEMPLATES_DIR"),
    ("hydra.execution.session_manager", "_CONFIG_DIR"),
    ("hydra.data.migrations.runner", "_SCRIPT_LOCATION"),
}


def _import_all_hydra_modules() -> None:
    for info in pkgutil.walk_packages(hydra.__path__, "hydra."):
        if not info.name.startswith("hydra.data.migrations"):
            importlib.import_module(info.name)


# At collection, before the autouse fixture, so by-value aliases capture the real paths.
_import_all_hydra_modules()


def _repo_paths_in_hydra_modules() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for name, module in list(sys.modules.items()):
        if not name.startswith("hydra."):
            continue
        for attr, value in vars(module).items():
            if attr.startswith("__"):
                continue
            if isinstance(value, str) and not os.path.isabs(value):
                continue
            if isinstance(value, (str, PurePath)) and Path(value).resolve().is_relative_to(REPO):
                found.add((name, attr))
    return found


def test_only_read_only_repo_paths_survive_fixture(strategies_dir: Path) -> None:
    assert _repo_paths_in_hydra_modules() == _READ_ONLY_REPO_PATHS


def test_repo_path_scan_sees_aliases_and_derived_constants(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = types.ModuleType("hydra._isolation_probe")
    probe.__dict__["_ALIAS"] = REPO_STRATEGIES
    probe.__dict__["_DERIVED"] = config_module._DEFAULT_CONFIG_DIR / "strategies"
    probe.__dict__["_AS_STR"] = str(REPO / "models")
    monkeypatch.setitem(sys.modules, probe.__name__, probe)
    assert _repo_paths_in_hydra_modules() - _READ_ONLY_REPO_PATHS == {
        (probe.__name__, "_ALIAS"),
        (probe.__name__, "_DERIVED"),
        (probe.__name__, "_AS_STR"),
    }
