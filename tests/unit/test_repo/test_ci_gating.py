"""Images publish only after every CI check passes; infra-backed tiers cannot pass by skipping."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOWS = _ROOT / ".github" / "workflows"
_PUBLISH_IF = "github.event_name == 'push' && github.ref == 'refs/heads/main'"
_INFRA_JOBS = ["integration-tests", "e2e-tests"]


def _ci() -> dict[str, Any]:
    workflow: dict[str, Any] = yaml.safe_load((_WORKFLOWS / "ci.yml").read_text())
    return workflow


def _jobs() -> dict[str, dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = _ci()["jobs"]
    return jobs


def _runs(job: dict[str, Any]) -> list[str]:
    return [str(step["run"]) for step in job.get("steps", []) if "run" in step]


def _pytest_step(job: dict[str, Any]) -> dict[str, Any]:
    [step] = [s for s in job["steps"] if "pytest" in str(s.get("run", ""))]
    return step


def test_publish_needs_every_other_job_and_runs_only_on_main_pushes() -> None:
    jobs = _jobs()
    publish = jobs["publish"]
    assert set(publish["needs"]) == set(jobs) - {"publish"}
    assert publish["if"] == _PUBLISH_IF
    assert publish["permissions"] == {"contents": "read", "packages": "write"}
    assert _ci()["permissions"] == {}


def test_only_publish_pushes_images() -> None:
    pushing = [
        name
        for name, job in _jobs().items()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("docker/build-push-action@")
        and step["with"].get("push") is not False
    ]
    assert pushing == ["publish"]


def test_image_smoke_builds_every_published_image_on_prs_and_pushes() -> None:
    jobs = _jobs()
    smoke = jobs["image-smoke"]
    assert "if" not in smoke
    images = {e["image"] for e in smoke["strategy"]["matrix"]["include"]}
    assert images == {e["image"] for e in jobs["publish"]["strategy"]["matrix"]["include"]}
    [build] = [s for s in smoke["steps"] if str(s.get("uses", "")).startswith("docker/build-push")]
    assert build["with"]["load"] is True
    start = "\n".join(_runs(smoke))
    assert "DATABASE_URL=" in start and "REDIS_URL=" in start
    assert '.db == "connected"' in start


def test_triggers_and_concurrency_keep_main_runs() -> None:
    workflow = _ci()
    triggers = workflow[True]
    assert set(triggers) == {"push", "pull_request", "workflow_dispatch"}
    assert triggers["push"]["branches"] == ["main"]
    assert workflow["concurrency"]["cancel-in-progress"] == (
        "${{ github.event_name == 'pull_request' }}"
    )


def test_separate_build_and_rules_workflows_are_gone() -> None:
    assert not (_WORKFLOWS / "build.yml").exists()
    assert not (_WORKFLOWS / "rules.yml").exists()
    promtool = "\n".join(_runs(_jobs()["promtool"]))
    assert "promtool" in promtool
    assert "check rules /rules/prometheus-rules.yml" in promtool
    assert "test rules /rules/prometheus-rules.test.yml" in promtool


def test_no_exit_code_five_escape_hatch() -> None:
    for path in _WORKFLOWS.glob("*.yml"):
        assert "$? -eq 5" not in path.read_text(), path.name
    assert "$? -eq 5" not in (_ROOT / "Makefile").read_text()


@pytest.mark.parametrize("job_name", _INFRA_JOBS)
def test_infra_tiers_run_on_service_containers_with_skips_as_failures(job_name: str) -> None:
    job = _jobs()[job_name]
    assert set(job["services"]) == {"timescaledb", "redis"}
    assert "pg_isready" in job["services"]["timescaledb"]["options"]
    assert "redis-cli ping" in job["services"]["redis"]["options"]
    env = _pytest_step(job)["env"]
    assert env["HYDRA_REQUIRE_INFRA"] == "1"
    assert env["HYDRA_DB_PORT"] == "${{ job.services.timescaledb.ports['5432'] }}"
    assert env["HYDRA_REDIS_URL"] == "redis://localhost:${{ job.services.redis.ports['6379'] }}/0"
    assert not any("docker run" in run for run in _runs(job))


def test_import_contract_runs_in_ci_and_make_check() -> None:
    assert any("uv run lint-imports" in run for job in _jobs().values() for run in _runs(job))
    makefile = (_ROOT / "Makefile").read_text()
    [check] = re.findall(r"^check:(.*)$", makefile, re.M)
    assert "imports" in check.split()


# ---------------------------------------------------------------------------
# HYDRA_REQUIRE_INFRA: the conftest hook and the tests/infra.py fixtures
# ---------------------------------------------------------------------------

_SKIPPING_TESTS = """
import pytest

