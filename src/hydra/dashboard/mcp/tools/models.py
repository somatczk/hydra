"""Model registry tools: list, get, retrain (submit, status, cancel) and the gated stage moves.

Each tool is a thin call into :mod:`hydra.services.models`, the functions the REST
``/api/models`` routes call. ``model_retrain`` is ungated like ``backtest_run``: it
queues a bounded ``ml_train`` job whose version lands in Staging, which no session
consumes. ``model_promote`` and ``model_rollback`` are GATED: they swap the Production
version every ML consumer attaches when it starts, recovers or pins its overlay — paper
and live alike — so each creates an approval and answers ``approval_required`` with the
one-time code; only after a human types it on the Hydra UI Activity page does the
executor run, attributed to ``service:mcp``.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hydra.core.config import MlConfig
from hydra.core.types import Timeframe
from hydra.dashboard.mcp import server
from hydra.dashboard.mcp.tools import _deps
from hydra.dashboard.security import audit
from hydra.services import models as models_svc
from hydra.services.approvals import APPROVAL_ACTOR, ApprovalStore
from hydra.services.errors import ServiceError
from hydra.services.live_gate import approval_response, create_live_approval, live_approval_store


class _Reason(BaseModel):
    """A stage-move reason a human can read: the same bounds as the other gated tools."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)


def _models_dir() -> Path:
    """The engine's ``ml.models_dir``; the section's default while the state is unbound."""
    cfg = getattr(_deps.engine_state(), "config", None)
    return Path(cfg.ml.models_dir if cfg is not None else MlConfig().models_dir)


def _approval_store() -> ApprovalStore:
    """The engine's one approval store, with the two model executors registered (idempotent)."""
    store = live_approval_store(_deps.engine_state())
    store.register("promote_model", models_svc.execute_promote)
    store.register("rollback_model", models_svc.execute_rollback)
    return store


@server.tool(name="model_list", structured_output=False)
async def model_list() -> str:
    """List every registered model version (name, version, stage Staging|Production|
    Archived, metrics, feature spec, drift, training job), in name-then-version order."""
    try:
        return _deps.json_text(await models_svc.list_models(_deps.db_pool(), _models_dir()))
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc


@server.tool(name="model_get", structured_output=False)
async def model_get(model_id: str) -> str:
    """Read one model version by its registry id (a UUID from ``model_list``)."""
    try:
        return _deps.json_text(await models_svc.get_model(_deps.db_pool(), _models_dir(), model_id))
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc


@server.tool(name="model_retrain", structured_output=False)
async def model_retrain(
    name: str,
    strategy_id: str,
    symbol: str,
    timeframe: Timeframe | None = None,
    horizon: int | None = None,
    window_1h: int | None = None,
    window_4h: int | None = None,
    window_1d: int | None = None,
) -> str:
    """Queue an ml_train job: train a new version of model ``name`` on stored bars.

    ``name`` is a plain identifier; ``strategy_id`` must exist and ``symbol`` must be
    a member of the top-100 universe (``universe_list``). The job is bounded (the
    window/horizon caps, the fetch budget, a 6 h timeout) and registers the new
    version in Staging — it never changes Production. Returns ``{"task_id",
    "status": "queued"}``; poll ``model_retrain_status``.
    """
    try:
        body = models_svc.MlRetrainRequest(
            strategy_id=strategy_id,
            symbol=symbol,
            timeframe=timeframe,
            horizon=horizon,
            window_1h=window_1h,
            window_4h=window_4h,
            window_1d=window_1d,
        )
        result = await models_svc.submit_retrain(_deps.db_pool(), name, body)
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc
    except ValidationError as exc:
        raise _deps.invalid_arguments(exc) from exc
    audit(
        _deps.MCP_PRINCIPAL,
        "model_retrain_submitted",
        name=name,
        task_id=result["task_id"],
        symbol=symbol,
        strategy_id=strategy_id,
    )
    return _deps.json_text(result)


@server.tool(name="model_retrain_status", structured_output=False)
async def model_retrain_status(task_id: str) -> str:
    """Poll an ml_train job: status (queued|running|completed|failed|cancelled), progress,
    error, the fetch/provenance/metrics detail and, once completed, the registered version."""
    try:
        return _deps.json_text(await models_svc.retrain_status(_deps.db_pool(), task_id))
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc


@server.tool(name="model_retrain_cancel", structured_output=False)
async def model_retrain_cancel(task_id: str) -> str:
    """Cancel a queued or running ml_train job (404 for any other job kind)."""
    try:
        result = await models_svc.cancel_retrain(_deps.db_pool(), task_id)
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc
    audit(
        _deps.MCP_PRINCIPAL,
        "model_retrain_cancelled",
        task_id=task_id,
        status=result["status"],
    )
    return _deps.json_text(result)


@server.tool(name="model_promote", structured_output=False)
async def model_promote(model_id: str, reason: str) -> str:
    """Ask to promote a model version to Production. GATED: this promotes nothing — it
    creates an approval and answers ``approval_required`` with a one-time code.

    A human must type that code on the Hydra UI Activity page; only then is the version
    promoted (the previous Production is archived), attributed to ``service:mcp``. The
    code is single-use and expires in 10 minutes. Backtests of ML strategies serve the
    Production version, so a fresh Staging version is backtestable once promoted.
    """
    try:
        body = _Reason(reason=reason)
        params = await models_svc.promote_params(_deps.db_pool(), model_id, body.reason)
        approval = await create_live_approval(
            _approval_store(),
            action="promote_model",
            reason=body.reason,
            params=params,
            requested_by=APPROVAL_ACTOR,
        )
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc
    except ValidationError as exc:
        raise _deps.invalid_arguments(exc) from exc
    return _deps.json_text(approval_response(approval))


@server.tool(name="model_rollback", structured_output=False)
async def model_rollback(model_id: str, reason: str) -> str:
    """Ask to roll a model back to its most recently archived Production version (any
    version id of the model names it). GATED: this rolls back nothing — it creates an
    approval and answers ``approval_required`` with a one-time code.

    The approval pins the current Production version: if it changes before the human
    types the code, the rollback refuses 409 ``model_state_changed`` and nothing
    changes. 409 ``rollback_refused`` up front when there is no archived version.
    """
    try:
        body = _Reason(reason=reason)
        params = await models_svc.rollback_params(_deps.db_pool(), model_id, body.reason)
        approval = await create_live_approval(
            _approval_store(),
            action="rollback_model",
            reason=body.reason,
            params=params,
            requested_by=APPROVAL_ACTOR,
        )
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc
    except ValidationError as exc:
        raise _deps.invalid_arguments(exc) from exc
    return _deps.json_text(approval_response(approval))
