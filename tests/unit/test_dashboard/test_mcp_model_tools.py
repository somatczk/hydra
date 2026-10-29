"""The 7 model-registry MCP tools, called through the mounted transport.

Reads and the retrain job surface run ungated; ``model_promote`` and ``model_rollback``
only create an approval — the registry's stages change only when a human types the
one-time code on the Activity page (the real ``/api/activity`` approve route). The
pool answers the registry's own SQL from an in-memory ``ml_models`` table, so the
service, the executors and ``ModelRegistry`` all run for real; the registry's
semantics on TimescaleDB are pinned in ``tests/integration/test_models_routes_pg.py``.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from structlog.testing import capture_logs

from hydra.backtest import worker
from hydra.backtest.data import NoMarketDataError
from hydra.core.config import HydraConfig
from hydra.core.logging import get_logger
from hydra.dashboard import security
from hydra.dashboard.api import app as engine_app
from hydra.ml.consumers import LiveFeaturesPredictor, _reset_overlays
from hydra.ml.features import DEFAULT_FEATURE_SPEC
from hydra.ml.serving import _reset_attached
from hydra.services import models as models_svc
from hydra.strategy.builtin.ml_ensemble import DEFAULT_REQUIRED_HISTORY, STRATEGY_CLASS
from hydra.strategy.ml_access import _reset_providers, provider_for
from tests.fixtures.ml_onnx import confidence_onnx, frozen_spec_metadata

from .conftest import TEST_API_TOKEN
from .test_mcp_tools import MODEL_TOOLS, TEST_MCP_TOKEN, Bind, _async_mcp

_UNIVERSE = {
    "exchange_id": "binance",
    "quote_asset": "USDT",
    "source": "test",
    "ranked_at": "2026-10-28T00:00:00+00:00",
    "count": 1,
    "symbols": [{"rank": 1, "symbol": "BTCUSDT", "quote_volume": 1.0}],
}

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

# the frozen 1h/4h/1d grid both ML consumers read their features from
_ML_GRID = {"primary": "1h", "confirmation": "4h", "entry": "1d"}


# ---------------------------------------------------------------------------
# An in-memory ml_models table behind the registry's own SQL
# ---------------------------------------------------------------------------


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


class _RegistryConn:
    """``ml_models`` answered by the registry's SQL; jobs, the universe and event rows too."""

    def __init__(self) -> None:
        self.models: dict[uuid.UUID, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.events: list[tuple[Any, ...]] = []
        self._clock = itertools.count(1)

    def add(
        self, version: int, stage: str, *, name: str = "alpha", artifact: str = "a.onnx"
    ) -> str:
        model_id = uuid.uuid4()
        self.models[model_id] = {
            "id": model_id,
            "name": name,
            "version": version,
            "stage": stage,
            "model_type": "xgboost",
            "metrics": json.dumps({"oos_accuracy": 0.51}),
            "feature_spec": json.dumps({"spec_version": 3}),
            "drift": None,
            "artifact_file": artifact,
            "artifact_sha256": "0" * 64,
            "trained_at": datetime(2026, 10, 28, 12, tzinfo=UTC),
            "created_by": f"job:ml-{version:08d}",
            "training_job_id": f"ml-{version:08d}",
            "archived_at": self._now() if stage == "archived" else None,
        }
        return str(model_id)

    def stages(self, name: str = "alpha") -> dict[int, str]:
        return {r["version"]: r["stage"] for r in self._of(name)}

    def _now(self) -> datetime:
        return datetime(2026, 10, 29, tzinfo=UTC) + timedelta(seconds=next(self._clock))

    def _of(self, name: str) -> list[dict[str, Any]]:
        return sorted(
            (r for r in self.models.values() if r["name"] == name), key=lambda r: r["version"]
        )

    def _archive_production(self, name: str, keep: int) -> dict[str, Any] | None:
        for row in self._of(name):
            if row["stage"] == "production" and row["version"] != keep:
                row.update(stage="archived", archived_at=self._now())
                return {"version": row["version"]}
        return None

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        if "FROM system_config" in sql:
            return {"value": json.dumps(_UNIVERSE)}
        if "FROM ml_models WHERE id = $1" in sql:
            return self.models.get(args[0])
        if sql.lstrip().startswith("SELECT") and "stage = 'production'" in sql:
            return next((r for r in self._of(args[0]) if r["stage"] == "production"), None)
        if "SET stage = 'archived'" in sql:  # promote's demote of the other Production
            return self._archive_production(args[0], args[1])
        if "SET stage = 'production'" in sql and "ORDER BY archived_at" in sql:
            archived = [r for r in self._of(args[0]) if r["stage"] == "archived"]
            if not archived:
                return None
            row = max(archived, key=lambda r: (r["archived_at"], r["version"]))
            row.update(stage="production", archived_at=None)
            return row
        if "SET stage = 'production'" in sql:
            row = next((r for r in self._of(args[0]) if r["version"] == args[1]), None)
            if row is not None:
                row.update(stage="production", archived_at=None)
            return row
        if "UPDATE backtest_jobs" in sql:  # request_cancel
            job = self.jobs[args[0]]
            if job["status"] == "queued":
                job["status"] = "cancelled"
            job["cancel_requested"] = True
            return job
        if "FROM backtest_jobs" in sql:
            job = self.jobs.get(args[0])
            if job is None or ("kind = 'ml_train'" in sql and job["kind"] != "ml_train"):
                return None
            if "AND kind = $2" in sql and job["kind"] != args[1]:
                return None
            return job
        return None  # the preflight's stored-bar range: nothing stored (auto-fetch queues)

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        if "FROM ml_models" in sql:
            rows = sorted(self.models.values(), key=lambda r: (r["name"], r["version"]))
            return [r for r in rows if not args or r["name"] == args[0]]
        return []

    async def execute(self, sql: str, *args: Any) -> str:
        if "INSERT INTO backtest_jobs" in sql:
            self.jobs[args[0]] = {
                "id": args[0],
                "kind": args[1],
                "request": args[2],
                "strategy_config": args[3],
                "detail": args[4],
                "status": "queued",
                "progress": 0.0,
                "error": None,
                "result": None,
                "cancel_requested": False,
            }
        elif "SET stage = 'archived'" in sql:  # rollback's archive of the current Production
            self._archive_production(args[0], args[1])
        elif "INSERT INTO risk_events" in sql:
            self.events.append(args)
        return "OK"

    def transaction(self) -> _Transaction:
        return _Transaction()


class _RegistryPool:
    def __init__(self) -> None:
        self.conn = _RegistryConn()

    def acquire(self) -> _RegistryPool:
        return self

    async def __aenter__(self) -> _RegistryConn:
        return self.conn

    async def __aexit__(self, *exc: object) -> None:
        return None


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A fresh approval store and ML attach state; a fresh audit logger to capture."""
    monkeypatch.setattr(engine_app.state, "approval_store", None, raising=False)
    monkeypatch.setattr(models_svc, "audit_logger", get_logger("hydra.audit"))
    monkeypatch.setattr(security, "audit_logger", get_logger("hydra.audit"))
    _reset_attached()
    _reset_providers()
    _reset_overlays()
    yield
    _reset_attached()
    _reset_providers()
    _reset_overlays()


@pytest.fixture()
def mcp_cfg(bind_api_config: Bind) -> HydraConfig:
    """Dev auth on the engine app (the activity approve route needs a human principal)."""
    return bind_api_config(
        HYDRA_API__AUTH_MODE="dev",
        HYDRA_API_TOKEN=TEST_API_TOKEN,
        HYDRA_MCP_TOKEN=TEST_MCP_TOKEN,
        HYDRA_MCP__ALLOWED_HOSTS="testserver",
    )


@pytest.fixture()
def pool(monkeypatch: pytest.MonkeyPatch) -> _RegistryPool:
    registry = _RegistryPool()
    monkeypatch.setattr(engine_app.state, "db_pool", registry)
    return registry


def _envelope(tool: str, text: str) -> tuple[int, str]:
    envelope = json.loads(text.removeprefix(f"Error executing tool {tool}: "))
    return envelope["status"], envelope["detail"]["code"]


async def _ui(method: str, path: str, **kwargs: Any) -> httpx.Response:
    """The human on the dashboard: the engine app under dev auth."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=engine_app),
        base_url="http://engine",
        headers={"X-Hydra-Request": "1"},
    ) as ui:
        return await ui.request(method, path, **kwargs)


