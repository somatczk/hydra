"""PreTradeRiskManager: mark pricing, open vs reduce, quote-only balance, account limits."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from hydra.core.types import Direction, MarketType, OrderRequest, OrderType, Position, Side, Symbol
from hydra.risk.circuit_breakers import CircuitBreakerRestrictions
from hydra.risk.market_rules import MarketRules
from hydra.risk.pretrade import (
    OPEN,
    REDUCE,
    PortfolioState,
    PreTradeRiskManager,
    RiskConfig,
    classify_intent,
)

BTC = Symbol("BTCUSDT")


def _order(qty: str, side: Side = Side.BUY, **kw: Any) -> OrderRequest:
    return OrderRequest(
        symbol=BTC,
        side=side,
        order_type=OrderType.MARKET,
        quantity=Decimal(qty),
        strategy_id="s",
        exchange_id="binance",
        market_type=MarketType.SPOT,
        **kw,
    )


def _long(qty: str, entry: str = "60000", symbol: str = "BTCUSDT") -> Position:
    return Position(
        symbol=Symbol(symbol),
        direction=Direction.LONG,
        quantity=Decimal(qty),
        avg_entry_price=Decimal(entry),
        unrealized_pnl=Decimal("0"),
        realized_pnl=Decimal("0"),
        strategy_id="s",
        exchange_id="binance",
    )


def _state(**kw: Any) -> PortfolioState:
    base: dict[str, Any] = {
        "portfolio_value": Decimal("10000"),
        "balances": {"USDT": Decimal("10000")},
        "marks": {"BTCUSDT": Decimal("60000")},
        "restrictions": CircuitBreakerRestrictions(),
    }
    base.update(kw)
    return PortfolioState(**base)


def _check(order: OrderRequest, state: PortfolioState, **cfg: Any) -> tuple[bool, str, str]:
    d = PreTradeRiskManager(RiskConfig(**cfg)).evaluate(order, state)
    return d.approved, d.check, d.reason


class TestMarkPricing:
    def test_market_order_priced_at_mark(self) -> None:
        ok, check, reason = _check(_order("50"), _state())
        assert (ok, check) == (False, "position_size")
        assert "3000000" in reason

    def test_negative_quantity_rejected(self) -> None:
        ok, check, reason = _check(_order("-5"), _state())
        assert (ok, check) == (False, "sanity")
        assert "non-positive quantity" in reason

    def test_opening_without_mark_or_price_rejected(self) -> None:
        ok, check, reason = _check(_order("0.001"), _state(marks={}))
        assert (ok, check) == (False, "no_mark")
        assert "no mark price" in reason

    def test_mark_wins_over_stale_order_price(self) -> None:
        ok, check, _ = _check(_order("0.02", price=Decimal("1")), _state())
        assert (ok, check) == (False, "position_size")

    def test_small_order_at_mark_approved(self) -> None:
        assert _check(_order("0.01"), _state())[0] is True


class TestQuoteOnlyBalance:
    def test_quote_only_balance(self) -> None:
        state = _state(balances={"USDT": Decimal("100"), "BTC": Decimal("1")})
        ok, check, reason = _check(_order("0.01"), state)
        assert (ok, check) == (False, "balance")
        assert "insufficient USDT" in reason


class TestIntent:
    def test_reduce_only_is_reduce(self) -> None:
        assert classify_intent(_order("1", Side.SELL, reduce_only=True)) == REDUCE

    def test_unflagged_sell_within_long_is_an_opening_spot_short(self) -> None:
        assert classify_intent(_order("0.01", Side.SELL)) == OPEN
        state = _state(positions=[_long("0.01")])
        ok, check, _ = _check(_order("0.01", Side.SELL), state)
        assert (ok, check) == (False, "spot_short")

    def test_buy_with_long_is_open(self) -> None:
        assert classify_intent(_order("0.01")) == OPEN

    @pytest.mark.parametrize(
        "blocker",
        [
            {"kill_switch": True},
            {"state_available": False},
            {"restrictions": CircuitBreakerRestrictions(tier=3, must_flatten=True)},
            {"restrictions": CircuitBreakerRestrictions(tier=4, is_shutdown=True)},
            {"restrictions": CircuitBreakerRestrictions(daily_halt=True)},
            {"restrictions": CircuitBreakerRestrictions(consecutive_loss_pause=True)},
            {"opening_orders_last_minute": 99},
            {"marks": {}},
            {"balances": {"USDT": Decimal("0")}},
            {"account_positions": [_long("0.5", symbol=f"X{i}USDT") for i in range(20)]},
        ],
    )
    def test_exit_never_blocked(self, blocker: dict[str, Any]) -> None:
        state = _state(positions=[_long("0.01")], **blocker)
        exit_order = _order("0.01", Side.SELL, reduce_only=True)
        assert _check(exit_order, state) == (True, "reduce", "reducing order: entry checks exempt")
        assert _check(_order("0.01"), state)[0] is False


class TestOpeningChecks:
    def test_kill_switch(self) -> None:
        assert _check(_order("0.01"), _state(kill_switch=True))[1] == "kill_switch"

    def test_state_unavailable(self) -> None:
        _, check, reason = _check(_order("0.01"), _state(state_available=False))
        assert (check, reason) == ("risk_state", "risk state unavailable")

    @pytest.mark.parametrize(
        ("restr", "check"),
        [
            (CircuitBreakerRestrictions(tier=2), "circuit_breaker"),
            (CircuitBreakerRestrictions(daily_halt=True), "daily_loss_halt"),
            (CircuitBreakerRestrictions(consecutive_loss_pause=True), "consecutive_loss_pause"),
        ],
    )
    def test_breaker_restrictions(self, restr: CircuitBreakerRestrictions, check: str) -> None:
        assert _check(_order("0.01"), _state(restrictions=restr))[1] == check

    def test_spot_short_open_rejected(self) -> None:
        assert _check(_order("0.01", Side.SELL), _state())[1] == "spot_short"

    def test_position_size_uses_sleeve(self) -> None:
        state = _state(sleeve_equity=Decimal("2000"))
        ok, check, _ = _check(_order("0.004"), state)
        assert (ok, check) == (False, "position_size")
        assert _check(_order("0.003"), state)[0] is True

    def test_tier1_halves_the_position_cap(self) -> None:
        tier1 = _state(
            restrictions=CircuitBreakerRestrictions(tier=1, position_size_multiplier=Decimal("0.5"))
        )
        assert _check(_order("0.015"), _state())[0] is True
        ok, check, reason = _check(_order("0.015"), tier1)
        assert (ok, check) == (False, "position_size")
        assert "(500.00" in reason
        assert _check(_order("0.008"), tier1)[0] is True

    def test_risk_per_trade_only_with_stop(self) -> None:
        cfg = {"max_risk_per_trade": Decimal("0.02")}
        assert _check(_order("0.01"), _state(), **cfg)[0] is True
        stop = _order("0.01", stop_price=Decimal("30000"))
        ok, check, reason = _check(stop, _state(), **cfg)
        assert (ok, check) == (False, "risk_per_trade")
        assert "300" in reason

    def test_gross_exposure_across_sessions(self) -> None:
        others = [_long("0.01"), _long("0.01"), _long("0.01")]
        state = _state(account_positions=others)
        ok, check, _ = _check(_order("0.01"), state, max_portfolio_heat=Decimal("0.20"))
        assert (ok, check) == (False, "portfolio_heat")
        assert _check(_order("0.01"), state, max_portfolio_heat=Decimal("0.30"))[0] is True

    def test_concurrent_positions_counted_across_sessions(self) -> None:
        others = [_long("0.001", symbol=s) for s in ("ETHUSDT", "SOLUSDT")]
        state = _state(account_positions=others)
        assert _check(_order("0.01"), state, max_concurrent_positions=2)[1] == (
            "concurrent_positions"
        )

    def test_market_rules_min_notional(self) -> None:
        rules = MarketRules(
            symbol="BTCUSDT",
            amount_step=Decimal("0.00001"),
            min_qty=Decimal("0.00001"),
            min_notional=Decimal("5"),
        )
        ok, check, reason = _check(_order("0.00005"), _state(market_rules={"BTCUSDT": rules}))
        assert (ok, check) == (False, "market_rules")
        assert "min notional" in reason

    def test_liquidity_skipped_without_volume(self) -> None:
        assert _check(_order("0.01"), _state(average_volume=None))[0] is True
        assert _check(_order("0.01"), _state(average_volume=Decimal("0.5")))[1] == "liquidity"

    def test_rate_limit(self) -> None:
        state = _state(opening_orders_last_minute=6)
        assert _check(_order("0.01"), state, max_orders_per_minute=6)[1] == "rate_limit"


class TestFuturesMargin:
    def _fut(self, qty: str) -> OrderRequest:
        return OrderRequest(
            symbol=Symbol("BTCUSDT"),
            side=Side.SELL,
            order_type=OrderType.MARKET,
            quantity=Decimal(qty),
            strategy_id="s",
            exchange_id="binanceusdm",
            market_type=MarketType.FUTURES,
        )

    def test_the_balance_check_pays_from_the_spendable_wallet(self) -> None:
        # an open short inflates get_balance by margin + notional; margin posts from the
        # wallet, so a passing check must mean payable
        state = _state(
            balances={"USDT": Decimal("700")},
            spendable_balance=Decimal("100"),
            leverage=1,
        )
        ok, check, reason = _check(self._fut("0.01"), state)
        assert (ok, check) == (False, "balance")
        assert "have 100" in reason

    def test_a_futures_order_within_the_wallet_passes(self) -> None:
        state = _state(
            balances={"USDT": Decimal("700")},
            spendable_balance=Decimal("650"),
            leverage=1,
        )
        assert _check(self._fut("0.01"), state) == (True, "all", "All checks approved")

    def test_a_state_without_a_spendable_read_falls_back_to_balances(self) -> None:
        state = _state(balances={"USDT": Decimal("10")}, leverage=1)
        ok, check, reason = _check(self._fut("0.01"), state)
        assert (ok, check) == (False, "balance")
        assert "have 10" in reason


class TestLegacyPath:
    async def test_constructor_tier_blocks_when_no_restrictions(self) -> None:
        mgr = PreTradeRiskManager(circuit_breaker_tier=2)
        state = _state(restrictions=None)
        result = await mgr.check_order(_order("0.001"), state)
        assert result.approved is False
        assert "circuit breaker" in result.reason
