"""The session-start ML gate through the service seam (P5-C).

``sessions_svc.start_session`` is the one path every transport (REST, MCP,
the trader backend) starts a session through, so the fail-closed gate is
asserted here on the shared envelope: no Production model, no artifact on the
mount and a strategy that cannot carry the frozen grid each refuse with their
own code, and a strategy that consumes no model keeps its exact old behaviour
(the gate resolves nothing and the manager is called unchanged). The registry
rows are stubs; the registry's real semantics and the full attach pipeline
run on real Postgres in ``tests/integration/test_ml_session_start_pg.py``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import yaml

from hydra.ml.features import DEFAULT_FEATURE_SPEC
from hydra.services import sessions as sessions_svc
from hydra.services import strategies as strategies_svc
from hydra.services.errors import ServiceError
from hydra.services.sessions import StartSessionRequest
from tests.fixtures.ml_onnx import confidence_onnx, frozen_spec_metadata

A_SHIPPED_STRATEGY = "momentum_rsi_macd"


class _StubManager:
    """The seam's manager stand-in: records the start, refuses nothing."""

    def __init__(self, *, config: Any = None, pool: Any = None) -> None:
        self._config = config
        self._db_pool = pool
        self.started: list[str] = []

    async def start_session(
        self,
        strategy_id: str,
        trading_mode: str,
        paper_capital: Any = None,
        actor: str | None = None,
    ) -> str:
        self.started.append(strategy_id)
        return "s1"

    def get_session(self, session_id: str) -> Any:
        return SimpleNamespace(
            session_id=session_id,
            strategy_id="x",
            trading_mode="paper",
            status="running",
            exchange_id="binance",
            symbols=["BTCUSDT"],
            timeframe="1h",
            paper_capital=None,
            started_at=None,
            stopped_at=None,
            error_message=None,
            positions_at_stop=0,
            flatten_incomplete=False,
            persist_error=None,
            created_by=None,
        )


def _overlay_strategy_yaml(tmp_path: Path, *, primary: str = "1h", name: str = "gated") -> str:
    cfg: dict[str, Any] = {
        "id": name,
        "name": name,
        "strategy_class": "hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        "symbols": ["BTCUSDT"],
        "timeframes": {"primary": primary, "confirmation": "4h", "entry": "1d"},
        "parameters": {"required_history": 2000},
        "ml_overlay": {
            "enabled": True,
            "model_name": "alpha",
            "confidence_threshold": 0.6,
        },
    }
    (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump(cfg))
    return name


@pytest.fixture()
def strategy_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(strategies_svc, "_CONFIG_DIR", tmp_path)
    return tmp_path


async def _start(mgr: Any, strategy_id: str) -> Any:
    return await sessions_svc.start_session(
        mgr, "user:test", StartSessionRequest(strategy_id=strategy_id)
    )


def _config_with(models_dir: Path) -> Any:
    return SimpleNamespace(ml=SimpleNamespace(models_dir=str(models_dir)))


# ---------------------------------------------------------------------------
# Registry-row stubs: the pool the registry's SQL runs against
# ---------------------------------------------------------------------------


class _Acquire:
    def __init__(self, row: Any) -> None:
        self._row = row

    async def __aenter__(self) -> _Acquire:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return self._row


class _Pool:
    def __init__(self, row: Any) -> None:
        self._row = row

    def acquire(self) -> _Acquire:
        return _Acquire(self._row)


def _production_row(artifact_file: str) -> dict[str, Any]:
    return {
        "id": "irrelevant",
        "name": "alpha",
        "version": 1,
        "stage": "production",
        "model_type": "onnx",
        "metrics": "{}",
        "feature_spec": "{}",
        "drift": None,
        "artifact_file": artifact_file,
        "artifact_sha256": "0" * 64,
        "trained_at": "irrelevant",
        "created_by": "test",
        "training_job_id": None,
        "archived_at": None,
    }


