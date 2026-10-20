"""OrderManager + RiskService + real paper executor: exits are never blocked, reduce_only clamps."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from datetime import timedelta
from decimal import Decimal

import pytest
from prometheus_client import REGISTRY

from hydra.core import metrics
from hydra.core.types import Side
from hydra.execution.order_manager import OrderManager, RiskRejectedError, _realized_pnl
from hydra.execution.paper_trading import PaperTradingExecutor
from hydra.execution.portfolio_state import build_portfolio_state
from hydra.risk.circuit_breakers import CircuitBreakerState
from hydra.risk.pretrade import PreTradeRiskManager, RiskConfig
from hydra.risk.state import RiskState
from tests.unit.test_risk.harness import (
    BTC,
    T0,
    Account,
    FillBus,
    flat_bar,
    make_account,
    order,
    permissive,
)

BUY, SELL = Side.BUY, Side.SELL


@pytest.fixture(autouse=True)
def _fresh_metrics() -> Iterator[None]:
    metrics._reset_metrics()
    yield
    metrics._reset_metrics()


async def _holding_long(acct: Account) -> PaperTradingExecutor:
    """Long 0.01 BTC from 60000, marked at 50000."""
    ex = await acct.add_session("s1", "10000")
    await acct.mark("60000")
    await ex.create_order(BTC, "BUY", "MARKET", Decimal("0.01"))
    await acct.mark("50000")
    return ex


Setup = Callable[[], Awaitable[Account]]


def _trade_pnl(le: str | None = None) -> float | None:
    """OrderManager's hydra_trade_pnl samples (strategy 's'); count, or the bucket <= le."""
    labels = {"symbol": BTC, "strategy_id": "s"}
    if le is None:
        return REGISTRY.get_sample_value("hydra_trade_pnl_count", labels)
    return REGISTRY.get_sample_value("hydra_trade_pnl_bucket", {**labels, "le": le})


async def _heat() -> Account:
    return await make_account(permissive(max_portfolio_heat=Decimal("0.04")))


async def _tier2() -> Account:
    return await make_account(
        permissive(), state=RiskState(cb=CircuitBreakerState(tier=2, tier_since=T0))
    )


async def _tier3() -> Account:
    state = RiskState(cb=CircuitBreakerState(tier=3, tier_since=T0, requires_manual_review=True))
    return await make_account(permissive(), state=state)


async def _tier4() -> Account:
    state = RiskState(cb=CircuitBreakerState(tier=4, tier_since=T0, requires_manual_review=True))
    return await make_account(permissive(), state=state)


async def _daily_halt() -> Account:
    state = RiskState(cb=CircuitBreakerState(daily_halt_until=T0 + timedelta(hours=10)))
    return await make_account(permissive(), state=state)


async def _pause() -> Account:
    state = RiskState(cb=CircuitBreakerState(consecutive_pause_until=T0 + timedelta(hours=2)))
    return await make_account(permissive(), state=state)


async def _kill_switch() -> Account:
    return await make_account(permissive(), kill_switch=True)


async def _rate_limited() -> Account:
    return await make_account(permissive(max_orders_per_minute=0))


CONDITIONS: dict[str, tuple[Setup, str]] = {
    "gross_exposure": (_heat, "portfolio_heat"),
    "cb_tier_2": (_tier2, "circuit_breaker"),
    "cb_tier_3": (_tier3, "circuit_breaker"),
    "cb_tier_4": (_tier4, "circuit_breaker"),
    "daily_halt": (_daily_halt, "daily_loss_halt"),
    "consecutive_pause": (_pause, "consecutive_loss_pause"),
    "kill_switch": (_kill_switch, "kill_switch"),
    "rate_limit": (_rate_limited, "rate_limit"),
}