async def _approve(approval: dict[str, Any]) -> httpx.Response:
    return await _ui(
        "POST",
        f"/api/activity/approvals/{approval['approval_id']}/approve",
        json={"code": approval["code"]},
    )


def _audits(logs: list[dict[str, Any]], action: str) -> list[dict[str, Any]]:
    return [e for e in logs if e.get("event") == "audit" and e.get("action") == action]


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------


async def test_the_seven_model_tools_are_advertised(mcp_cfg: HydraConfig) -> None:
    async with _async_mcp(mcp_cfg) as mcp:
        tools = (await mcp.request("tools/list"))["tools"]
    names = {tool["name"] for tool in tools}
    assert {name for name in names if name.startswith("model_")} == MODEL_TOOLS
    gated = {t["name"] for t in tools if "GATED" in (t.get("description") or "")}
    assert MODEL_TOOLS & gated == {"model_promote", "model_rollback"}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_list_and_get_are_the_rest_shapes(mcp_cfg: HydraConfig, pool: _RegistryPool) -> None:
    v1 = pool.conn.add(1, "archived")
    v2 = pool.conn.add(2, "production")
    async with _async_mcp(mcp_cfg) as mcp:
        is_error, text = await mcp.call("model_list")
        assert not is_error, text
        listed = json.loads(text)
        is_error, text = await mcp.call("model_get", {"model_id": v2})
        assert not is_error, text
        got = json.loads(text)
        bad = await mcp.call("model_get", {"model_id": "../alpha.onnx"})
        unknown = await mcp.call("model_get", {"model_id": str(uuid.uuid4())})
    assert [(m["id"], m["version"], m["stage"]) for m in listed] == [
        (v1, 1, "Archived"),
        (v2, 2, "Production"),
    ]
    assert listed == (await _ui("GET", "/api/models")).json()
    assert got == (await _ui("GET", f"/api/models/{v2}")).json() == listed[1]
    assert bad[0] and _envelope("model_get", bad[1]) == (400, "invalid_model_id")
    assert unknown[0] and _envelope("model_get", unknown[1]) == (404, "model_not_found")


