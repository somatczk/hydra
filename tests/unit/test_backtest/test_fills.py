"""Tests for hydra.backtest.fills -- FillSimulator, CommissionConfig, SlippageModel."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from hydra.backtest.fills import CommissionConfig, FillSimulator, SlippageModel
from hydra.core.types import (
    OHLCV,
    MarketType,
    OrderRequest,
    OrderType,
    Side,
    Symbol,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SYM = Symbol("BTCUSDT")
TS1 = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
TS2 = datetime(2024, 1, 1, 1, 0, tzinfo=UTC)
TS3 = datetime(2024, 1, 1, 2, 0, tzinfo=UTC)


def _bar(
    open_: str = "100",
    high: str = "110",
    low: str = "90",
    close: str = "105",
    volume: str = "1000",
    ts: datetime = TS1,
) -> OHLCV:
    return OHLCV(
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
        timestamp=ts,
    )


def _order(
    side: Side = Side.BUY,
    order_type: OrderType = OrderType.MARKET,
    quantity: str = "1",
    price: str | None = None,
    stop_price: str | None = None,
    market_type: MarketType = MarketType.SPOT,
) -> OrderRequest:
    return OrderRequest(
        symbol=SYM,
        side=side,
        order_type=order_type,
        quantity=Decimal(quantity),
        strategy_id="test-strat",
        exchange_id="binance",
        market_type=market_type,
        price=Decimal(price) if price else None,
        stop_price=Decimal(stop_price) if stop_price else None,
    )


@pytest.fixture
def default_commission() -> CommissionConfig:
    return CommissionConfig()


@pytest.fixture
def simulator() -> FillSimulator:
    return FillSimulator(
        SlippageModel(spread_factor=Decimal("0"), volume_impact_factor=Decimal("0"))
    )


@pytest.fixture
def simulator_with_slippage() -> FillSimulator:
    return FillSimulator(SlippageModel())


# ---------------------------------------------------------------------------
# Market order fills
# ---------------------------------------------------------------------------


class TestMarketOrderFill:
    def test_market_buy_fills_at_next_bar_open(
        self, simulator: FillSimulator, default_commission: CommissionConfig
    ) -> None:
        """Market buy should fill at the next bar's open price (zero slippage)."""
        current = _bar(ts=TS1)
        next_bar = _bar(open_="102", ts=TS2)
        order = _order(side=Side.BUY, order_type=OrderType.MARKET)

        fill = simulator.simulate_fill(order, current, next_bar, default_commission)

        assert fill is not None
        assert fill.price == Decimal("102")
        assert fill.side == Side.BUY
        assert fill.quantity == Decimal("1")
        assert fill.timestamp == TS2

    def test_market_sell_fills_at_next_bar_open(
        self, simulator: FillSimulator, default_commission: CommissionConfig
    ) -> None:
        """Market sell should fill at next bar's open price."""
        current = _bar(ts=TS1)
        next_bar = _bar(open_="98", ts=TS2)
        order = _order(side=Side.SELL, order_type=OrderType.MARKET)

        fill = simulator.simulate_fill(order, current, next_bar, default_commission)

        assert fill is not None
        assert fill.price == Decimal("98")

    def test_market_order_no_next_bar_returns_none(
        self, simulator: FillSimulator, default_commission: CommissionConfig
    ) -> None:
        """Market order with no next bar should return None."""
        current = _bar(ts=TS1)
        order = _order(side=Side.BUY, order_type=OrderType.MARKET)

        fill = simulator.simulate_fill(order, current, None, default_commission)

        assert fill is None

    def test_market_order_with_slippage(
        self, simulator_with_slippage: FillSimulator, default_commission: CommissionConfig
    ) -> None:
        """Market buy with slippage should fill above the open price."""
        current = _bar(ts=TS1)
        next_bar = _bar(open_="100", ts=TS2)
        order = _order(side=Side.BUY, order_type=OrderType.MARKET, quantity="10")

        fill = simulator_with_slippage.simulate_fill(
            order, current, next_bar, default_commission, avg_volume=Decimal("1000")
        )

        assert fill is not None
        # Price should be above 100 due to slippage
        assert fill.price > Decimal("100")


# ---------------------------------------------------------------------------
# Limit order fills
# ---------------------------------------------------------------------------