@pytest.fixture()
def needs_db():
    pytest.skip("no db here")

@pytest.mark.integration
def test_integration_body_skip():
    pytest.skip("no redis here")

@pytest.mark.integration
def test_integration_fixture_skip(needs_db):
    pass

@pytest.mark.e2e
def test_e2e_skip():
    pytest.skip("no stack here")

def test_unit_skip():
    pytest.skip("unit skips are allowed")
"""

_INFRA_FIXTURE_TESTS = """
import pytest

@pytest.mark.integration
def test_redis(redis_url):
    pass

@pytest.mark.integration
async def test_fresh_db(fresh_database):
    pass

@pytest.mark.integration
def test_module_db(database_dsn):
    pass
"""


def _run(pytester: pytest.Pytester, source: str) -> pytest.RunResult:
    pytester.makeini(
        "[pytest]\nasyncio_mode = auto\nasyncio_default_fixture_loop_scope = function\n"
    )
    pytester.makepyfile(source)
    return pytester.runpytest("-p", "tests.conftest", "-rA")


def _clear_infra_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("HYDRA_REQUIRE_INFRA", "HYDRA_DB_HOST", "HYDRA_DB_PORT", "HYDRA_REDIS_URL"):
        monkeypatch.delenv(var, raising=False)


def test_skips_stay_skips_without_require_infra(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_infra_env(monkeypatch)
    _run(pytester, _SKIPPING_TESTS).assert_outcomes(skipped=4)


def test_require_infra_turns_tier_skips_into_failures(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_infra_env(monkeypatch)
    monkeypatch.setenv("HYDRA_REQUIRE_INFRA", "1")
    result = _run(pytester, _SKIPPING_TESTS)
    result.assert_outcomes(failed=2, errors=1, skipped=1)
    out = result.stdout.str()
    for reason in ("no redis here", "no db here", "no stack here"):
        assert f"HYDRA_REQUIRE_INFRA=1 but the test was skipped: {reason}\n" in out
    result.stdout.fnmatch_lines(
        [
            "ERROR *::test_integration_fixture_skip*",
            "FAILED *::test_integration_body_skip*",
            "FAILED *::test_e2e_skip*",
        ]
    )


def test_unconfigured_infra_fixtures_fail_under_require_infra(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_infra_env(monkeypatch)
    _run(pytester, _INFRA_FIXTURE_TESTS).assert_outcomes(skipped=3)

    monkeypatch.setenv("HYDRA_REQUIRE_INFRA", "1")
    result = _run(pytester, _INFRA_FIXTURE_TESTS)
    result.assert_outcomes(errors=3)
    result.stdout.fnmatch_lines(["*HYDRA_REDIS_URL not set*", "*HYDRA_DB_HOST not set*"])


def test_configured_but_unreachable_infra_fails_even_without_require_infra(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_infra_env(monkeypatch)
    monkeypatch.setenv("HYDRA_REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("HYDRA_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("HYDRA_DB_PORT", "1")
    result = _run(pytester, _INFRA_FIXTURE_TESTS)
    result.assert_outcomes(errors=3)
    result.stdout.fnmatch_lines(
        [
            "*HYDRA_REDIS_URL is set but Redis is unreachable*",
            "*HYDRA_DB_* is set but the database server is unreachable*",
        ]
    )