async def test_reads_without_a_pool_are_the_503_envelope(
    mcp_cfg: HydraConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_app.state, "db_pool", None, raising=False)
    async with _async_mcp(mcp_cfg) as mcp:
        is_error, text = await mcp.call("model_list")
    assert is_error and _envelope("model_list", text) == (503, "database_unavailable")


# ---------------------------------------------------------------------------
# Retrain: ungated, the version lands in Staging
# ---------------------------------------------------------------------------


def _write_rule_strategy(directory: Path, strategy_id: str) -> None:
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


async def test_retrain_queues_polls_and_cancels_the_ml_train_job(
    mcp_cfg: HydraConfig, pool: _RegistryPool, strategies_dir: Path
) -> None:
    _write_rule_strategy(strategies_dir, "saw")
    async with _async_mcp(mcp_cfg) as mcp:
        with capture_logs() as logs:
            is_error, text = await mcp.call(
                "model_retrain",
                {
                    "name": "alpha",
                    "strategy_id": "saw",
                    "symbol": "BTCUSDT",
                    "horizon": 2,
                    "window_1h": 500,
                },
            )
        assert not is_error, text
        queued = json.loads(text)
        is_error, text = await mcp.call("model_retrain_status", {"task_id": queued["task_id"]})
        assert not is_error, text
        status = json.loads(text)
        with capture_logs() as cancel_logs:
            is_error, text = await mcp.call("model_retrain_cancel", {"task_id": queued["task_id"]})
        assert not is_error, text
        cancelled = json.loads(text)

        too_wide = await mcp.call(
            "model_retrain",
            {"name": "alpha", "strategy_id": "saw", "symbol": "BTCUSDT", "window_1h": 0},
        )
        bad_name = await mcp.call(
            "model_retrain", {"name": ".hidden", "strategy_id": "saw", "symbol": "BTCUSDT"}
        )
        outside = await mcp.call(
            "model_retrain", {"name": "alpha", "strategy_id": "saw", "symbol": "NOPEUSDT"}
        )
        not_a_retrain = await mcp.call("model_retrain_cancel", {"task_id": "task-bt000001"})

    assert queued["status"] == "queued" and queued["task_id"].startswith("ml-")
    job = pool.conn.jobs[queued["task_id"]]
    assert job["kind"] == "ml_train"
    request = json.loads(job["request"])
    assert (request["name"], request["symbol"], request["horizon"], request["window_1h"]) == (
        "alpha",
        "BTCUSDT",
        2,
        500,
    )
    assert json.loads(job["strategy_config"])["id"] == "saw"
    [submitted] = _audits(logs, "model_retrain_submitted")
    assert (submitted["principal"], submitted["principal_kind"], submitted["task_id"]) == (
        "mcp",
        "service",
        queued["task_id"],
    )
    assert (status["task_id"], status["status"], status["progress"]) == (
        queued["task_id"],
        "queued",
        0.0,
    )
    assert cancelled == {"task_id": queued["task_id"], "status": "cancelled"}
    assert _audits(cancel_logs, "model_retrain_cancelled")[0]["principal"] == "mcp"
    assert too_wide[0] and _envelope("model_retrain", too_wide[1]) == (422, "validation_error")
    assert "window_1h" in json.loads(too_wide[1].split(": ", 1)[1])["detail"]["message"]
    assert bad_name[0] and _envelope("model_retrain", bad_name[1]) == (400, "invalid_model_name")
    assert outside[0] and _envelope("model_retrain", outside[1]) == (
        422,
        "symbol_not_in_universe",
    )
    assert not_a_retrain[0] and _envelope("model_retrain_cancel", not_a_retrain[1]) == (
        404,
        "task_not_found",
    )
    assert len(pool.conn.jobs) == 1  # the refused submits queued nothing


