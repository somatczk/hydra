"""Every surface that reports a version reports pyproject's [project].version."""

from __future__ import annotations

import textwrap
import tomllib
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

import hydra
from hydra.core.config import PlatformConfig, load_config
from hydra.dashboard.api import app

_ROOT = Path(__file__).resolve().parents[3]


def _pyproject_version() -> str:
    with (_ROOT / "pyproject.toml").open("rb") as f:
        return str(tomllib.load(f)["project"]["version"])


def test_package_version_matches_pyproject() -> None:
    assert hydra.__version__ == _pyproject_version()


def test_api_version_matches_pyproject() -> None:
    expected = _pyproject_version()
    assert app.version == expected
    resp = TestClient(app).get("/openapi.json")
    assert resp.status_code == 200
    assert resp.json()["info"]["version"] == expected


def test_platform_config_version_matches_pyproject() -> None:
    assert PlatformConfig().version == _pyproject_version()


def test_stale_yaml_version_cannot_override(tmp_path: Path) -> None:
    (tmp_path / "base.yaml").write_text(
        textwrap.dedent("""\
        platform:
          name: hydra
          version: "0.1.0"
        """)
    )
    with pytest.raises(ValidationError) as exc_info:
        load_config(env="base", config_dir=tmp_path)
    assert ("platform", "version") in {tuple(e["loc"]) for e in exc_info.value.errors()}
    assert PlatformConfig().version == _pyproject_version()


def test_repo_base_yaml_carries_no_version() -> None:
    data = yaml.safe_load((_ROOT / "config" / "base.yaml").read_text())
    assert "version" not in data["platform"]
