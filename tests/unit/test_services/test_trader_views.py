"""The trader's risk views: exact Decimal derivations, float at every egress.

P7-B4 finishes the routes' Decimal interior: the percent derivations in
``_account_fields``/``_breakers`` are exact Decimal math quantized at the cent instead
of float products, and the served dicts carry the Decimals — every egress floats them
(the routes' float-typed response models, the trader server's JSON encoder, the MCP
tools' dump). The float product this replaces rounds a hostile half the wrong way:
``round(float(Decimal("0.02675")) * 100, 2)`` is 2.67; the exact answer is 2.68.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from hydra.dashboard.routes.risk import CircuitBreaker, RiskStatus
from hydra.risk.pretrade import RiskConfig
from hydra.risk.service import RiskService
from hydra.risk.state import InMemoryRiskStateStore, RiskState
from hydra.services.trader import LocalTrader
from tests.unit.test_risk.harness import Clock, rules_cache

D = Decimal


class _Mgr:
    """The engine handle the routes read: a manager whose RiskService is the fixture's."""

    def __init__(self, svc: RiskService) -> None:
        self.risk = svc

    def list_sessions(self) -> list[object]:
        return []


async def _hostile_trader() -> LocalTrader:
    """State and config picked so every percent derivation sits on a float-hostile half.

    Drawdown 0.02675 -> 2.675 %, day start 107 against equity 104.325 -> a daily loss
    of 2.675, the same day start at the 2.5 % daily-loss cap -> a limit of 2.675, and a
    3.35 % max-drawdown limit whose unrounded float product leaked as 3.3500000000000005.
    """
    store = InMemoryRiskStateStore(
        paper_capital=D("10000"),
        config=RiskConfig(max_drawdown_pct=D("0.0335"), max_daily_loss_pct=D("0.025")),
        state=RiskState(
            equity=D("104.325"),
            peak_equity=D("10000"),
            current_drawdown=D("0.02675"),
            day_start_equity=D("107"),
        ),
    )
    clock = Clock()
    svc = RiskService(store, clock=clock, market_rules=rules_cache(clock))
    # reads only need the loaded state; evaluate() would recompute drawdown from capital
    await svc.load()
    return LocalTrader(None, _Mgr(svc))


def _floated(value: Any) -> Any:
    """The same shape with every Decimal floated — what a float interior would serve."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _floated(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_floated(v) for v in value]
    return value


async def test_percent_derivations_are_exact_not_float_products() -> None:
    trader = await _hostile_trader()
    body = await trader.risk_status()

    # every hostile half lands on the exact side, not the float product's
    assert body["current_drawdown"] == D("2.68")  # round(float(0.02675) * 100, 2) == 2.67
    assert body["daily_loss"] == D("2.68")  # round(float(2.675), 2) == 2.67
    assert body["daily_loss_limit"] == D("2.68")  # 107 * 0.025 == 2.675, float: 2.67
    assert body["max_drawdown_limit"] == D("3.35")  # float(0.0335) * 100 == 3.3500000000000005
    # the pass-throughs keep the ledger's own Decimals
    assert body["equity"] == D("104.325")
    assert body["peak_equity"] == D("10000")
    assert body["day_start_equity"] == D("107")
    assert body["restrictions"]["size_multiplier"] == D("1")


async def test_the_breakers_derive_their_percent_fields_in_decimal() -> None:
    trader = await _hostile_trader()
    rows = await trader.circuit_breakers()

    assert [r["threshold"] for r in rows] == [
        "0.67% drawdown",
        "1.12% drawdown",
        "2.23% drawdown",
        "3.35% drawdown",
    ]
    assert [r["threshold_pct"] for r in rows] == [
        D("0.6700"),
        D("1.1167"),
        D("2.2333"),
        D("3.3500"),
    ]
    assert {r["current_value"] for r in rows} == {D("2.68")}


async def test_the_response_model_renders_the_decimal_interior_as_floats() -> None:
    """The egress contract: the route's float-typed model fields coerce the Decimals to
    the same bytes a float interior gives — a Decimal never reaches the wire."""
    trader = await _hostile_trader()
    body = await trader.risk_status()

    assert RiskStatus.model_validate(body).model_dump_json() == (
        RiskStatus.model_validate(_floated(body)).model_dump_json()
    )
    model = RiskStatus.model_validate(body)
    assert (model.current_drawdown, model.daily_loss, model.daily_loss_limit) == (
        2.68,
        2.68,
        2.68,
    )
    assert (model.equity, model.peak_equity, model.day_start_equity) == (
        104.325,
        10000.0,
        107.0,
    )
    breakers = [CircuitBreaker.model_validate(r) for r in body["circuit_breakers"]]
    assert [b.threshold_pct for b in breakers] == [0.67, 1.1167, 2.2333, 3.35]
    assert {b.current_value for b in breakers} == {2.68}


@pytest.mark.parametrize(
    ("op", "field"),
    [("risk_status", "current_drawdown"), ("circuit_breakers", "current_value")],
)
async def test_every_read_op_serves_the_same_hostile_exactness(op: str, field: str) -> None:
    """The hostile half is exact on every route that derives it (status and breakers)."""
    trader = await _hostile_trader()
    result = await getattr(trader, op)()

    rows = result if isinstance(result, list) else [result]
    assert all(row[field] == D("2.68") for row in rows)
