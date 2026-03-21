"""Tests for DCAStrategy.

Covers:
- Full deal lifecycle (start → base fill → safety fills → TP fill)
- Volume scaling per safety order
- Max safety order enforcement
- TP recalculation after safety order fills
- Immediate vs RSI oversold start condition
- Edge cases (no ohlcv, stop cleanup, unknown start condition)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hydra.core.events import BarEvent, OrderFillEvent
from hydra.core.types import OHLCV, OrderStatus, OrderType, Side, Symbol, Timeframe
from hydra.strategy.base import CancelOrder, PlaceOrder
from hydra.strategy.builtin.dca import _TAG_BASE, _TAG_TP, DCAStrategy, _DealState
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext

# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _make_config(**param_overrides: object) -> StrategyConfig:
    params = {
        "base_order_size": 100,
        "safety_order_size": 50,
        "safety_order_count": 3,
        "price_deviation_pct": 1.0,
        "volume_scale": 1.5,
        "step_scale": 1.0,
        "take_profit_pct": 2.0,
        "start_condition": "immediate",
    }
    params.update(param_overrides)
    return StrategyConfig(
        id="dca_test",
        name="DCA Test",
        strategy_class="hydra.strategy.builtin.dca.DCAStrategy",
        symbols=["BTCUSDT"],
        parameters=params,
    )


def _make_strategy(**param_overrides: object) -> DCAStrategy:
    config = _make_config(**param_overrides)
    context = StrategyContext()
    return DCAStrategy(config=config, context=context)


def _make_bar(price: float, symbol: str = "BTCUSDT") -> BarEvent:
    p = Decimal(str(price))
    return BarEvent(
        symbol=Symbol(symbol),
        timeframe=Timeframe.H1,
        ohlcv=OHLCV(
            open=p,
            high=p * Decimal("1.001"),
            low=p * Decimal("0.999"),
            close=p,
            volume=Decimal("10"),
            timestamp=datetime.now(UTC),
        ),
    )


def _make_fill(
    order_id: str,
    side: str,
    quantity: float,
    price: float,
    symbol: str = "BTCUSDT",
) -> OrderFillEvent:
    return OrderFillEvent(
        order_id=order_id,
        symbol=Symbol(symbol),
        side=Side(side),
        order_type=OrderType.MARKET,
        quantity=Decimal(str(quantity)),
        price=Decimal(str(price)),
        fee=Decimal("0"),
        fee_currency="USDT",
        exchange_id="binance",
        status=OrderStatus.FILLED,
    )


# ---------------------------------------------------------------------------
# Tests: start condition
# ---------------------------------------------------------------------------


class TestStartCondition:
    async def test_immediate_start_places_base_order_on_first_bar(self) -> None:
        strategy = _make_strategy(start_condition="immediate")
        bar = _make_bar(50000)
        actions = await strategy.on_bar(bar)

        assert len(actions) == 1
        assert isinstance(actions[0], PlaceOrder)
        assert actions[0].tag == _TAG_BASE
        assert actions[0].side == Side.BUY
        assert actions[0].order_type == OrderType.MARKET
        # quantity = base_order_size / price = 100 / 50000
        expected_qty = Decimal("100") / Decimal("50000")
        assert actions[0].quantity == expected_qty

    async def test_immediate_start_does_not_repeat_after_first_bar(self) -> None:
        strategy = _make_strategy(start_condition="immediate")
        bar = _make_bar(50000)
        await strategy.on_bar(bar)
        # Second bar — deal is now ACTIVE, not IDLE
        actions = await strategy.on_bar(bar)
        # No base order should be placed again
        base_orders = [a for a in actions if isinstance(a, PlaceOrder) and a.tag == _TAG_BASE]
        assert base_orders == []

    async def test_no_ohlcv_returns_empty(self) -> None:
        strategy = _make_strategy(start_condition="immediate")
        bar = BarEvent(symbol=Symbol("BTCUSDT"), timeframe=Timeframe.H1, ohlcv=None)
        actions = await strategy.on_bar(bar)
        assert actions == []

    async def test_rsi_oversold_does_not_start_immediately(self) -> None:
        """RSI condition should not trigger on insufficient bar history."""
        strategy = _make_strategy(start_condition="rsi_oversold", rsi_period=14)
        # Only 5 bars in context — RSI needs 15
        ctx = strategy._context
        for i in range(5):
            ctx.add_bar(
                "BTCUSDT",
                Timeframe.H1,
                OHLCV(
                    open=Decimal("50000"),
                    high=Decimal("50500"),
                    low=Decimal("49500"),
                    close=Decimal("50000"),
                    volume=Decimal("10"),
                    timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=i),
                ),
            )
        bar = _make_bar(50000)
        actions = await strategy.on_bar(bar)
        assert actions == []

    async def test_rsi_oversold_triggers_when_rsi_is_low(self) -> None:
        """RSI condition should start a deal when RSI drops below oversold threshold."""
        strategy = _make_strategy(start_condition="rsi_oversold", rsi_period=14, rsi_oversold=30)
        ctx = strategy._context
        # Build a strong downtrend to get RSI < 30: start high then drop sharply
        prices = [50000.0] * 14 + [40000.0] * 14  # sharp 20% drop
        for i, p in enumerate(prices):
            ctx.add_bar(
                "BTCUSDT",
                Timeframe.H1,
                OHLCV(
                    open=Decimal(str(p)),
                    high=Decimal(str(p * 1.001)),
                    low=Decimal(str(p * 0.999)),
                    close=Decimal(str(p)),
                    volume=Decimal("10"),
                    timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=i),
                ),
            )
        bar = _make_bar(40000)
        actions = await strategy.on_bar(bar)
        # After a severe downtrend, RSI should be oversold → deal starts
        base_orders = [a for a in actions if isinstance(a, PlaceOrder) and a.tag == _TAG_BASE]
        assert len(base_orders) == 1

    async def test_unknown_start_condition_does_not_start(self) -> None:
        strategy = _make_strategy(start_condition="magic_condition")
        bar = _make_bar(50000)
        actions = await strategy.on_bar(bar)
        assert actions == []


# ---------------------------------------------------------------------------
# Tests: deal lifecycle
# ---------------------------------------------------------------------------


class TestDealLifecycle:
    async def test_full_lifecycle_no_safety_orders(self) -> None:
        """Base order → immediate TP hit (price rises on next bar) → IDLE."""
        strategy = _make_strategy(
            base_order_size=100,
            safety_order_count=0,
            take_profit_pct=2.0,
            start_condition="immediate",
        )

        # Bar 1: start deal
        bar_start = _make_bar(50000)
        actions = await strategy.on_bar(bar_start)
        assert len(actions) == 1
        assert actions[0].tag == _TAG_BASE

        # Simulate base order fill at 50000
        fill_base = _make_fill(_TAG_BASE, "BUY", 100 / 50000, 50000)
        fill_actions = await strategy.on_fill(fill_base)
        # Should place a TP limit order
        tp_orders = [a for a in fill_actions if isinstance(a, PlaceOrder) and a.tag == _TAG_TP]
        assert len(tp_orders) == 1
        tp = tp_orders[0]
        assert tp.side == Side.SELL
        assert tp.order_type == OrderType.LIMIT
        # TP price = 50000 * 1.02 = 51000
        assert tp.price == Decimal("50000") * Decimal("1.02")

        # Record TP order ID
        strategy._pending_tp_id = "tp_order_1"

        # Bar 2: price jumps above TP → should issue SELL market order
        bar_tp = _make_bar(51500)
        actions2 = await strategy.on_bar(bar_tp)
        sell_orders = [a for a in actions2 if isinstance(a, PlaceOrder) and a.side == Side.SELL]
        assert len(sell_orders) >= 1
        # Should be a market close
        market_sells = [a for a in sell_orders if a.order_type == OrderType.MARKET]
        assert len(market_sells) == 1

    async def test_tp_fill_resets_to_idle(self) -> None:
        strategy = _make_strategy(start_condition="immediate", safety_order_count=0)
        bar = _make_bar(50000)
        await strategy.on_bar(bar)

        fill_base = _make_fill(_TAG_BASE, "BUY", 0.002, 50000)
        await strategy.on_fill(fill_base)

        # Simulate TP fill
        fill_tp = _make_fill(_TAG_TP, "SELL", 0.002, 51000)
        await strategy.on_fill(fill_tp)

        # Strategy should be back to IDLE
        assert strategy._deal_state == _DealState.IDLE
        assert strategy._base_order_filled is False
        assert strategy._total_quantity == Decimal("0")
        assert strategy._avg_entry_price == Decimal("0")

    async def test_new_deal_starts_after_tp(self) -> None:
        """After TP fill, the next bar should start a fresh deal."""
        strategy = _make_strategy(start_condition="immediate", safety_order_count=0)
        bar = _make_bar(50000)
        await strategy.on_bar(bar)
        fill_base = _make_fill(_TAG_BASE, "BUY", 0.002, 50000)
        await strategy.on_fill(fill_base)
        fill_tp = _make_fill(_TAG_TP, "SELL", 0.002, 51000)
        await strategy.on_fill(fill_tp)

        # Now IDLE — next bar should restart
        bar2 = _make_bar(48000)
        actions = await strategy.on_bar(bar2)
        base_orders = [a for a in actions if isinstance(a, PlaceOrder) and a.tag == _TAG_BASE]
        assert len(base_orders) == 1


# ---------------------------------------------------------------------------
# Tests: safety orders
# ---------------------------------------------------------------------------


class TestSafetyOrders:
    async def _setup_active_deal(self, strategy: DCAStrategy, entry_price: float = 50000) -> None:
        """Helper: start a deal and fill the base order."""
        bar = _make_bar(entry_price)
        await strategy.on_bar(bar)
        qty = strategy._base_order_size / Decimal(str(entry_price))
        fill = _make_fill(_TAG_BASE, "BUY", float(qty), entry_price)
        await strategy.on_fill(fill)

    async def test_safety_order_placed_when_price_drops(self) -> None:
        strategy = _make_strategy(
            price_deviation_pct=2.0,  # trigger SO1 at -2%
            safety_order_count=3,
            take_profit_pct=3.0,
        )
        await self._setup_active_deal(strategy, entry_price=50000)
        # Price drops by 2% → first safety should be triggered
        trigger_price = 50000 * 0.98
        bar = _make_bar(trigger_price)
        actions = await strategy.on_bar(bar)

        safety_orders = [a for a in actions if isinstance(a, PlaceOrder) and a.tag == "safety_1"]
        assert len(safety_orders) == 1
        so = safety_orders[0]
        assert so.side == Side.BUY
        assert so.order_type == OrderType.LIMIT

    async def test_safety_order_not_placed_before_deviation(self) -> None:
        strategy = _make_strategy(
            price_deviation_pct=2.0,
            safety_order_count=3,
            take_profit_pct=3.0,
        )
        await self._setup_active_deal(strategy, entry_price=50000)
        # Price only drops by 1% — not enough for 2% deviation
        bar = _make_bar(50000 * 0.99)
        actions = await strategy.on_bar(bar)
        safety_orders = [a for a in actions if isinstance(a, PlaceOrder)]
        assert all(a.tag != "safety_1" for a in safety_orders)

    async def test_safety_order_count_respected(self) -> None:
        """Never place more safety orders than configured."""
        strategy = _make_strategy(
            price_deviation_pct=1.0,
            safety_order_count=2,
            take_profit_pct=5.0,
            volume_scale=1.0,
        )
        await self._setup_active_deal(strategy, entry_price=50000)

        # Trigger SO1
        bar1 = _make_bar(50000 * 0.99)
        await strategy.on_bar(bar1)
        # Simulate SO1 fill
        fill_so1 = _make_fill("safety_1", "BUY", 50 / 49500, 49500)
        strategy._pending_safety_ids["safety_1"] = "safety_1"
        await strategy.on_fill(fill_so1)

        # Trigger SO2
        bar2 = _make_bar(50000 * 0.98)
        await strategy.on_bar(bar2)
        fill_so2 = _make_fill("safety_2", "BUY", 50 / 49000, 49000)
        strategy._pending_safety_ids["safety_2"] = "safety_2"
        await strategy.on_fill(fill_so2)

        # Now at max (2) safety orders. Price drops more — no SO3 should be placed.
        bar3 = _make_bar(50000 * 0.95)
        actions = await strategy.on_bar(bar3)
        safety_3 = [a for a in actions if isinstance(a, PlaceOrder) and a.tag == "safety_3"]
        assert safety_3 == []

    async def test_safety_not_placed_twice(self) -> None:
        """A safety order level must not be placed more than once."""
        strategy = _make_strategy(price_deviation_pct=1.0, safety_order_count=3)
        await self._setup_active_deal(strategy, entry_price=50000)

        # Manually mark SO1 as already placed
        strategy._safety_orders_placed = 1
        strategy._pending_safety_ids["safety_1"] = "existing_so1_id"

        # Trigger at SO1 level — should not place again
        bar = _make_bar(50000 * 0.989)
        actions = await strategy.on_bar(bar)
        new_so1 = [a for a in actions if isinstance(a, PlaceOrder) and a.tag == "safety_1"]
        assert new_so1 == []


# ---------------------------------------------------------------------------
# Tests: volume scaling
# ---------------------------------------------------------------------------


class TestVolumeScaling:
    def test_safety_order_sizes_scale_correctly(self) -> None:
        strategy = _make_strategy(
            safety_order_size=50,
            volume_scale=2.0,
            safety_order_count=4,
        )
        expected_sizes = [
            Decimal("50"),  # SO1: 50 * 2^0
            Decimal("100"),  # SO2: 50 * 2^1
            Decimal("200"),  # SO3: 50 * 2^2
            Decimal("400"),  # SO4: 50 * 2^3
        ]
        for i, expected in enumerate(expected_sizes, start=1):
            actual = strategy._safety_order_size_for(i)
            assert actual == expected, f"SO{i}: expected {expected}, got {actual}"

    def test_volume_scale_1_means_equal_sizes(self) -> None:
        strategy = _make_strategy(safety_order_size=75, volume_scale=1.0, safety_order_count=5)
        for i in range(1, 6):
            assert strategy._safety_order_size_for(i) == Decimal("75")


# ---------------------------------------------------------------------------
# Tests: deviation price calculation
# ---------------------------------------------------------------------------


class TestDeviationPrices:
    def test_equal_step_deviation(self) -> None:
        strategy = _make_strategy(price_deviation_pct=1.0, step_scale=1.0, safety_order_count=3)
        avg = Decimal("50000")
        prices = strategy._compute_deviation_prices(avg)
        # Cumulative: 1%, 2%, 3%
        assert prices[0] == avg * Decimal("0.99")
        assert prices[1] == avg * Decimal("0.98")
        assert prices[2] == avg * Decimal("0.97")

    def test_step_scale_doubles_each_step(self) -> None:
        strategy = _make_strategy(price_deviation_pct=1.0, step_scale=2.0, safety_order_count=3)
        avg = Decimal("10000")
        prices = strategy._compute_deviation_prices(avg)
        # Steps: 1%, 2%, 4% → cumulative: 1%, 3%, 7%
        assert prices[0] == avg * (Decimal("1") - Decimal("0.01"))
        assert prices[1] == avg * (Decimal("1") - Decimal("0.03"))
        assert prices[2] == avg * (Decimal("1") - Decimal("0.07"))


# ---------------------------------------------------------------------------
# Tests: TP recalculation
# ---------------------------------------------------------------------------


class TestTakeProfitRecalculation:
    async def test_tp_price_updates_after_safety_fill(self) -> None:
        """After a safety order fill, TP price must reflect the new avg entry."""
        strategy = _make_strategy(
            base_order_size=100,
            safety_order_size=100,
            price_deviation_pct=2.0,
            volume_scale=1.0,
            take_profit_pct=2.0,
            safety_order_count=3,
        )
        # Start deal
        bar = _make_bar(50000)
        await strategy.on_bar(bar)
        base_qty = Decimal("100") / Decimal("50000")

        # Fill base order at 50000
        fill_base = _make_fill(_TAG_BASE, "BUY", float(base_qty), 50000)
        fill_actions = await strategy.on_fill(fill_base)
        tp_before = next(a for a in fill_actions if isinstance(a, PlaceOrder) and a.tag == _TAG_TP)
        assert tp_before.price == Decimal("50000") * Decimal("1.02")

        # Record TP and simulate safety order at 2% drop (49000)
        strategy._pending_tp_id = "tp_v1"
        so_price = Decimal("49000")
        so_qty = Decimal("100") / so_price
        strategy._pending_safety_ids["safety_1"] = "so1_id"
        fill_so = _make_fill("so1_id", "BUY", float(so_qty), 49000)
        so_actions = await strategy.on_fill(fill_so)

        # Should cancel old TP and place a new one
        cancels = [a for a in so_actions if isinstance(a, CancelOrder)]
        assert any(a.order_id == "tp_v1" for a in cancels)

        new_tps = [a for a in so_actions if isinstance(a, PlaceOrder) and a.tag == _TAG_TP]
        assert len(new_tps) == 1
        new_tp = new_tps[0]

        # Verify the new TP price reflects the lower avg entry
        expected_avg = (base_qty * Decimal("50000") + so_qty * Decimal("49000")) / (
            base_qty + so_qty
        )
        expected_tp_price = expected_avg * Decimal("1.02")
        assert abs(new_tp.price - expected_tp_price) < Decimal("0.01")

    async def test_avg_entry_accumulates_correctly(self) -> None:
        strategy = _make_strategy(
            base_order_size=200,
            safety_order_size=100,
            price_deviation_pct=2.0,
            volume_scale=1.0,
            safety_order_count=3,
        )
        bar = _make_bar(10000)
        await strategy.on_bar(bar)

        # Base fill: 200 USDT at 10000 → 0.02 BTC
        fill_base = _make_fill(_TAG_BASE, "BUY", 0.02, 10000)
        await strategy.on_fill(fill_base)
        assert strategy._total_quantity == Decimal("0.02")
        assert strategy._avg_entry_price == Decimal("10000")

        # Safety fill: 100 USDT at 9800 → 0.0102... BTC
        so_qty = Decimal("100") / Decimal("9800")
        strategy._pending_safety_ids["safety_1"] = "safety_1"
        fill_so = _make_fill("safety_1", "BUY", float(so_qty), 9800)
        await strategy.on_fill(fill_so)

        total_qty = Decimal("0.02") + so_qty
        total_cost = Decimal("200") + so_qty * Decimal("9800")
        expected_avg = total_cost / total_qty
        assert abs(strategy._avg_entry_price - expected_avg) < Decimal("0.001")


# ---------------------------------------------------------------------------
# Tests: on_stop cleanup
# ---------------------------------------------------------------------------


class TestOnStop:
    async def test_on_stop_cancels_pending_orders(self) -> None:
        strategy = _make_strategy()
        bar = _make_bar(50000)
        await strategy.on_bar(bar)
        fill_base = _make_fill(_TAG_BASE, "BUY", 0.002, 50000)
        await strategy.on_fill(fill_base)

        # Manually register pending orders
        strategy._pending_tp_id = "tp_999"
        strategy._pending_safety_ids["safety_1"] = "so_111"

        actions = await strategy.on_stop()

        cancel_ids = {a.order_id for a in actions if isinstance(a, CancelOrder)}
        assert "tp_999" in cancel_ids
        assert "so_111" in cancel_ids

    async def test_on_stop_resets_state(self) -> None:
        strategy = _make_strategy()
        bar = _make_bar(50000)
        await strategy.on_bar(bar)
        await strategy.on_stop()

        assert strategy._deal_state == _DealState.IDLE
        assert strategy._total_quantity == Decimal("0")
        assert strategy._pending_tp_id is None
        assert strategy._pending_safety_ids == {}


# ---------------------------------------------------------------------------
# Tests: required_history
# ---------------------------------------------------------------------------


class TestRequiredHistory:
    def test_immediate_start_requires_1_bar(self) -> None:
        strategy = _make_strategy(start_condition="immediate")
        assert strategy.required_history == 1

    def test_rsi_start_requires_rsi_period_plus_1(self) -> None:
        strategy = _make_strategy(start_condition="rsi_oversold", rsi_period=14)
        assert strategy.required_history == 15

    def test_rsi_custom_period(self) -> None:
        strategy = _make_strategy(start_condition="rsi_oversold", rsi_period=21)
        assert strategy.required_history == 22