class TestExitNeverBlocked:
    @pytest.mark.parametrize("condition", list(CONDITIONS))
    async def test_exit_never_blocked(self, condition: str) -> None:
        setup, check = CONDITIONS[condition]
        acct = await setup()
        ex = await _holding_long(acct)

        with pytest.raises(RiskRejectedError) as info:
            await acct.submit("s1", BUY, "0.001")
        assert info.value.check == check

        assert await acct.submit("s1", SELL, "0.01", reduce_only=True)
        assert await ex.get_positions(BTC) == []
        assert (await ex.get_balance())["USDT"] == Decimal("9900.00")
        fill = acct.buses["s1"].fills[-1]
        assert (fill.reduce_only, fill.session_id, fill.realized_pnl) == (
            True,
            "s1",
            Decimal("-100.00"),
        )
        assert (_trade_pnl(), _trade_pnl("-100.0"), _trade_pnl("-200.0")) == (1.0, 1.0, 0.0)

    async def test_duplicate_reductions_both_fill(self) -> None:
        acct = await make_account(permissive())
        ex = await _holding_long(acct)
        acct.managers["s1"]._dedup_window = 60.0
        assert await acct.submit("s1", SELL, "0.005", reduce_only=True)
        assert await acct.submit("s1", SELL, "0.005", reduce_only=True)
        assert await ex.get_positions(BTC) == []

    async def test_unflagged_exit_within_position_is_a_reduction(self) -> None:
        acct = await _kill_switch()
        ex = await _holding_long(acct)
        assert await acct.submit("s1", SELL, "0.01")
        assert await ex.get_positions(BTC) == []


class TestConcurrentExits:
    async def test_two_reduce_only_exits_fill_once(self) -> None:
        acct = await make_account(permissive())
        ex = await _holding_long(acct)
        results = await asyncio.gather(
            acct.submit("s1", SELL, "0.01", reduce_only=True),
            acct.submit("s1", SELL, "0.01", reduce_only=True),
        )
        assert sorted(r is None for r in results) == [False, True]
        assert await ex.get_positions(BTC) == []
        assert (await ex.get_balance())["USDT"] == Decimal("9900.00")
        assert [f.quantity for f in acct.buses["s1"].fills] == [Decimal("0.01")]

    async def test_second_unflagged_exit_is_an_opening_and_refused(self) -> None:
        acct = await make_account(permissive())
        ex = await _holding_long(acct)
        results = await asyncio.gather(
            acct.submit("s1", SELL, "0.01"),
            acct.submit("s1", SELL, "0.01"),
            return_exceptions=True,
        )
        assert sum(isinstance(r, str) for r in results) == 1
        errors = [(r.check, r.intent) for r in results if isinstance(r, RiskRejectedError)]
        assert errors == [("spot_short", "open")]
        assert await ex.get_positions(BTC) == []
        assert (await ex.get_balance())["USDT"] == Decimal("9900.00")


class TestRealizedPnl:
    async def test_net_of_exit_and_pro_rata_entry_fee(self) -> None:
        acct = await make_account(permissive())
        await acct.add_session("s1", "10000", fee="0.001")
        await acct.mark("50000")
        assert await acct.submit("s1", BUY, "0.01")
        await acct.mark("50100")
        assert await acct.submit("s1", SELL, "0.01", reduce_only=True)
        fill = acct.buses["s1"].fills[-1]
        assert (fill.gross_pnl, fill.fee, fill.realized_pnl) == (
            Decimal("1.0"),
            Decimal("0.501"),
            Decimal("-0.001"),
        )
        assert acct.store.state.consecutive_losses == 1
        assert (_trade_pnl(), _trade_pnl("0.0"), _trade_pnl("-10.0")) == (1.0, 1.0, 0.0)


class TestRealizedPnlFromResponse:
    @pytest.mark.parametrize(
        ("side", "resp", "fee", "expected"),
        [
            (SELL, {"pnl": 1.0, "cost": 501.0}, "0.501", (Decimal("-0.001"), Decimal("1.0"))),
            (BUY, {"pnl": 1.0, "cost": 499.0}, "0.499", (Decimal("0.001"), Decimal("1.0"))),
            (
                SELL,
                {"pnl": 1.0, "cost": 501.0, "net_pnl": -0.002},
                "0.501",
                (Decimal("-0.002"), Decimal("1.0")),
            ),
            (SELL, {"cost": 501.0}, "0.501", (None, None)),
        ],
    )
    def test_gross_only_response_takes_pro_rata_entry_fee(
        self,
        side: Side,
        resp: dict[str, float],
        fee: str,
        expected: tuple[Decimal | None, Decimal | None],
    ) -> None:
        assert _realized_pnl(resp, Decimal(fee), side) == expected


