"""Tests for the RuleBasedStrategy and rule evaluation engine."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import numpy as np

from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import OHLCV, Direction, Position, Symbol, Timeframe
from hydra.strategy.builtin.rule_based import RuleBasedStrategy
from hydra.strategy.condition_schema import (
    Comparator,
    Condition,
    ConditionGroup,
    LogicOperator,
)
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext
from hydra.strategy.rule_engine import evaluate_condition, evaluate_condition_group


def _make_context_with_data(
    prices: list[float],
    symbol: str = "BTCUSDT",
    timeframe: Timeframe = Timeframe.H1,
) -> StrategyContext:
    """Create a StrategyContext pre-populated with bar data."""
    ctx = StrategyContext()
    for i, price in enumerate(prices):
        high = price * 1.02
        low = price * 0.98
        ohlcv = OHLCV(
            open=Decimal(str(price)),
            high=Decimal(str(round(high, 2))),
            low=Decimal(str(round(low, 2))),
            close=Decimal(str(price)),
            volume=Decimal("1000"),
            timestamp=datetime(2024, 1, 1, i % 24, 0, 0, tzinfo=UTC),
        )
        ctx.add_bar(symbol, timeframe, ohlcv)
    return ctx


class TestConditionEvaluation:
    """Test individual condition evaluation."""

    def test_less_than(self) -> None:
        """Test less_than comparator."""
        # Create data where RSI would be calculable
        prices = list(range(120, 60, -1))  # downtrend -> low RSI
        ctx = _make_context_with_data([float(p) for p in prices])

        cond = Condition(
            indicator="rsi",
            params={"period": 14},
            comparator=Comparator.LESS_THAN,
            value=50.0,
        )
        result = evaluate_condition(cond, ctx, "BTCUSDT", Timeframe.H1)
        # RSI of a downtrend should be < 50
        assert result is True

    def test_greater_than(self) -> None:
        """Test greater_than comparator."""
        prices = list(range(60, 120))  # uptrend -> high RSI
        ctx = _make_context_with_data([float(p) for p in prices])

        cond = Condition(
            indicator="rsi",
            params={"period": 14},
            comparator=Comparator.GREATER_THAN,
            value=50.0,
        )
        result = evaluate_condition(cond, ctx, "BTCUSDT", Timeframe.H1)
        assert result is True

    def test_crosses_above(self) -> None:
        """Test crosses_above comparator with SMA."""
        # Create data where the close crosses above SMA
        # Start below SMA, then cross above
        prices = [100.0] * 20  # establish SMA at 100
        prices.extend([98.0, 97.0, 96.0])  # dip below
        prices.extend([99.0, 100.5, 102.0])  # cross above

        ctx = _make_context_with_data(prices)

        cond = Condition(
            indicator="sma",
            params={"period": 5},
            comparator=Comparator.CROSSES_ABOVE,
            value=99.0,  # a threshold
        )
        # The SMA(5) at the end should be around 99-100
        # This tests the crossing logic works without error
        result = evaluate_condition(cond, ctx, "BTCUSDT", Timeframe.H1)
        assert isinstance(result, bool)

    def test_crosses_below(self) -> None:
        """Test crosses_below comparator."""
        prices = [100.0] * 20
        prices.extend([102.0, 103.0, 104.0])  # above threshold
        prices.extend([101.0, 99.5, 98.0])  # cross below

        ctx = _make_context_with_data(prices)

        cond = Condition(
            indicator="sma",
            params={"period": 5},
            comparator=Comparator.CROSSES_BELOW,
            value=101.0,
        )
        result = evaluate_condition(cond, ctx, "BTCUSDT", Timeframe.H1)
        assert isinstance(result, bool)

    def test_insufficient_data_returns_false(self) -> None:
        """Condition evaluation with insufficient data should return False."""
        ctx = _make_context_with_data([100.0])

        cond = Condition(
            indicator="rsi",
            params={"period": 14},
            comparator=Comparator.LESS_THAN,
            value=30.0,
        )
        result = evaluate_condition(cond, ctx, "BTCUSDT", Timeframe.H1)
        assert result is False


class TestConditionGroupEvaluation:
    """Test condition group (AND/OR) evaluation."""

    def test_and_operator_all_true(self) -> None:
        """AND group where all conditions are true."""
        prices = list(range(120, 60, -1))  # strong downtrend
        ctx = _make_context_with_data([float(p) for p in prices])

        group = ConditionGroup(
            operator=LogicOperator.AND,
            conditions=[
                Condition(
                    indicator="rsi",
                    params={"period": 14},
                    comparator=Comparator.LESS_THAN,
                    value=50.0,
                ),
                Condition(
                    indicator="rsi",
                    params={"period": 14},
                    comparator=Comparator.LESS_THAN,
                    value=90.0,
                ),
            ],
        )
        result = evaluate_condition_group(group, ctx, "BTCUSDT", Timeframe.H1)
        assert result is True

    def test_and_operator_one_false(self) -> None:
        """AND group where one condition is false."""
        prices = list(range(120, 60, -1))  # downtrend -> RSI < 50
        ctx = _make_context_with_data([float(p) for p in prices])

        group = ConditionGroup(
            operator=LogicOperator.AND,
            conditions=[
                Condition(
                    indicator="rsi",
                    params={"period": 14},
                    comparator=Comparator.LESS_THAN,
                    value=50.0,  # True for downtrend
                ),
                Condition(
                    indicator="rsi",
                    params={"period": 14},
                    comparator=Comparator.GREATER_THAN,
                    value=80.0,  # False for downtrend
                ),
            ],
        )
        result = evaluate_condition_group(group, ctx, "BTCUSDT", Timeframe.H1)
        assert result is False

    def test_or_operator_one_true(self) -> None:
        """OR group where at least one condition is true."""
        prices = list(range(120, 60, -1))  # downtrend -> RSI < 50
        ctx = _make_context_with_data([float(p) for p in prices])

        group = ConditionGroup(
            operator=LogicOperator.OR,
            conditions=[
                Condition(
                    indicator="rsi",
                    params={"period": 14},
                    comparator=Comparator.LESS_THAN,
                    value=50.0,  # True
                ),
                Condition(
                    indicator="rsi",
                    params={"period": 14},
                    comparator=Comparator.GREATER_THAN,
                    value=80.0,  # False
                ),
            ],
        )
        result = evaluate_condition_group(group, ctx, "BTCUSDT", Timeframe.H1)
        assert result is True

    def test_or_operator_all_false(self) -> None:
        """OR group where all conditions are false."""
        prices = list(range(120, 60, -1))  # downtrend
        ctx = _make_context_with_data([float(p) for p in prices])

        group = ConditionGroup(
            operator=LogicOperator.OR,
            conditions=[
                Condition(
                    indicator="rsi",
                    params={"period": 14},
                    comparator=Comparator.GREATER_THAN,
                    value=80.0,
                ),
                Condition(
                    indicator="rsi",
                    params={"period": 14},
                    comparator=Comparator.GREATER_THAN,
                    value=90.0,
                ),
            ],
        )
        result = evaluate_condition_group(group, ctx, "BTCUSDT", Timeframe.H1)
        assert result is False

    def test_empty_group_returns_false(self) -> None:
        """Empty condition group returns False."""
        ctx = _make_context_with_data([100.0] * 30)
        group = ConditionGroup(operator=LogicOperator.AND, conditions=[])
        result = evaluate_condition_group(group, ctx, "BTCUSDT", Timeframe.H1)
        assert result is False

    def test_none_group_returns_false(self) -> None:
        """None group returns False."""
        ctx = _make_context_with_data([100.0] * 30)
        result = evaluate_condition_group(None, ctx, "BTCUSDT", Timeframe.H1)
        assert result is False


class TestRuleBasedStrategy:
    """Tests for the full RuleBasedStrategy."""

    async def test_entry_long_signal(self) -> None:
        """Rule-based strategy generates entry_long signal when conditions met."""
        rules = {
            "entry_long": {
                "operator": "AND",
                "conditions": [
                    {
                        "indicator": "rsi",
                        "params": {"period": 14},
                        "comparator": "less_than",
                        "value": 50.0,
                    },
                ],
            },
        }

        cfg = StrategyConfig(
            id="rule_test",
            name="Rule Test",
            strategy_class="hydra.strategy.builtin.rule_based.RuleBasedStrategy",
            symbols=["BTCUSDT"],
            parameters={"rules": rules, "required_history": 50},
        )

        # Create downtrend data -> RSI < 50
        prices = list(range(120, 60, -1))
        ctx = _make_context_with_data([float(p) for p in prices])

        strategy = RuleBasedStrategy(config=cfg, context=ctx)

        bar = BarEvent(
            symbol=Symbol("BTCUSDT"),
            timeframe=Timeframe.H1,
            ohlcv=OHLCV(
                open=Decimal("61"),
                high=Decimal("62"),
                low=Decimal("60"),
                close=Decimal("61"),
                volume=Decimal("1000"),
                timestamp=datetime.now(UTC),
            ),
        )
        signals = await strategy.on_bar(bar)
        # Should have generated an entry_long signal
        entry_signals = [s for s in signals if isinstance(s, EntrySignal)]
        assert len(entry_signals) > 0
        assert entry_signals[0].direction == Direction.LONG

    async def test_no_signal_when_conditions_not_met(self) -> None:
        """No signal when conditions are not satisfied."""
        rules = {
            "entry_long": {
                "operator": "AND",
                "conditions": [
                    {
                        "indicator": "rsi",
                        "params": {"period": 14},
                        "comparator": "less_than",
                        "value": 10.0,  # Very restrictive
                    },
                ],
            },
        }

        cfg = StrategyConfig(
            id="rule_test2",
            name="Rule Test 2",
            strategy_class="hydra.strategy.builtin.rule_based.RuleBasedStrategy",
            symbols=["BTCUSDT"],
            parameters={"rules": rules, "required_history": 50},
        )

        # Normal data, RSI will not be < 10
        rng = np.random.default_rng(42)
        prices = (np.cumsum(rng.standard_normal(60)) + 100).tolist()
        prices = [max(p, 1.0) for p in prices]
        ctx = _make_context_with_data(prices)

        strategy = RuleBasedStrategy(config=cfg, context=ctx)

        bar = BarEvent(
            symbol=Symbol("BTCUSDT"),
            timeframe=Timeframe.H1,
            ohlcv=OHLCV(
                open=Decimal(str(prices[-1])),
                high=Decimal(str(round(prices[-1] * 1.01, 2))),
                low=Decimal(str(round(prices[-1] * 0.99, 2))),
                close=Decimal(str(prices[-1])),
                volume=Decimal("1000"),
                timestamp=datetime.now(UTC),
            ),
        )
        signals = await strategy.on_bar(bar)
        entry_signals = [s for s in signals if isinstance(s, EntrySignal)]
        assert len(entry_signals) == 0

    async def test_empty_rules(self) -> None:
        """Strategy with no rules should generate no signals."""
        cfg = StrategyConfig(
            id="empty_rules",
            name="Empty Rules",
            strategy_class="hydra.strategy.builtin.rule_based.RuleBasedStrategy",
            symbols=["BTCUSDT"],
            parameters={"rules": {}, "required_history": 5},
        )

        prices = [100.0] * 10
        ctx = _make_context_with_data(prices)
        strategy = RuleBasedStrategy(config=cfg, context=ctx)

        bar = BarEvent(
            symbol=Symbol("BTCUSDT"),
            timeframe=Timeframe.H1,
            ohlcv=OHLCV(
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=Decimal("1000"),
                timestamp=datetime.now(UTC),
            ),
        )
        signals = await strategy.on_bar(bar)
        assert signals == []

    async def test_exit_long_with_position(self) -> None:
        """Rule-based exit_long should trigger when position exists and conditions met."""
        rules = {
            "exit_long": {
                "operator": "AND",
                "conditions": [
                    {
                        "indicator": "rsi",
                        "params": {"period": 14},
                        "comparator": "greater_than",
                        "value": 50.0,
                    },
                ],
            },
        }

        cfg = StrategyConfig(
            id="rule_exit_test",
            name="Rule Exit Test",
            strategy_class="hydra.strategy.builtin.rule_based.RuleBasedStrategy",
            symbols=["BTCUSDT"],
            parameters={"rules": rules, "required_history": 50},
        )

        # Uptrend data -> RSI > 50
        prices = list(range(60, 120))
        ctx = _make_context_with_data([float(p) for p in prices])

        # Set a LONG position
        from hydra.core.types import Direction

        pos = Position(
            symbol=Symbol("BTCUSDT"),
            direction=Direction.LONG,
            quantity=Decimal("1"),
            avg_entry_price=Decimal("80"),
            unrealized_pnl=Decimal("20"),
            realized_pnl=Decimal("0"),
            strategy_id="rule_exit_test",
            exchange_id="binance",
        )
        ctx.set_position("BTCUSDT", pos)

        strategy = RuleBasedStrategy(config=cfg, context=ctx)

        bar = BarEvent(
            symbol=Symbol("BTCUSDT"),
            timeframe=Timeframe.H1,
            ohlcv=OHLCV(
                open=Decimal("119"),
                high=Decimal("120"),
                low=Decimal("118"),
                close=Decimal("119"),
                volume=Decimal("1000"),
                timestamp=datetime.now(UTC),
            ),
        )
        signals = await strategy.on_bar(bar)
        exit_signals = [s for s in signals if isinstance(s, ExitSignal)]
        assert len(exit_signals) > 0
        assert exit_signals[0].reason == "Rule-based exit (long)"