# ---------------------------------------------------------------------------
# Promote / rollback: gated through the approval store
# ---------------------------------------------------------------------------


async def test_promote_is_gated_then_promotes_on_the_humans_code(
    mcp_cfg: HydraConfig, pool: _RegistryPool
) -> None:
    pool.conn.add(1, "production")
    v2 = pool.conn.add(2, "staging")
    v3 = pool.conn.add(3, "staging")
    async with _async_mcp(mcp_cfg) as mcp:
        is_error, text = await mcp.call("model_promote", {"model_id": v2, "reason": "beats v1"})
        assert not is_error, text
        approval = json.loads(text)
        again = json.loads(
            (await mcp.call("model_promote", {"model_id": v2, "reason": "beats v1"}))[1]
        )
        other = await mcp.call("model_promote", {"model_id": v3, "reason": "or v3"})
        short = await mcp.call("model_promote", {"model_id": v2, "reason": "no"})

    assert approval["status"] == "approval_required"
    assert re.fullmatch(r"[0-9]{6}", approval["code"])
    assert "Hydra UI Activity page" in approval["note"]
    assert approval["params"] == {
        "model_id": v2,
        "name": "alpha",
        "version": 2,
        "reason": "beats v1",
    }
    assert pool.conn.stages() == {1: "production", 2: "staging", 3: "staging"}  # nothing yet
    assert again["approval_id"] == approval["approval_id"]  # an identical retry dedupes
    assert other[0] and _envelope("model_promote", other[1]) == (409, "approval_conflict")
    assert short[0] and _envelope("model_promote", short[1]) == (422, "validation_error")

    with capture_logs() as logs:
        resp = await _approve(approval)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "executed": True,
        "result": {
            "id": v2,
            "name": "alpha",
            "version": 2,
            "stage": "Production",
            "message": "Model 'alpha' v2 promoted to Production",
        },
        "actor": "service:mcp",
        "approved_by": "dev:dev",
    }
    assert pool.conn.stages() == {1: "archived", 2: "production", 3: "staging"}
    [promoted] = _audits(logs, "model_promoted")
    assert (promoted["actor"], promoted["model"], promoted["reason"]) == (
        "service:mcp",
        v2,
        "beats v1",
    )
    kinds = [args[1] for args in pool.conn.events]
    assert kinds == ["approval_requested", "approval_approved"]


