"""Every Dockerfile referenced by the Makefile, compose files and CI publish job exists."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]
_COMPOSE_FILES = ["deploy/compose.yml", "docker-compose.ml.yml"]
_DOCKER_BUILD = re.compile(r"docker build\s+-f\s+(\S+)\s+-t\s+(\S+)")


def _makefile_builds() -> list[tuple[str, str]]:
    return _DOCKER_BUILD.findall((_ROOT / "Makefile").read_text())


def _makefile_dockerfiles() -> list[str]:
    return [dockerfile for dockerfile, _ in _makefile_builds()]


def _build_matrix() -> list[dict[str, Any]]:
    workflow = yaml.safe_load((_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    include: list[dict[str, Any]] = workflow["jobs"]["publish"]["strategy"]["matrix"]["include"]
    return include


def _compose_dockerfiles(compose_rel: str) -> list[tuple[str, Path]]:
    compose_path = _ROOT / compose_rel
    services = yaml.safe_load(compose_path.read_text())["services"]
    refs: list[tuple[str, Path]] = []
    for name, svc in services.items():
        build = svc.get("build")
        if build is None:
            continue
        if isinstance(build, str):
            build = {"context": build}
        context = compose_path.parent / build.get("context", ".")
        refs.append((name, (context / build.get("dockerfile", "Dockerfile")).resolve()))
    return refs


def test_makefile_dockerfiles_exist() -> None:
    paths = _makefile_dockerfiles()
    assert len(paths) == 3
    missing = [p for p in paths if not (_ROOT / p).is_file()]
    assert missing == []


@pytest.mark.parametrize("compose_rel", _COMPOSE_FILES)
def test_compose_dockerfiles_exist(compose_rel: str) -> None:
    refs = _compose_dockerfiles(compose_rel)
    assert refs, f"{compose_rel} has no build sections"
    missing = [(name, str(path)) for name, path in refs if not path.is_file()]
    assert missing == []
    assert all(path.is_relative_to(_ROOT / "docker") for _, path in refs)


def test_build_workflow_dockerfiles_exist() -> None:
    matrix = _build_matrix()
    assert {entry["image"] for entry in matrix} == {
        "hydra-engine",
        "hydra-dashboard",
        "hydra-backtest",
    }
    missing = [e["dockerfile"] for e in matrix if not (_ROOT / e["dockerfile"]).is_file()]
    assert missing == []


def test_makefile_images_match_ci_matrix() -> None:
    assert set(_makefile_builds()) == {(e["dockerfile"], e["image"]) for e in _build_matrix()}