class TestLimitOrderFill:
    def test_limit_buy_fills_when_price_crosses(
        self, simulator: FillSimulator, default_commission: CommissionConfig
    ) -> None:
        """Limit buy at 95 should fill when bar low <= 95."""
        bar = _bar(low="90", high="110", ts=TS1)
        order = _order(side=Side.BUY, order_type=OrderType.LIMIT, price="95")

        fill = simulator.simulate_fill(order, bar, None, default_commission)

        assert fill is not None
        assert fill.price <= Decimal("95")

    def test_limit_buy_not_filled_when_price_above(
        self, simulator: FillSimulator, default_commission: CommissionConfig
    ) -> None:
        """Limit buy at 85 should NOT fill when bar low is 90."""
        bar = _bar(low="90", high="110", ts=TS1)
        order = _order(side=Side.BUY, order_type=OrderType.LIMIT, price="85")

        fill = simulator.simulate_fill(order, bar, None, default_commission)

        assert fill is None

    def test_limit_sell_fills_when_price_crosses(
        self, simulator: FillSimulator, default_commission: CommissionConfig
    ) -> None:
        """Limit sell at 108 should fill when bar high >= 108."""
        bar = _bar(low="90", high="110", ts=TS1)
        order = _order(side=Side.SELL, order_type=OrderType.LIMIT, price="108")

        fill = simulator.simulate_fill(order, bar, None, default_commission)

        assert fill is not None
        assert fill.price >= Decimal("108")

    def test_limit_sell_not_filled_when_price_below(
        self, simulator: FillSimulator, default_commission: CommissionConfig
    ) -> None:
        """Limit sell at 115 should NOT fill when bar high is 110."""
        bar = _bar(low="90", high="110", ts=TS1)
        order = _order(side=Side.SELL, order_type=OrderType.LIMIT, price="115")

        fill = simulator.simulate_fill(order, bar, None, default_commission)

        assert fill is None


# ---------------------------------------------------------------------------
# Stop-market order fills
# ---------------------------------------------------------------------------


class TestStopMarketFill:
    def test_stop_market_buy_triggers(
        self, simulator: FillSimulator, default_commission: CommissionConfig
    ) -> None:
        """Buy stop at 108 should trigger when bar high >= 108."""
        bar = _bar(low="90", high="110", ts=TS1)
        order = _order(side=Side.BUY, order_type=OrderType.STOP_MARKET, stop_price="108")

        fill = simulator.simulate_fill(order, bar, None, default_commission)

        assert fill is not None
        assert fill.price == Decimal("108")  # Zero slippage simulator
        assert fill.side == Side.BUY

    def test_stop_market_sell_triggers(
        self, simulator: FillSimulator, default_commission: CommissionConfig
    ) -> None:
        """Sell stop at 92 should trigger when bar low <= 92."""
        bar = _bar(low="90", high="110", ts=TS1)
        order = _order(side=Side.SELL, order_type=OrderType.STOP_MARKET, stop_price="92")

        fill = simulator.simulate_fill(order, bar, None, default_commission)

        assert fill is not None
        assert fill.price == Decimal("92")

    def test_stop_market_not_triggered(
        self, simulator: FillSimulator, default_commission: CommissionConfig
    ) -> None:
        """Buy stop at 115 should NOT trigger when bar high is 110."""
        bar = _bar(low="90", high="110", ts=TS1)
        order = _order(side=Side.BUY, order_type=OrderType.STOP_MARKET, stop_price="115")

        fill = simulator.simulate_fill(order, bar, None, default_commission)

        assert fill is None


# ---------------------------------------------------------------------------
# Commission calculation
# ---------------------------------------------------------------------------


class TestCommission:
    def test_spot_taker_commission(self) -> None:
        """Fee = quantity * price * fee_rate."""
        fee = FillSimulator.calculate_fee(
            quantity=Decimal("2"),
            price=Decimal("50000"),
            fee_rate=Decimal("0.001"),
        )
        # 2 * 50000 * 0.001 = 100
        assert fee == Decimal("100.00000000")

    def test_spot_maker_rate(self) -> None:
        config = CommissionConfig(spot_maker=Decimal("0.0005"), spot_taker=Decimal("0.001"))
        assert config.fee_rate(MarketType.SPOT, is_maker=True) == Decimal("0.0005")
        assert config.fee_rate(MarketType.SPOT, is_maker=False) == Decimal("0.001")

    def test_futures_rate(self) -> None:
        config = CommissionConfig(futures_maker=Decimal("0.0002"), futures_taker=Decimal("0.0004"))
        assert config.fee_rate(MarketType.FUTURES, is_maker=True) == Decimal("0.0002")
        assert config.fee_rate(MarketType.FUTURES, is_maker=False) == Decimal("0.0004")

    def test_commission_applied_to_market_fill(self, simulator: FillSimulator) -> None:
        """Market order fill should include commission."""
        comm = CommissionConfig(spot_taker=Decimal("0.001"))
        bar = _bar(ts=TS1)
        next_bar = _bar(open_="100", ts=TS2)
        order = _order(side=Side.BUY, order_type=OrderType.MARKET, quantity="2")

        fill = simulator.simulate_fill(order, bar, next_bar, comm)

        assert fill is not None
        # fee = 2 * 100 * 0.001 = 0.2
        expected_fee = Decimal("0.20000000")
        assert fill.fee == expected_fee