class TestSessionStartMlGate:
    async def test_a_strategy_without_ml_starts_exactly_as_before(self, strategy_dir: Path) -> None:
        mgr = _StubManager()
        result = await _start(mgr, A_SHIPPED_STRATEGY)
        assert result["session_id"] == "s1"
        assert mgr.started == [A_SHIPPED_STRATEGY]

    async def test_a_strategy_file_that_cannot_be_resolved_defers_to_the_manager(
        self, strategy_dir: Path
    ) -> None:
        # no such file: the manager's own start refusal answers (unchanged)
        mgr = _StubManager()
        result = await _start(mgr, "no-such-strategy")
        assert mgr.started == ["no-such-strategy"]
        assert result["session_id"] == "s1"

    async def test_an_overlay_strategy_without_a_registry_refuses(self, strategy_dir: Path) -> None:
        name = _overlay_strategy_yaml(strategy_dir)
        mgr = _StubManager()  # no config/pool: the registry is unreachable
        with pytest.raises(ServiceError) as raised:
            await _start(mgr, name)
        assert raised.value.status_code == 503
        detail = raised.value.detail
        assert detail["code"] == "ml_model_unavailable"
        assert detail["state"] == "unavailable"
        assert "refuses to start without the model" in detail["message"]
        assert mgr.started == []

    async def test_an_overlay_strategy_off_the_frozen_grid_refuses(
        self, strategy_dir: Path, tmp_path: Path
    ) -> None:
        name = _overlay_strategy_yaml(strategy_dir, primary="15m")
        mgr = _StubManager(config=_config_with(tmp_path), pool=object())
        with pytest.raises(ServiceError) as raised:
            await _start(mgr, name)
        assert raised.value.status_code == 400
        assert raised.value.detail["code"] == "invalid_session"
        assert "frozen ML feature grid" in raised.value.detail["message"]
        assert mgr.started == []

    async def test_a_not_promoted_model_refuses_as_invalid(
        self, strategy_dir: Path, tmp_path: Path
    ) -> None:
        name = _overlay_strategy_yaml(strategy_dir)
        mgr = _StubManager(
            config=_config_with(tmp_path),
            pool=_Pool(None),  # no Production row
        )
        with pytest.raises(ServiceError) as raised:
            await _start(mgr, name)
        assert raised.value.status_code == 422
        assert raised.value.detail["code"] == "ml_model_not_promoted"
        assert "no Production version" in raised.value.detail["message"]
        assert mgr.started == []

    async def test_a_missing_artifact_refuses_as_unavailable(
        self, strategy_dir: Path, tmp_path: Path
    ) -> None:
        name = _overlay_strategy_yaml(strategy_dir)
        models_dir = tmp_path / "models"  # exists but the artifact does not
        models_dir.mkdir()
        mgr = _StubManager(
            config=_config_with(models_dir),
            pool=_Pool(_production_row("absent.onnx")),  # the §7 mount case
        )
        with pytest.raises(ServiceError) as raised:
            await _start(mgr, name)
        assert raised.value.status_code == 503
        assert raised.value.detail["code"] == "ml_model_artifact_missing"
        assert raised.value.detail["state"] == "unavailable"
        assert "refuses rather than trade without the model" in raised.value.detail["message"]
        assert mgr.started == []

    async def test_a_short_history_refuses_as_invalid(
        self, strategy_dir: Path, tmp_path: Path
    ) -> None:
        name = _overlay_strategy_yaml(strategy_dir)
        (strategy_dir / f"{name}.yaml").write_text(
            (strategy_dir / f"{name}.yaml").read_text().replace("2000", "50")
        )
        mgr = _StubManager(config=_config_with(tmp_path), pool=object())
        with pytest.raises(ServiceError) as raised:
            await _start(mgr, name)
        assert raised.value.status_code == 400
        assert raised.value.detail["code"] == "invalid_session"
        assert "required_history" in raised.value.detail["message"]
        assert mgr.started == []


class TestSessionsNeverTakeAModelPin:
    """Research jobs may pin a registry version; a session start never can.

    The registry here would serve the pinned Staging version perfectly (a real
    artifact on the mount, its true sha256) — so the only reason the start
    refuses is that sessions resolve Production and nothing else.
    """

    class _StagingOnlyPool:
        def __init__(self, staging: dict[str, Any]) -> None:
            self.staging = staging

        def acquire(self) -> Any:
            staging = self.staging

            class _Conn:
                async def __aenter__(self) -> Any:
                    return self

                async def __aexit__(self, *exc: Any) -> None:
                    return None

                async def fetchrow(self, sql: str, *args: Any) -> Any:
                    return staging if "WHERE id = $1" in sql and args[0] == staging["id"] else None

            return _Conn()

    async def test_a_pin_in_the_start_payload_never_serves_a_staging_version(
        self, strategy_dir: Path, tmp_path: Path
    ) -> None:
        meta = frozen_spec_metadata(list(DEFAULT_FEATURE_SPEC.feature_names), 3, 4)
        blob = confidence_onnx(meta, len(DEFAULT_FEATURE_SPEC.feature_names))
        (tmp_path / "alpha-v2.onnx").write_bytes(blob)
        staging = {
            **_production_row("alpha-v2.onnx"),
            "id": uuid4(),
            "version": 2,
            "stage": "staging",
            "artifact_sha256": hashlib.sha256(blob).hexdigest(),
        }
        name = _overlay_strategy_yaml(strategy_dir)
        mgr = _StubManager(config=_config_with(tmp_path), pool=self._StagingOnlyPool(staging))

        assert "model_version_id" not in StartSessionRequest.model_fields
        body = StartSessionRequest.model_validate(
            {"strategy_id": name, "model_version_id": str(staging["id"])}
        )
        with pytest.raises(ServiceError) as raised:
            await sessions_svc.start_session(mgr, "user:test", body)

        assert raised.value.detail["code"] == "ml_model_not_promoted"
        assert mgr.started == []
