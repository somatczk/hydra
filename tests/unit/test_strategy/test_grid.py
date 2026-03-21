"""Unit tests for GridStrategy."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from hydra.core.events import BarEvent, OrderFillEvent
from hydra.core.types import OHLCV, OrderStatus, OrderType, Side, Symbol, Timeframe
from hydra.strategy.base import PlaceOrder
from hydra.strategy.builtin.grid import (
    GridStrategy,
    compute_arithmetic_levels,
    compute_geometric_levels,
)
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(
    upper: float = 100_000,
    lower: float = 60_000,
    grid_count: int = 10,
    total_investment: float = 5_000,
    grid_type: str = "arithmetic",
) -> StrategyConfig:
    return StrategyConfig(
        id="grid_test",
        name="Grid Test",
        strategy_class="hydra.strategy.builtin.grid.GridStrategy",
        symbols=["BTCUSDT"],
        parameters={
            "upper_price": upper,
            "lower_price": lower,
            "grid_count": grid_count,
            "total_investment": total_investment,
            "grid_type": grid_type,
        },
    )


def _make_strategy(
    current_price: float = 80_000,
    **config_kwargs,
) -> GridStrategy:
    config = _make_config(**config_kwargs)
    ctx = StrategyContext()
    # Seed the context with one bar so on_start can read a current price
    bar = OHLCV(
        timestamp=datetime.now(UTC),
        open=Decimal(str(current_price)),
        high=Decimal(str(current_price)),
        low=Decimal(str(current_price)),
        close=Decimal(str(current_price)),
        volume=Decimal("1"),
    )
    ctx.add_bar("BTCUSDT", Timeframe.H1, bar)
    return GridStrategy(config=config, context=ctx)


def _fill(
    order_id: str,
    side: Side,
    price: float,
    quantity: float = 0.001,
) -> OrderFillEvent:
    return OrderFillEvent(
        order_id=order_id,
        symbol=Symbol("BTCUSDT"),
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Decimal(str(quantity)),
        price=Decimal(str(price)),
        fee=Decimal("0"),
        fee_currency="USDT",
        status=OrderStatus.FILLED,
    )


# ---------------------------------------------------------------------------
# Grid level computation
# ---------------------------------------------------------------------------


class TestArithmeticLevels:
    def test_count_is_grid_count_plus_one(self):
        levels = compute_arithmetic_levels(Decimal("60000"), Decimal("100000"), grid_count=10)
        assert len(levels) == 11

    def test_first_and_last_levels(self):
        levels = compute_arithmetic_levels(Decimal("60000"), Decimal("100000"), grid_count=10)
        assert levels[0] == Decimal("60000")
        assert levels[-1] == Decimal("100000")

    def test_equal_spacing(self):
        levels = compute_arithmetic_levels(Decimal("60000"), Decimal("100000"), grid_count=4)
        # Expected: 60000, 70000, 80000, 90000, 100000
        expected_step = Decimal("10000")
        for i in range(1, len(levels)):
            assert levels[i] - levels[i - 1] == expected_step

    def test_small_grid(self):
        levels = compute_arithmetic_levels(Decimal("100"), Decimal("200"), grid_count=2)
        assert levels == [Decimal("100"), Decimal("150"), Decimal("200")]


class TestGeometricLevels:
    def test_count_is_grid_count_plus_one(self):
        levels = compute_geometric_levels(Decimal("60000"), Decimal("100000"), grid_count=10)
        assert len(levels) == 11

    def test_first_and_last_levels(self):
        levels = compute_geometric_levels(Decimal("60000"), Decimal("100000"), grid_count=10)
        assert levels[0] == Decimal("60000")
        # Last level may have floating-point deviation; check close enough
        assert abs(float(levels[-1]) - 100_000) < 1e-4

    def test_equal_percentage_spacing(self):
        """Each step should multiply by the same ratio."""
        levels = compute_geometric_levels(Decimal("1000"), Decimal("2000"), grid_count=4)
        ratios = [float(levels[i + 1]) / float(levels[i]) for i in range(len(levels) - 1)]
        # All ratios should be equal
        assert all(abs(r - ratios[0]) < 1e-9 for r in ratios)

    def test_geometric_vs_arithmetic_distribution(self):
        """Geometric levels should have smaller lower steps than arithmetic."""
        arith = compute_arithmetic_levels(Decimal("1000"), Decimal("4000"), 3)
        geo = compute_geometric_levels(Decimal("1000"), Decimal("4000"), 3)

        lower_arith_step = float(arith[1]) - float(arith[0])
        lower_geo_step = float(geo[1]) - float(geo[0])

        # Geometric places levels closer at the bottom of the range
        assert lower_geo_step < lower_arith_step


# ---------------------------------------------------------------------------
# Initial order placement
# ---------------------------------------------------------------------------


class TestOnStart:
    @pytest.mark.asyncio
    async def test_initial_orders_placed(self):
        strategy = _make_strategy(current_price=80_000)
        actions = await strategy.on_start()

        assert len(actions) > 0
        assert all(isinstance(a, PlaceOrder) for a in actions)

    @pytest.mark.asyncio
    async def test_buys_below_current_price(self):
        strategy = _make_strategy(current_price=80_000)
        actions = await strategy.on_start()

        buys = [a for a in actions if isinstance(a, PlaceOrder) and a.side == Side.BUY]
        assert all(a.price < Decimal("80000") for a in buys), (
            "All buy orders must be below current price"
        )

    @pytest.mark.asyncio
    async def test_sells_above_current_price(self):
        strategy = _make_strategy(current_price=80_000)
        actions = await strategy.on_start()

        sells = [a for a in actions if isinstance(a, PlaceOrder) and a.side == Side.SELL]
        assert all(a.price > Decimal("80000") for a in sells), (
            "All sell orders must be above current price"
        )

    @pytest.mark.asyncio
    async def test_no_orders_at_current_price_level(self):
        """If current price coincides exactly with a grid level, skip it."""
        # With 10 intervals from 60k to 100k, levels are at multiples of 4000.
        # Place current price at level 5: 60000 + 5*4000 = 80000
        strategy = _make_strategy(current_price=80_000, grid_count=10)
        actions = await strategy.on_start()

        prices = [a.price for a in actions if isinstance(a, PlaceOrder)]
        assert Decimal("80000") not in prices

    @pytest.mark.asyncio
    async def test_initialized_flag_set(self):
        strategy = _make_strategy(current_price=80_000)
        assert not strategy._initialized
        await strategy.on_start()
        assert strategy._initialized

    @pytest.mark.asyncio
    async def test_order_tags_use_level_index(self):
        strategy = _make_strategy(current_price=80_000)
        actions = await strategy.on_start()

        for action in actions:
            assert isinstance(action, PlaceOrder)
            # Tags must match 'grid_buy_N' or 'grid_sell_N'
            assert action.tag.startswith("grid_buy_") or action.tag.startswith("grid_sell_")
            # The suffix must be a valid integer
            suffix = action.tag.rsplit("_", 1)[-1]
            assert suffix.isdigit()

    @pytest.mark.asyncio
    async def test_price_below_lower_bound_warns(self, caplog):
        """When current price is below the grid, a warning is logged."""
        import logging

        with caplog.at_level(logging.WARNING):
            strategy = _make_strategy(current_price=50_000)
            await strategy.on_start()

        assert any("below lower bound" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_price_above_upper_bound_warns(self, caplog):
        """When current price is above the grid, a warning is logged."""
        import logging

        with caplog.at_level(logging.WARNING):
            strategy = _make_strategy(current_price=110_000)
            await strategy.on_start()

        assert any("above upper bound" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_geometric_grid_initial_placement(self):
        strategy = _make_strategy(current_price=80_000, grid_type="geometric")
        actions = await strategy.on_start()

        buys = [a for a in actions if isinstance(a, PlaceOrder) and a.side == Side.BUY]
        sells = [a for a in actions if isinstance(a, PlaceOrder) and a.side == Side.SELL]

        assert len(buys) > 0
        assert len(sells) > 0
        assert all(a.price < Decimal("80000") for a in buys)
        assert all(a.price > Decimal("80000") for a in sells)


# ---------------------------------------------------------------------------
# on_bar — boundary monitoring
# ---------------------------------------------------------------------------


class TestOnBar:
    def _make_bar(self, price: float) -> BarEvent:
        ohlcv = OHLCV(
            timestamp=datetime.now(UTC),
            open=Decimal(str(price)),
            high=Decimal(str(price)),
            low=Decimal(str(price)),
            close=Decimal(str(price)),
            volume=Decimal("1"),
        )
        return BarEvent(symbol=Symbol("BTCUSDT"), timeframe=Timeframe.H1, ohlcv=ohlcv)

    @pytest.mark.asyncio
    async def test_on_bar_returns_no_orders_within_range(self):
        strategy = _make_strategy(current_price=80_000)
        await strategy.on_start()

        bar = self._make_bar(75_000)
        actions = await strategy.on_bar(bar)
        assert actions == []

    @pytest.mark.asyncio
    async def test_on_bar_returns_empty_before_initialized(self):
        strategy = _make_strategy(current_price=80_000)
        # Do NOT call on_start
        bar = self._make_bar(80_000)
        actions = await strategy.on_bar(bar)
        assert actions == []

    @pytest.mark.asyncio
    async def test_on_bar_warns_below_lower_bound(self, caplog):
        import logging

        strategy = _make_strategy(current_price=80_000)
        await strategy.on_start()

        with caplog.at_level(logging.WARNING):
            bar = self._make_bar(50_000)
            await strategy.on_bar(bar)

        assert any("broke below lower" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_on_bar_warns_above_upper_bound(self, caplog):
        import logging

        strategy = _make_strategy(current_price=80_000)
        await strategy.on_start()

        with caplog.at_level(logging.WARNING):
            bar = self._make_bar(110_000)
            await strategy.on_bar(bar)

        assert any("broke above upper" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# on_fill — buy fill → sell placed
# ---------------------------------------------------------------------------


class TestOnFillBuy:
    @pytest.mark.asyncio
    async def test_buy_fill_places_sell_one_level_up(self):
        """When a tracked buy at level N fills, a sell at level N+1 is placed."""
        strategy = _make_strategy(current_price=80_000, grid_count=10)
        await strategy.on_start()

        # Find the highest buy level (level just below 80000) and register it
        levels = strategy._grid_levels
        # Levels below 80000 are at indices 0..4 (60k,64k,68k,72k,76k for 10-step grid)
        buy_idx = 4  # 76000
        strategy.register_order(buy_idx, "BUY", "order-buy-4")

        fill = _fill("order-buy-4", Side.BUY, price=float(levels[buy_idx]))
        fill = OrderFillEvent(
            order_id="order-buy-4",
            symbol=Symbol("BTCUSDT"),
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=strategy._quantity_per_grid,
            price=levels[buy_idx],
            fee=Decimal("0"),
            fee_currency="USDT",
            status=OrderStatus.FILLED,
        )
        actions = await strategy.on_fill(fill)

        assert len(actions) == 1
        action = actions[0]
        assert isinstance(action, PlaceOrder)
        assert action.side == Side.SELL
        assert action.price == levels[buy_idx + 1]
        assert action.tag == f"grid_sell_{buy_idx + 1}"

    @pytest.mark.asyncio
    async def test_buy_fill_at_top_level_places_no_sell(self, caplog):
        """Buy at the top level cannot spawn a sell — no action, warning logged."""
        import logging

        strategy = _make_strategy(current_price=80_000, grid_count=10)
        await strategy.on_start()

        levels = strategy._grid_levels
        top_idx = len(levels) - 1
        strategy.register_order(top_idx, "BUY", "order-buy-top")

        fill = OrderFillEvent(
            order_id="order-buy-top",
            symbol=Symbol("BTCUSDT"),
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=strategy._quantity_per_grid,
            price=levels[top_idx],
            fee=Decimal("0"),
            fee_currency="USDT",
            status=OrderStatus.FILLED,
        )
        with caplog.at_level(logging.WARNING):
            actions = await strategy.on_fill(fill)

        assert actions == []
        assert any("top level" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_buy_fill_stores_pending_sell_buy_price(self):
        """After a buy fill the buy price must be tracked for P&L calculation."""
        strategy = _make_strategy(current_price=80_000, grid_count=10)
        await strategy.on_start()

        levels = strategy._grid_levels
        buy_idx = 3
        strategy.register_order(buy_idx, "BUY", "order-buy-3")

        fill = OrderFillEvent(
            order_id="order-buy-3",
            symbol=Symbol("BTCUSDT"),
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=strategy._quantity_per_grid,
            price=levels[buy_idx],
            fee=Decimal("0"),
            fee_currency="USDT",
            status=OrderStatus.FILLED,
        )
        await strategy.on_fill(fill)

        sell_idx = buy_idx + 1
        assert sell_idx in strategy._pending_sell_buy_price
        assert strategy._pending_sell_buy_price[sell_idx] == levels[buy_idx]


# ---------------------------------------------------------------------------
# on_fill — sell fill → buy placed + profit recorded
# ---------------------------------------------------------------------------


class TestOnFillSell:
    @pytest.mark.asyncio
    async def test_sell_fill_places_buy_one_level_down(self):
        """When a tracked sell at level N fills, a buy at level N-1 is placed."""
        strategy = _make_strategy(current_price=80_000, grid_count=10)
        await strategy.on_start()

        levels = strategy._grid_levels
        sell_idx = 6  # first level above 80000 in a 10-step grid
        strategy.register_order(sell_idx, "SELL", "order-sell-6")

        fill = OrderFillEvent(
            order_id="order-sell-6",
            symbol=Symbol("BTCUSDT"),
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            quantity=strategy._quantity_per_grid,
            price=levels[sell_idx],
            fee=Decimal("0"),
            fee_currency="USDT",
            status=OrderStatus.FILLED,
        )
        actions = await strategy.on_fill(fill)

        assert len(actions) == 1
        action = actions[0]
        assert isinstance(action, PlaceOrder)
        assert action.side == Side.BUY
        assert action.price == levels[sell_idx - 1]
        assert action.tag == f"grid_buy_{sell_idx - 1}"

    @pytest.mark.asyncio
    async def test_sell_fill_at_bottom_level_places_no_buy(self, caplog):
        """Sell at the lowest level cannot spawn a buy — no action, warning logged."""
        import logging

        strategy = _make_strategy(current_price=80_000, grid_count=10)
        await strategy.on_start()

        levels = strategy._grid_levels
        strategy.register_order(0, "SELL", "order-sell-0")

        fill = OrderFillEvent(
            order_id="order-sell-0",
            symbol=Symbol("BTCUSDT"),
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            quantity=strategy._quantity_per_grid,
            price=levels[0],
            fee=Decimal("0"),
            fee_currency="USDT",
            status=OrderStatus.FILLED,
        )
        with caplog.at_level(logging.WARNING):
            actions = await strategy.on_fill(fill)

        assert actions == []
        assert any("bottom level" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_sell_fill_does_not_record_profit_without_prior_buy(self):
        """A sell fill that has no matching buy fill should not change profit."""
        strategy = _make_strategy(current_price=80_000, grid_count=10)
        await strategy.on_start()

        levels = strategy._grid_levels
        sell_idx = 6
        strategy.register_order(sell_idx, "SELL", "order-sell-6")

        fill = OrderFillEvent(
            order_id="order-sell-6",
            symbol=Symbol("BTCUSDT"),
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            quantity=strategy._quantity_per_grid,
            price=levels[sell_idx],
            fee=Decimal("0"),
            fee_currency="USDT",
            status=OrderStatus.FILLED,
        )
        await strategy.on_fill(fill)

        assert strategy.grid_profit == Decimal("0")
        assert strategy.cycles_completed == 0


# ---------------------------------------------------------------------------
# Full buy→sell cycle: profit tracking
# ---------------------------------------------------------------------------


class TestProfitTracking:
    @pytest.mark.asyncio
    async def test_complete_cycle_records_profit(self):
        """Simulate buy-fill then sell-fill and verify profit is captured."""
        strategy = _make_strategy(current_price=80_000, grid_count=10)
        await strategy.on_start()

        levels = strategy._grid_levels
        buy_idx = 4
        sell_idx = buy_idx + 1
        qty = strategy._quantity_per_grid

        # Step 1: buy fill at level 4
        strategy.register_order(buy_idx, "BUY", "order-buy-4")
        buy_fill = OrderFillEvent(
            order_id="order-buy-4",
            symbol=Symbol("BTCUSDT"),
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=qty,
            price=levels[buy_idx],
            fee=Decimal("0"),
            fee_currency="USDT",
            status=OrderStatus.FILLED,
        )
        sell_actions = await strategy.on_fill(buy_fill)

        # The sell order placed by on_fill — register it
        assert len(sell_actions) == 1
        strategy.register_order(sell_idx, "SELL", "order-sell-5")

        # Step 2: sell fill at level 5
        sell_fill = OrderFillEvent(
            order_id="order-sell-5",
            symbol=Symbol("BTCUSDT"),
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            quantity=qty,
            price=levels[sell_idx],
            fee=Decimal("0"),
            fee_currency="USDT",
            status=OrderStatus.FILLED,
        )
        await strategy.on_fill(sell_fill)

        expected_profit = (levels[sell_idx] - levels[buy_idx]) * qty
        assert strategy.grid_profit == expected_profit
        assert strategy.cycles_completed == 1

    @pytest.mark.asyncio
    async def test_multiple_cycles_accumulate_profit(self):
        strategy = _make_strategy(current_price=80_000, grid_count=10)
        await strategy.on_start()

        levels = strategy._grid_levels
        qty = strategy._quantity_per_grid
        total_profit = Decimal("0")

        for buy_idx in [2, 3]:
            sell_idx = buy_idx + 1

            strategy.register_order(buy_idx, "BUY", f"buy-{buy_idx}")
            buy_fill = OrderFillEvent(
                order_id=f"buy-{buy_idx}",
                symbol=Symbol("BTCUSDT"),
                side=Side.BUY,
                order_type=OrderType.LIMIT,
                quantity=qty,
                price=levels[buy_idx],
                fee=Decimal("0"),
                fee_currency="USDT",
                status=OrderStatus.FILLED,
            )
            await strategy.on_fill(buy_fill)

            strategy.register_order(sell_idx, "SELL", f"sell-{sell_idx}")
            sell_fill = OrderFillEvent(
                order_id=f"sell-{sell_idx}",
                symbol=Symbol("BTCUSDT"),
                side=Side.SELL,
                order_type=OrderType.LIMIT,
                quantity=qty,
                price=levels[sell_idx],
                fee=Decimal("0"),
                fee_currency="USDT",
                status=OrderStatus.FILLED,
            )
            await strategy.on_fill(sell_fill)

            total_profit += (levels[sell_idx] - levels[buy_idx]) * qty

        assert strategy.grid_profit == total_profit
        assert strategy.cycles_completed == 2

    @pytest.mark.asyncio
    async def test_profit_is_positive_for_arithmetic_grid(self):
        """In a standard arithmetic grid the step is constant and profit > 0."""
        strategy = _make_strategy(current_price=80_000, grid_count=10)
        await strategy.on_start()

        levels = strategy._grid_levels
        qty = strategy._quantity_per_grid
        buy_idx = 3
        sell_idx = buy_idx + 1

        strategy.register_order(buy_idx, "BUY", "b")
        await strategy.on_fill(
            OrderFillEvent(
                order_id="b",
                symbol=Symbol("BTCUSDT"),
                side=Side.BUY,
                order_type=OrderType.LIMIT,
                quantity=qty,
                price=levels[buy_idx],
                fee=Decimal("0"),
                fee_currency="USDT",
                status=OrderStatus.FILLED,
            )
        )

        strategy.register_order(sell_idx, "SELL", "s")
        await strategy.on_fill(
            OrderFillEvent(
                order_id="s",
                symbol=Symbol("BTCUSDT"),
                side=Side.SELL,
                order_type=OrderType.LIMIT,
                quantity=qty,
                price=levels[sell_idx],
                fee=Decimal("0"),
                fee_currency="USDT",
                status=OrderStatus.FILLED,
            )
        )

        assert strategy.grid_profit > Decimal("0")


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_unrecognised_fill_returns_empty(self):
        strategy = _make_strategy(current_price=80_000)
        await strategy.on_start()

        unknown_fill = OrderFillEvent(
            order_id="totally-unknown",
            symbol=Symbol("BTCUSDT"),
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("0.001"),
            price=Decimal("78000"),
            fee=Decimal("0"),
            fee_currency="USDT",
            status=OrderStatus.FILLED,
        )
        actions = await strategy.on_fill(unknown_fill)
        assert actions == []

    @pytest.mark.asyncio
    async def test_on_fill_before_initialized_returns_empty(self):
        strategy = _make_strategy(current_price=80_000)
        # Do NOT call on_start
        fill = _fill("x", Side.BUY, 76_000)
        actions = await strategy.on_fill(fill)
        assert actions == []

    def test_invalid_grid_count_raises(self):
        config = _make_config(grid_count=1)
        ctx = StrategyContext()
        strategy = GridStrategy(config=config, context=ctx)
        with pytest.raises(ValueError, match="grid_count"):
            strategy._parse_params()

    def test_inverted_bounds_raises(self):
        config = _make_config(upper=60_000, lower=100_000)
        ctx = StrategyContext()
        strategy = GridStrategy(config=config, context=ctx)
        with pytest.raises(ValueError, match="upper_price"):
            strategy._parse_params()

    def test_zero_investment_raises(self):
        config = _make_config(total_investment=0)
        ctx = StrategyContext()
        strategy = GridStrategy(config=config, context=ctx)
        with pytest.raises(ValueError, match="total_investment"):
            strategy._parse_params()

    def test_required_history_is_zero(self):
        strategy = _make_strategy()
        assert strategy.required_history == 0

    @pytest.mark.asyncio
    async def test_quantity_per_grid_is_positive(self):
        strategy = _make_strategy()
        await strategy.on_start()
        assert strategy._quantity_per_grid > Decimal("0")

    @pytest.mark.asyncio
    async def test_on_stop_returns_empty_list(self):
        strategy = _make_strategy()
        await strategy.on_start()
        actions = await strategy.on_stop()
        assert actions == []

    @pytest.mark.asyncio
    async def test_tag_based_recovery_buy(self):
        """Fill with unknown order_id but recognisable tag is handled via tag recovery."""
        strategy = _make_strategy(current_price=80_000, grid_count=10)
        await strategy.on_start()

        levels = strategy._grid_levels
        buy_idx = 2

        fill = OrderFillEvent(
            order_id="unknown-order-id",
            symbol=Symbol("BTCUSDT"),
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=strategy._quantity_per_grid,
            price=levels[buy_idx],
            fee=Decimal("0"),
            fee_currency="USDT",
            status=OrderStatus.FILLED,
        )
        # Call the internal tag-based recovery directly
        actions = await strategy._handle_fill_by_tag(f"grid_buy_{buy_idx}", fill, "BTCUSDT")

        assert len(actions) == 1
        assert actions[0].side == Side.SELL
        assert actions[0].price == levels[buy_idx + 1]

    @pytest.mark.asyncio
    async def test_tag_based_recovery_sell_records_profit(self):
        """Tag-based sell recovery computes profit when buy price is known."""
        strategy = _make_strategy(current_price=80_000, grid_count=10)
        await strategy.on_start()

        levels = strategy._grid_levels
        buy_idx = 3
        sell_idx = buy_idx + 1
        buy_price = levels[buy_idx]
        sell_price = levels[sell_idx]
        qty = strategy._quantity_per_grid

        # Seed the pending dict (as if the buy fill already happened)
        strategy._pending_sell_buy_price[sell_idx] = buy_price

        fill = OrderFillEvent(
            order_id="unknown-sell-id",
            symbol=Symbol("BTCUSDT"),
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            quantity=qty,
            price=sell_price,
            fee=Decimal("0"),
            fee_currency="USDT",
            status=OrderStatus.FILLED,
        )
        await strategy._handle_fill_by_tag(f"grid_sell_{sell_idx}", fill, "BTCUSDT")

        expected = (sell_price - buy_price) * qty
        assert strategy.grid_profit == expected
        assert strategy.cycles_completed == 1
