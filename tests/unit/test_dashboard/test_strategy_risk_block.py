"""Builder SL/TP (SM-7): persisted into the YAML ``risk:`` block, read back, enforced in paper."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from hydra.dashboard.api import app
from hydra.strategy.config import load_strategy_config
from tests.fixtures.universe import seeded_universe_pool
from tests.unit.test_execution.session_harness import Harness, executor

from .conftest import TEST_API_TOKEN


@pytest.fixture()
async def api(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    # the stored universe row every builder save now reads (the refresher's steady state)
    monkeypatch.setattr(app.state, "db_pool", seeded_universe_pool())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://engine",
        headers={"Authorization": f"Bearer {TEST_API_TOKEN}"},
    ) as client:
        yield client


def _payload(**risk: Any) -> dict[str, Any]:
    return {
        "name": "Builder SLTP",
        "description": "",
        "exchange_id": "binance",
        "symbol": "BTCUSDT",
        "rules": {
            "entry_long": {
                "operator": "AND",
                "conditions": [
                    {"indicator": "close", "params": {}, "comparator": "less_than", "value": 100.5},
                    {
                        "indicator": "close",
                        "params": {},
                        "comparator": "greater_than",
                        "value": 99.5,
                    },
                ],
            },
            "exit_long": None,
            "entry_short": None,
            "exit_short": None,
        },
        "timeframes": {"primary": "1h"},
        "risk": {
            "stop_loss_method": "fixed_pct",
            "stop_loss_value": 3.0,
            "take_profit_method": "fixed_pct",
            "take_profit_value": 6.0,
            "sizing_method": "fixed_fractional",
            "sizing_params": {"risk_per_trade_pct": 1.0, "max_position_pct": 10.0},
            **risk,
        },
        "enable_immediately": False,
    }


async def test_builder_sl_tp_is_persisted_read_back_and_enforced(
    api: httpx.AsyncClient, strategies_dir: Path
) -> None:
    resp = await api.post("/api/strategies/save", json=_payload())
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]
    path = Path(resp.json()["config_path"])
    assert yaml.safe_load(path.read_text())["risk"] == {
        "stop_loss_method": "fixed_pct",
        "stop_loss_value": 3.0,
        "take_profit_method": "fixed_pct",
        "take_profit_value": 6.0,
        "atr_period": 14,
    }
    assert load_strategy_config(path).risk.take_profit_value == 6.0
    detail = (await api.get(f"/api/strategies/{sid}")).json()
    assert {k: detail["risk"][k] for k in ("stop_loss_value", "take_profit_value")} == {
        "stop_loss_value": 3.0,
        "take_profit_value": 6.0,
    }

    h = Harness(strategies_dir, history_bars=60)
    await h.start()
    try:
        session = await h.session(sid)
        await h.bar(session, "100")
        legs = sorted(
            (o["type"], o["stop_price"], o["price"])
            for o in await executor(session).fetch_open_orders()
        )
        # entry 100.1: stop 100.1 x 0.97, take-profit 100.1 x 1.06
        assert legs == [("LIMIT", None, 106.106), ("STOP_MARKET", 97.097, None)]
    finally:
        await h.close()


async def test_builder_trailing_stop_saves_without_its_take_profit(
    api: httpx.AsyncClient,
) -> None:
    # the builder always sends a take-profit; a trailing stop sells the whole position, so it wins
    resp = await api.post(
        "/api/strategies/save",
        json=_payload(stop_loss_method="trailing_stop", stop_loss_value=2.0),
    )
    assert resp.status_code == 201, resp.text
    path = Path(resp.json()["config_path"])
    assert yaml.safe_load(path.read_text())["risk"] == {
        "stop_loss_method": "trailing_stop",
        "stop_loss_value": 2.0,
        "take_profit_method": "none",
        "take_profit_value": 0.0,
        "atr_period": 14,
    }


async def test_invalid_builder_risk_settings_are_refused(api: httpx.AsyncClient) -> None:
    resp = await api.post("/api/strategies/save", json=_payload(stop_loss_value=150.0))
    assert resp.status_code == 422
    assert "below 100" in resp.json()["detail"]


async def test_strategy_without_a_risk_block_reports_the_default_stop(
    api: httpx.AsyncClient, strategies_dir: Path
) -> None:
    (strategies_dir / "plain.yaml").write_text(
        "id: plain\nname: Plain\n"
        "strategy_class: hydra.strategy.builtin.rule_based.RuleBasedStrategy\n"
        "parameters:\n  rules: {}\n"
    )
    risk = (await api.get("/api/strategies/plain")).json()["risk"]
    assert (risk["stop_loss_method"], risk["stop_loss_value"], risk["take_profit_method"]) == (
        "fixed_pct",
        5.0,
        "none",
    )
