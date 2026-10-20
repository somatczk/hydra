"""Python, uv and service-image pins agree across .python-version, pyproject, CI and Dockerfiles."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import yaml
from packaging.specifiers import SpecifierSet

_ROOT = Path(__file__).resolve().parents[3]
_CI = _ROOT / ".github" / "workflows" / "ci.yml"
_PYTHON_DOCKERFILES = ["docker/engine/Dockerfile", "docker/backtest/Dockerfile"]
_DASHBOARD_DOCKERFILE = "docker/dashboard/Dockerfile"
_UV_DOCKERFILES = [*_PYTHON_DOCKERFILES, "docker/ml/Dockerfile"]
_DIGEST = re.compile(r"[a-z0-9./-]+:[\w.-]+@sha256:[0-9a-f]{64}")
_UV_IMAGE = re.compile(r"^COPY --from=ghcr\.io/astral-sh/uv:(\S+) /uv /uvx /bin/$", re.M)
_UV_SYNC = re.compile(r"\buv sync\b[^\n]*")


def _pyproject() -> dict[str, Any]:
    with (_ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)


def _ci() -> dict[str, Any]:
    workflow: dict[str, Any] = yaml.safe_load(_CI.read_text())
    return workflow


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = job.get("steps", [])
    return steps


def _setup_uv_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name, job in _ci()["jobs"].items():
        for step in _steps(job):
            if str(step.get("uses", "")).startswith("astral-sh/setup-uv@"):
                versions[name] = step.get("with", {}).get("version")
    return versions


def _service_images() -> set[str]:
    images: set[str] = set()
    for job in _ci()["jobs"].values():
        images |= {svc["image"] for svc in job.get("services", {}).values()}
    return images


def test_python_version_file_matches_the_runtime_images() -> None:
    pinned = (_ROOT / ".python-version").read_text().strip()
    assert pinned == "3.14"
    for rel in _PYTHON_DOCKERFILES:
        bases = re.findall(r"^FROM python:(\d+\.\d+)-slim@", (_ROOT / rel).read_text(), re.M)
        assert bases == [pinned, pinned], rel


def test_published_images_pin_one_base_digest_per_image() -> None:
    bases: dict[str, set[str]] = {}
    for rel in [*_PYTHON_DOCKERFILES, _DASHBOARD_DOCKERFILE]:
        froms = re.findall(r"^FROM (\S+)", (_ROOT / rel).read_text(), re.M)
        assert len(froms) == 2, rel
        assert all(_DIGEST.fullmatch(ref) for ref in froms), froms
        bases[rel] = set(froms)
    assert all(len(refs) == 1 for refs in bases.values()), bases


def test_python_floor_and_lint_targets_stay_3_12() -> None:
    data = _pyproject()
    assert data["project"]["requires-python"] == ">=3.12"
    assert data["tool"]["ruff"]["target-version"] == "py312"
    assert data["tool"]["mypy"]["python_version"] == "3.12"
    pinned = (_ROOT / ".python-version").read_text().strip()
    assert SpecifierSet(data["project"]["requires-python"]).contains(f"{pinned}.0")


def test_every_uv_job_pins_the_same_uv_as_the_images() -> None:
    required = SpecifierSet(_pyproject()["tool"]["uv"]["required-version"])
    ci_versions = _setup_uv_versions()
    image_versions = {rel: _UV_IMAGE.findall((_ROOT / rel).read_text()) for rel in _UV_DOCKERFILES}

    assert ci_versions, "no setup-uv step in ci.yml"
    assert all(len(v) == 1 for v in image_versions.values()), image_versions
    pinned = set(ci_versions.values()) | {v[0] for v in image_versions.values()}
    assert pinned == {"0.13.3"}
    assert required.contains("0.13.3")
    assert required.contains("0.12.18"), "Dependabot's bundled uv must satisfy required-version"
    assert not required.contains("0.6.10")


def test_every_job_running_uv_installs_it_with_setup_uv() -> None:
    with_setup = set(_setup_uv_versions())
    for name, job in _ci()["jobs"].items():
        runs_uv = any(re.search(r"\buv (sync|run)\b", str(s.get("run", ""))) for s in _steps(job))
        if runs_uv:
            assert name in with_setup, name


def test_uv_is_never_installed_with_pip() -> None:
    files = [*_UV_DOCKERFILES, _DASHBOARD_DOCKERFILE, "Makefile"]
    files += [str(p.relative_to(_ROOT)) for p in (_ROOT / ".github" / "workflows").glob("*.yml")]
    offenders = [f for f in files if re.search(r"pip install[^\n]*\buv\b", (_ROOT / f).read_text())]
    assert offenders == []


def test_every_uv_sync_is_locked() -> None:
    files = [*_UV_DOCKERFILES, "Makefile", ".github/workflows/ci.yml"]
    syncs = {f: _UV_SYNC.findall((_ROOT / f).read_text()) for f in files}
    assert all(syncs.values()), syncs
    unlocked = [(f, cmd) for f, cmds in syncs.items() for cmd in cmds if "--locked" not in cmd]
    assert unlocked == []


def test_ci_services_use_the_dev_compose_images() -> None:
    compose = yaml.safe_load((_ROOT / "docker-compose.dev.yml").read_text())
    dev_images = {svc["image"] for svc in compose["services"].values()}
    assert dev_images == {"timescale/timescaledb:2.30.2-pg16", "redis:8-alpine"}
    assert _service_images() == dev_images


def test_ci_node_matches_the_dashboard_image() -> None:
    dockerfile = (_ROOT / _DASHBOARD_DOCKERFILE).read_text()
    [base] = re.findall(r"^FROM node:(\d+)-alpine@\S+ AS builder$", dockerfile, re.M)
    node_versions = {
        str(step["with"]["node-version"])
        for job in _ci()["jobs"].values()
        for step in _steps(job)
        if str(step.get("uses", "")).startswith("actions/setup-node@")
    }
    assert node_versions == {base}