class TestReduceOnlyClamp:
    async def test_reduce_only_clamped_to_position(self) -> None:
        acct = await make_account(permissive())
        ex = await _holding_long(acct)
        assert await acct.submit("s1", SELL, "0.05", reduce_only=True)
        assert await ex.get_positions(BTC) == []
        assert (await ex.get_balance())["USDT"] == Decimal("9900.00")
        assert acct.buses["s1"].fills[-1].quantity == Decimal("0.01")

    async def test_reduce_only_without_position_is_noop(self) -> None:
        acct = await make_account(permissive())
        ex = await acct.add_session("s1", "10000")
        await acct.mark("60000")
        assert await acct.submit("s1", SELL, "0.01", reduce_only=True) is None
        assert acct.buses["s1"].fills == []
        assert await ex.get_filled_orders() == []

    async def test_reduce_only_buy_against_long_is_noop(self) -> None:
        acct = await make_account(permissive())
        ex = await _holding_long(acct)
        assert await acct.submit("s1", BUY, "0.01", reduce_only=True) is None
        assert [p.quantity for p in await ex.get_positions(BTC)] == [Decimal("0.01")]


class TestOpeningPath:
    async def test_market_order_priced_at_mark_on_om_path(self) -> None:
        acct = await make_account(RiskConfig())
        await acct.add_session("s1", "10000")
        await acct.mark("60000")
        with pytest.raises(RiskRejectedError, match="3000000") as info:
            await acct.submit("s1", BUY, "50")
        assert (info.value.check, info.value.intent) == ("position_size", "open")
        assert (
            REGISTRY.get_sample_value(
                "hydra_risk_rejections_total", {"check": "position_size", "intent": "open"}
            )
            == 1.0
        )
        with pytest.raises(RiskRejectedError, match="non-positive"):
            await acct.submit("s1", BUY, "-5")

    async def test_opening_fill_has_no_realized_pnl(self) -> None:
        acct = await make_account(permissive())
        await acct.add_session("s1", "10000")
        await acct.mark("60000")
        assert await acct.submit("s1", BUY, "0.01")
        fill = acct.buses["s1"].fills[-1]
        assert (fill.realized_pnl, fill.reduce_only, fill.price) == (
            None,
            False,
            Decimal("60000.0"),
        )

    async def test_dedup_applies_to_openings(self) -> None:
        acct = await make_account(permissive())
        await acct.add_session("s1", "10000")
        await acct.mark("60000")
        acct.managers["s1"]._dedup_window = 60.0
        assert await acct.submit("s1", BUY, "0.01")
        with pytest.raises(RiskRejectedError, match="Duplicate order rejected"):
            await acct.submit("s1", BUY, "0.01")


class TestLegacyPath:
    async def test_pre_risk_service_path_prices_at_mark_and_exempts_exits(self) -> None:
        ex = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("10000")},
            slippage_pct=Decimal("0"),
            fee_pct=Decimal("0"),
        )
        await ex.on_closed_bar(BTC, flat_bar(Decimal("60000"), T0))
        await ex.create_order(BTC, "BUY", "MARKET", Decimal("0.01"))
        await ex.on_closed_bar(BTC, flat_bar(Decimal("50000"), T0 + timedelta(hours=1)))

        async def build() -> object:
            return await build_portfolio_state(ex, [BTC], None)

        om = OrderManager(
            executor=ex,
            event_bus=FillBus(None, ""),
            risk_checker=PreTradeRiskManager(RiskConfig(max_portfolio_heat=Decimal("0.04"))),
            portfolio_state_builder=build,
            dedup_window=0.0,
        )
        with pytest.raises(RiskRejectedError, match="3000000"):
            await om.submit_order(order(BUY, "60"))
        with pytest.raises(RiskRejectedError) as info:
            await om.submit_order(order(BUY, "0.001"))
        assert info.value.check == "portfolio_heat"
        assert await om.submit_order(order(SELL, "0.01"))
        assert await ex.get_positions(BTC) == []