async def test_promote_of_an_unknown_model_issues_no_code(
    mcp_cfg: HydraConfig, pool: _RegistryPool
) -> None:
    async with _async_mcp(mcp_cfg) as mcp:
        unknown = await mcp.call(
            "model_promote", {"model_id": str(uuid.uuid4()), "reason": "go live"}
        )
        bad = await mcp.call("model_promote", {"model_id": "alpha", "reason": "go live"})
    assert unknown[0] and _envelope("model_promote", unknown[1]) == (404, "model_not_found")
    assert bad[0] and _envelope("model_promote", bad[1]) == (400, "invalid_model_id")
    assert pool.conn.events == []  # no approval was ever requested


async def test_rollback_is_gated_and_refuses_when_production_moved_under_it(
    mcp_cfg: HydraConfig, pool: _RegistryPool
) -> None:
    pool.conn.add(1, "archived")
    v2 = pool.conn.add(2, "production")
    v3 = pool.conn.add(3, "staging")
    async with _async_mcp(mcp_cfg) as mcp:
        is_error, text = await mcp.call("model_rollback", {"model_id": v2, "reason": "v2 drifts"})
        assert not is_error, text
        stale = json.loads(text)
        assert stale["params"]["production_version"] == 2
        assert pool.conn.stages() == {1: "archived", 2: "production", 3: "staging"}

        # meanwhile the operator promotes v3 in the UI: "the latest archived" is now v2
        assert (await _ui("POST", f"/api/models/{v3}/promote")).status_code == 200
        refused = await _approve(stale)
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["code"] == "model_state_changed"
        assert pool.conn.stages() == {1: "archived", 2: "archived", 3: "production"}

        # a fresh request pins v3 and restores the most recently archived (v2)
        fresh = json.loads(
            (await mcp.call("model_rollback", {"model_id": v3, "reason": "v3 is worse"}))[1]
        )
        assert fresh["params"]["production_version"] == 3
        with capture_logs() as logs:
            resp = await _approve(fresh)
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == {
        "id": v2,
        "name": "alpha",
        "restored_version": 2,
        "stage": "Production",
        "message": "Model 'alpha' rolled back to v2",
    }
    assert pool.conn.stages() == {1: "archived", 2: "production", 3: "archived"}
    [rolled] = _audits(logs, "model_rolled_back")
    assert (rolled["actor"], rolled["restored_version"]) == ("service:mcp", 2)


async def test_rollback_with_nothing_archived_is_refused_before_a_code(
    mcp_cfg: HydraConfig, pool: _RegistryPool
) -> None:
    v1 = pool.conn.add(1, "production")
    async with _async_mcp(mcp_cfg) as mcp:
        is_error, text = await mcp.call("model_rollback", {"model_id": v1, "reason": "undo it"})
    assert is_error and _envelope("model_rollback", text) == (409, "rollback_refused")
    assert pool.conn.events == []
    assert pool.conn.stages() == {1: "production"}


