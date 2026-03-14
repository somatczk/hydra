"""Tests for backtest dashboard routes.

Tests cover:
- POST /run returns 202 with task_id
- GET /status returns correct status for known/unknown tasks
- GET /results returns list of summaries
- GET /results/{id} returns detail or 404
- PATCH /results/{id} renames a result
- DELETE /results/{id} removes a result
- GET /results/{id}/verify recomputes metrics
- ``_generate_sample_bars`` produces correct count and valid OHLCV bars
- ``_default_strategy_config`` returns valid StrategyConfig
- Background task lifecycle: queued -> running -> completed
- Background task handles failures gracefully
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hydra.dashboard.routes.backtest import (
    _RESULTS,
    _TASKS,
    BacktestRunRequest,
    _default_strategy_config,
    _generate_sample_bars,
    _run_backtest_task,
    router,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_app(pool: object | None = None) -> FastAPI:
    test_app = FastAPI()
    test_app.include_router(router)
    test_app.state.db_pool = pool
    return test_app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(_make_app(pool=None))


@pytest.fixture(autouse=True)
def _clean_tasks() -> None:
    """Ensure task/result state is clean between tests.

    We snapshot and restore the dicts to avoid cross-test pollution while
    preserving the module-level seed data in ``_RESULTS``.
    """
    saved_tasks = dict(_TASKS)
    saved_results = dict(_RESULTS)
    _TASKS.clear()
    yield
    _TASKS.clear()
    _TASKS.update(saved_tasks)
    _RESULTS.clear()
    _RESULTS.update(saved_results)


# ---------------------------------------------------------------------------
# _generate_sample_bars helper
# ---------------------------------------------------------------------------


class TestGenerateSampleBars:
    def test_correct_count(self) -> None:
        bars = _generate_sample_bars(100)
        assert len(bars) == 100

    def test_ohlcv_fields_valid(self) -> None:
        bars = _generate_sample_bars(10)
        for bar in bars:
            assert isinstance(bar.open, Decimal)
            assert isinstance(bar.high, Decimal)
            assert isinstance(bar.low, Decimal)
            assert isinstance(bar.close, Decimal)
            assert isinstance(bar.volume, Decimal)
            assert bar.timestamp is not None
            assert bar.timestamp.tzinfo is not None

    def test_high_ge_low(self) -> None:
        bars = _generate_sample_bars(200, seed=99)
        for bar in bars:
            assert bar.high >= bar.low, f"high={bar.high} < low={bar.low}"

    def test_volume_positive(self) -> None:
        bars = _generate_sample_bars(50)
        for bar in bars:
            assert bar.volume >= 0

    def test_deterministic_with_same_seed(self) -> None:
        bars1 = _generate_sample_bars(20, seed=42)
        bars2 = _generate_sample_bars(20, seed=42)
        for a, b in zip(bars1, bars2, strict=True):
            assert a.close == b.close

    def test_different_seed_different_bars(self) -> None:
        bars1 = _generate_sample_bars(10, seed=1)
        bars2 = _generate_sample_bars(10, seed=2)
        closes1 = [b.close for b in bars1]
        closes2 = [b.close for b in bars2]
        assert closes1 != closes2

    def test_timestamps_are_sequential(self) -> None:
        bars = _generate_sample_bars(5)
        for i in range(1, len(bars)):
            assert bars[i].timestamp > bars[i - 1].timestamp

    def test_price_stays_above_floor(self) -> None:
        """Price should never go below the 100.0 floor."""
        bars = _generate_sample_bars(1000, seed=0)
        for bar in bars:
            assert float(bar.close) >= 100.0


# ---------------------------------------------------------------------------
# _default_strategy_config helper
# ---------------------------------------------------------------------------


class TestDefaultStrategyConfig:
    def test_returns_strategy_class_and_config(self) -> None:
        _cls, config = _default_strategy_config("test-strat")
        assert config.id == "test-strat"
        assert config.name == "Backtest Strategy"
        assert len(config.symbols) > 0
        assert "rules" in config.parameters

    def test_custom_symbol(self) -> None:
        _, config = _default_strategy_config("s1", symbol="ETHUSDT")
        assert "ETHUSDT" in config.symbols

    def test_config_has_valid_structure(self) -> None:
        _, config = _default_strategy_config("s1")
        assert config.exchange.exchange_id == "binance"
        assert config.timeframes.primary.value == "1h"
        rules = config.parameters["rules"]
        assert "entry_long" in rules
        assert "exit_long" in rules


# ---------------------------------------------------------------------------
# POST /run endpoint
# ---------------------------------------------------------------------------


class TestRunBacktest:
    def test_returns_202_with_task_id(self, client: TestClient) -> None:
        resp = client.post(
            "/api/backtest/run",
            json={
                "strategy_id": "strat-1",
                "start_date": "2026-01-01",
                "end_date": "2026-03-01",
            },
        )
        assert resp.status_code == 202
        data = resp.json()
        assert "task_id" in data
        assert data["status"] == "queued"
        assert data["task_id"].startswith("task-")

    def test_task_id_is_unique(self, client: TestClient) -> None:
        payload = {
            "strategy_id": "strat-1",
            "start_date": "2026-01-01",
            "end_date": "2026-03-01",
        }
        r1 = client.post("/api/backtest/run", json=payload)
        r2 = client.post("/api/backtest/run", json=payload)
        assert r1.json()["task_id"] != r2.json()["task_id"]

    def test_default_initial_capital(self, client: TestClient) -> None:
        resp = client.post(
            "/api/backtest/run",
            json={
                "strategy_id": "strat-1",
                "start_date": "2026-01-01",
                "end_date": "2026-03-01",
            },
        )
        assert resp.status_code == 202

    def test_custom_initial_capital(self, client: TestClient) -> None:
        resp = client.post(
            "/api/backtest/run",
            json={
                "strategy_id": "strat-1",
                "start_date": "2026-01-01",
                "end_date": "2026-03-01",
                "initial_capital": 50000,
            },
        )
        assert resp.status_code == 202

    def test_run_with_name(self, client: TestClient) -> None:
        resp = client.post(
            "/api/backtest/run",
            json={
                "strategy_id": "strat-1",
                "start_date": "2026-01-01",
                "end_date": "2026-03-01",
                "name": "My Test Backtest",
            },
        )
        assert resp.status_code == 202


# ---------------------------------------------------------------------------
# GET /status/{task_id} endpoint
# ---------------------------------------------------------------------------


class TestBacktestStatus:
    def test_status_for_known_task(self, client: TestClient) -> None:
        run_resp = client.post(
            "/api/backtest/run",
            json={
                "strategy_id": "strat-1",
                "start_date": "2026-01-01",
                "end_date": "2026-03-01",
            },
        )
        task_id = run_resp.json()["task_id"]

        resp = client.get(f"/api/backtest/status/{task_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_id"] == task_id
        assert data["status"] in ("queued", "running", "completed", "failed")
        assert "progress" in data

    def test_status_returns_404_for_unknown_task(self, client: TestClient) -> None:
        resp = client.get("/api/backtest/status/nonexistent-task")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /results and /results/{id} endpoints
# ---------------------------------------------------------------------------


class TestBacktestResults:
    def test_list_results(self, client: TestClient) -> None:
        resp = client.get("/api/backtest/results")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        # Seed data should include at least bt-1, bt-2, bt-3
        assert len(data) >= 3
        for item in data:
            assert "id" in item
            assert "strategy" in item
            assert "period" in item
            assert "status" in item
            assert "metrics" in item
            assert "name" in item

    def test_list_results_includes_name(self, client: TestClient) -> None:
        resp = client.get("/api/backtest/results")
        data = resp.json()
        bt1 = next(r for r in data if r["id"] == "bt-1")
        assert bt1["name"] == "LSTM Momentum Q1"

    def test_get_result_detail(self, client: TestClient) -> None:
        resp = client.get("/api/backtest/results/bt-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "bt-1"
        assert "equity_curve" in data
        assert "trades" in data
        assert "metrics" in data
        assert data["metrics"]["total_trades"] == 142
        assert data["name"] == "LSTM Momentum Q1"

    def test_get_result_detail_includes_stopped_reason(self, client: TestClient) -> None:
        resp = client.get("/api/backtest/results/bt-1")
        data = resp.json()
        assert "stopped_reason" in data

    def test_get_result_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/backtest/results/nonexistent")
        assert resp.status_code == 404

    def test_result_detail_has_trade_records(self, client: TestClient) -> None:
        resp = client.get("/api/backtest/results/bt-1")
        data = resp.json()
        assert len(data["trades"]) > 0
        trade = data["trades"][0]
        assert "entry_time" in trade
        assert "exit_time" in trade
        assert "side" in trade
        assert "entry_price" in trade
        assert "exit_price" in trade
        assert "pnl" in trade


# ---------------------------------------------------------------------------
# PATCH /results/{id} -- rename
# ---------------------------------------------------------------------------


class TestRenameBacktest:
    def test_rename_result(self, client: TestClient) -> None:
        resp = client.patch(
            "/api/backtest/results/bt-1",
            json={"name": "New Name"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "New Name"
        assert data["id"] == "bt-1"

    def test_rename_persists_in_list(self, client: TestClient) -> None:
        client.patch("/api/backtest/results/bt-2", json={"name": "Renamed"})
        resp = client.get("/api/backtest/results")
        data = resp.json()
        bt2 = next(r for r in data if r["id"] == "bt-2")
        assert bt2["name"] == "Renamed"

    def test_rename_not_found(self, client: TestClient) -> None:
        resp = client.patch(
            "/api/backtest/results/nonexistent",
            json={"name": "Nope"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /results/{id}
# ---------------------------------------------------------------------------


class TestDeleteBacktest:
    def test_delete_result(self, client: TestClient) -> None:
        resp = client.delete("/api/backtest/results/bt-1")
        assert resp.status_code == 204

    def test_delete_removes_from_list(self, client: TestClient) -> None:
        client.delete("/api/backtest/results/bt-1")
        resp = client.get("/api/backtest/results")
        ids = [r["id"] for r in resp.json()]
        assert "bt-1" not in ids

    def test_delete_makes_detail_404(self, client: TestClient) -> None:
        client.delete("/api/backtest/results/bt-1")
        resp = client.get("/api/backtest/results/bt-1")
        assert resp.status_code == 404

    def test_delete_not_found(self, client: TestClient) -> None:
        resp = client.delete("/api/backtest/results/nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /results/{id}/verify
# ---------------------------------------------------------------------------


class TestVerifyBacktest:
    def test_verify_result(self, client: TestClient) -> None:
        resp = client.get("/api/backtest/results/bt-1/verify")
        assert resp.status_code == 200
        data = resp.json()
        assert "trade_count_match" in data
        assert "win_rate_match" in data
        assert "total_pnl_match" in data
        assert "all_passed" in data
        assert isinstance(data["computed_trade_count"], int)
        assert isinstance(data["computed_total_pnl"], float)

    def test_verify_seed_data_fails(self, client: TestClient) -> None:
        """Seed data has incomplete trades, so verification should fail."""
        resp = client.get("/api/backtest/results/bt-1/verify")
        data = resp.json()
        # bt-1 reports 142 trades but only has 1 in the trades array
        assert data["trade_count_match"] is False

    def test_verify_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/backtest/results/nonexistent/verify")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Background task lifecycle
# ---------------------------------------------------------------------------


class TestBacktestBackgroundTask:
    @pytest.mark.asyncio
    async def test_task_completes_successfully(self) -> None:
        """The background task should move through queued -> running -> completed."""
        task_id = "test-lifecycle"
        body = BacktestRunRequest(
            strategy_id="strat-1",
            start_date="2026-01-01",
            end_date="2026-02-01",
            initial_capital=10000.0,
        )
        _TASKS[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "progress": 0.0,
            "request": body.model_dump(),
        }

        await _run_backtest_task(task_id, body, pool=None)

        assert _TASKS[task_id]["status"] == "completed"
        assert _TASKS[task_id]["progress"] == 100.0
        assert "result_id" in _TASKS[task_id]
        result_id = _TASKS[task_id]["result_id"]
        assert result_id in _RESULTS
        result = _RESULTS[result_id]
        assert result["status"] == "completed"
        assert "equity_curve" in result
        assert "trades" in result
        assert "metrics" in result

    @pytest.mark.asyncio
    async def test_task_stores_name(self) -> None:
        """Background task should store the name from the request."""
        task_id = "test-name"
        body = BacktestRunRequest(
            strategy_id="strat-1",
            start_date="2026-01-01",
            end_date="2026-02-01",
            initial_capital=10000.0,
            name="My Named Backtest",
        )
        _TASKS[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "progress": 0.0,
            "request": body.model_dump(),
        }

        await _run_backtest_task(task_id, body, pool=None)

        result_id = _TASKS[task_id]["result_id"]
        assert _RESULTS[result_id]["name"] == "My Named Backtest"

    @pytest.mark.asyncio
    async def test_task_stores_stopped_reason(self) -> None:
        """Background task should store stopped_reason from the engine result."""
        task_id = "test-stopped"
        body = BacktestRunRequest(
            strategy_id="strat-1",
            start_date="2026-01-01",
            end_date="2026-02-01",
            initial_capital=10000.0,
        )
        _TASKS[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "progress": 0.0,
            "request": body.model_dump(),
        }

        await _run_backtest_task(task_id, body, pool=None)

        result_id = _TASKS[task_id]["result_id"]
        # stopped_reason should be present (None for normal runs)
        assert "stopped_reason" in _RESULTS[result_id]

    @pytest.mark.asyncio
    async def test_task_handles_failure(self) -> None:
        """When the runner raises, the task should be marked as failed."""
        task_id = "test-failure"
        body = BacktestRunRequest(
            strategy_id="strat-1",
            start_date="2026-01-01",
            end_date="2026-02-01",
            initial_capital=10000.0,
        )
        _TASKS[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "progress": 0.0,
            "request": body.model_dump(),
        }

        with patch("hydra.dashboard.routes.backtest.BacktestRunner") as mock_runner_cls:
            mock_runner = mock_runner_cls.return_value
            mock_runner.run = AsyncMock(side_effect=RuntimeError("Backtest exploded"))

            await _run_backtest_task(task_id, body, pool=None)

        assert _TASKS[task_id]["status"] == "failed"
        assert _TASKS[task_id]["progress"] == 0.0
        assert "error" in _TASKS[task_id]

    @pytest.mark.asyncio
    async def test_task_tries_db_bars_first(self) -> None:
        """When pool is present but returns no rows, falls back to synthetic."""
        task_id = "test-db-fallback"
        body = BacktestRunRequest(
            strategy_id="strat-1",
            start_date="2026-01-01",
            end_date="2026-02-01",
            initial_capital=10000.0,
        )
        _TASKS[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "progress": 0.0,
            "request": body.model_dump(),
        }

        pool = MagicMock()
        conn = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool.acquire.return_value = ctx
        # Return empty rows so it falls back to synthetic bars
        conn.fetch = AsyncMock(return_value=[])

        await _run_backtest_task(task_id, body, pool=pool)

        assert _TASKS[task_id]["status"] == "completed"
        conn.fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_task_db_exception_falls_back_to_synthetic(self) -> None:
        """When pool.acquire raises, falls back to synthetic bars."""
        task_id = "test-db-error"
        body = BacktestRunRequest(
            strategy_id="strat-1",
            start_date="2026-01-01",
            end_date="2026-02-01",
            initial_capital=10000.0,
        )
        _TASKS[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "progress": 0.0,
            "request": body.model_dump(),
        }

        pool = MagicMock()
        conn = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool.acquire.return_value = ctx
        conn.fetch = AsyncMock(side_effect=RuntimeError("DB error"))

        await _run_backtest_task(task_id, body, pool=pool)

        # Should still complete using synthetic bars
        assert _TASKS[task_id]["status"] == "completed"