# ---------------------------------------------------------------------------
# Slippage model
# ---------------------------------------------------------------------------


class TestSlippageModel:
    def test_zero_slippage(self) -> None:
        """Zero spread and volume impact should give zero slippage."""
        model = SlippageModel(
            spread_factor=Decimal("0"),
            volume_impact_factor=Decimal("0"),
        )
        sim = FillSimulator(model)
        slippage = sim._compute_slippage(Decimal("100"), Decimal("1"), Decimal("1000"))
        assert slippage == Decimal("0")

    def test_spread_only_slippage(self) -> None:
        """With spread_factor=0.001 and no volume impact, slippage = 0.001/2 * price."""
        model = SlippageModel(
            spread_factor=Decimal("0.001"),
            volume_impact_factor=Decimal("0"),
        )
        sim = FillSimulator(model)
        slippage = sim._compute_slippage(Decimal("100"), Decimal("1"), Decimal("1000"))
        expected = Decimal("0.05")  # 0.001/2 * 100
        assert slippage == expected

    def test_slippage_increases_with_order_size(self) -> None:
        """Larger orders should produce more slippage."""
        sim = FillSimulator(SlippageModel())
        small_slip = sim._compute_slippage(Decimal("100"), Decimal("1"), Decimal("1000"))
        large_slip = sim._compute_slippage(Decimal("100"), Decimal("100"), Decimal("1000"))
        assert large_slip > small_slip


# ---------------------------------------------------------------------------
# Parameterized: all order types with known bars
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    (
        "side",
        "order_type",
        "price",
        "stop_price",
        "bar_open",
        "bar_high",
        "bar_low",
        "expected_fill",
    ),
    [
        # Market buy fills at next bar open
        (Side.BUY, OrderType.MARKET, None, None, "100", "110", "90", True),
        # Market sell fills at next bar open
        (Side.SELL, OrderType.MARKET, None, None, "100", "110", "90", True),
        # Limit buy at 95 fills (low=90)
        (Side.BUY, OrderType.LIMIT, "95", None, "100", "110", "90", True),
        # Limit buy at 85 does not fill (low=90)
        (Side.BUY, OrderType.LIMIT, "85", None, "100", "110", "90", False),
        # Stop market buy at 108 triggers (high=110)
        (Side.BUY, OrderType.STOP_MARKET, None, "108", "100", "110", "90", True),
        # Stop market buy at 115 does not trigger (high=110)
        (Side.BUY, OrderType.STOP_MARKET, None, "115", "100", "110", "90", False),
    ],
    ids=[
        "market_buy_fills",
        "market_sell_fills",
        "limit_buy_fills",
        "limit_buy_no_fill",
        "stop_buy_triggers",
        "stop_buy_no_trigger",
    ],
)
def test_order_types_parameterized(
    side: Side,
    order_type: OrderType,
    price: str | None,
    stop_price: str | None,
    bar_open: str,
    bar_high: str,
    bar_low: str,
    expected_fill: bool,
) -> None:
    """Parameterized test for all order types against known bar data."""
    sim = FillSimulator(
        SlippageModel(spread_factor=Decimal("0"), volume_impact_factor=Decimal("0"))
    )
    comm = CommissionConfig()
    bar = _bar(open_=bar_open, high=bar_high, low=bar_low, ts=TS1)
    next_bar = _bar(open_=bar_open, ts=TS2)

    order = _order(
        side=side,
        order_type=order_type,
        price=price,
        stop_price=stop_price,
    )

    fill = sim.simulate_fill(order, bar, next_bar, comm)
    assert (fill is not None) == expected_fill