# ---------------------------------------------------------------------------
# The research loop: import ML strategies, backtest them once a model is promoted
# ---------------------------------------------------------------------------


def _native_yaml() -> str:
    return yaml.safe_dump(
        {
            "name": "ML native",
            "strategy_class": STRATEGY_CLASS,
            "symbols": ["BTCUSDT"],
            "exchange": {"exchange_id": "binance", "market_type": "SPOT"},
            "timeframes": _ML_GRID,
            "parameters": {
                "model_name": "alpha",
                "entry_threshold": 0.6,
                "required_history": DEFAULT_REQUIRED_HISTORY,
            },
        }
    )


def _overlay_yaml() -> str:
    return yaml.safe_dump(
        {
            "name": "Gated saw",
            "strategy_class": "hydra.strategy.builtin.rule_based.RuleBasedStrategy",
            "symbols": ["BTCUSDT"],
            "exchange": {"exchange_id": "binance", "market_type": "SPOT"},
            "timeframes": _ML_GRID,
            "parameters": {"rules": _RULES, "required_history": DEFAULT_REQUIRED_HISTORY},
            "ml_overlay": {"enabled": True, "model_name": "alpha", "confidence_threshold": 0.6},
        }
    )


async def test_imported_ml_strategies_backtest_once_the_model_is_promoted(
    mcp_cfg: HydraConfig, pool: _RegistryPool, strategies_dir: Path
) -> None:
    """strategy_import → backtest_run → the worker's model gate, before and after a promote.

    The worker refuses a job whose model has no Production version; after the gated
    promote it attaches the model and goes on to the bars (none are stored here — the
    full bar replay on TimescaleDB is tests/integration/test_ml_backtest_job_pg.py).
    """
    models_dir = Path(mcp_cfg.ml.models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)
    meta = frozen_spec_metadata(list(DEFAULT_FEATURE_SPEC.feature_names), 3, 4)
    blob = confidence_onnx(meta, len(DEFAULT_FEATURE_SPEC.feature_names))
    (models_dir / "alpha-v1.onnx").write_bytes(blob)
    staged = pool.conn.add(1, "staging", artifact="alpha-v1.onnx")
    pool.conn.models[uuid.UUID(staged)]["artifact_sha256"] = hashlib.sha256(blob).hexdigest()

    task_ids: list[str] = []
    async with _async_mcp(mcp_cfg) as mcp:
        for filename, content in (("native.yaml", _native_yaml()), ("gated.yaml", _overlay_yaml())):
            is_error, text = await mcp.call(
                "strategy_import", {"filename": filename, "yaml_content": content}
            )
            assert not is_error, text
            strategy_id = json.loads(text)["id"]
            is_error, text = await mcp.call(
                "backtest_run",
                {"strategy_id": strategy_id, "start_date": "2026-01-01", "end_date": "2026-01-03"},
            )
            assert not is_error, text
            task_ids.append(json.loads(text)["task_id"])

        jobs = [pool.conn.jobs[task_id] for task_id in task_ids]
        for job in jobs:
            with pytest.raises(worker.JobError, match="has no Production version"):
                await worker.run_backtest_job(pool, None, job, worker.JobRun())

        is_error, text = await mcp.call(
            "model_promote", {"model_id": staged, "reason": "backtest the ML strategies"}
        )
        assert not is_error, text
        assert (await _approve(json.loads(text))).status_code == 200

    for job in jobs:
        with pytest.raises(NoMarketDataError):
            await worker.run_backtest_job(pool, None, job, worker.JobRun())
    predictor = provider_for("alpha")  # the native strategy's predictor, attached by the gate
    assert isinstance(predictor, LiveFeaturesPredictor)
    assert (predictor.model.name, predictor.model.version) == ("alpha", 1)
