"""Fill simulation for backtesting: realistic order fill with slippage and commissions.

Simulates market, limit, stop-market, stop-limit, and OCO order fills using
OHLCV bar data. All financial calculations use Decimal for precision.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from hydra.core.types import (
    OHLCV,
    MarketType,
    OrderFill,
    OrderRequest,
    OrderType,
    Side,
)

# ---------------------------------------------------------------------------
# Configuration dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SlippageModel:
    """Controls how slippage is computed for market and stop-market fills.

    slippage = spread_factor / 2 + sqrt(order_size / avg_volume) * volume_impact_factor
    """

    spread_factor: Decimal = Decimal("0.0001")
    volume_impact_factor: Decimal = Decimal("0.1")


@dataclass(frozen=True, slots=True)
class CommissionConfig:
    """Commission rates expressed as fractions (e.g. 0.001 = 0.1%)."""

    spot_maker: Decimal = Decimal("0.001")
    spot_taker: Decimal = Decimal("0.001")
    futures_maker: Decimal = Decimal("0.0002")
    futures_taker: Decimal = Decimal("0.0004")

    def fee_rate(self, market_type: MarketType, is_maker: bool) -> Decimal:
        """Return the appropriate fee rate."""
        if market_type == MarketType.FUTURES:
            return self.futures_maker if is_maker else self.futures_taker
        return self.spot_maker if is_maker else self.spot_taker


# ---------------------------------------------------------------------------
# FillSimulator
# ---------------------------------------------------------------------------


class FillSimulator:
    """Simulates order fills against OHLCV bars.

    Market orders fill at the *next* bar's open plus slippage.
    Limit/stop orders are checked against the current bar's high/low range.
    """

    def __init__(self, slippage_model: SlippageModel | None = None) -> None:
        self._slippage = slippage_model or SlippageModel()

    # -- Public API ----------------------------------------------------------

    def simulate_fill(
        self,
        order: OrderRequest,
        current_bar: OHLCV,
        next_bar: OHLCV | None,
        commission: CommissionConfig,
        avg_volume: Decimal | None = None,
    ) -> OrderFill | None:
        """Attempt to fill *order* given bar data.

        Returns an ``OrderFill`` if the order would have been executed,
        or ``None`` if conditions were not met.
        """
        if order.order_type == OrderType.MARKET:
            return self._fill_market(order, current_bar, next_bar, commission, avg_volume)
        if order.order_type == OrderType.LIMIT:
            return self._fill_limit(order, current_bar, commission)
        if order.order_type == OrderType.STOP_MARKET:
            return self._fill_stop_market(order, current_bar, commission, avg_volume)
        if order.order_type == OrderType.STOP_LIMIT:
            return self._fill_stop_limit(order, current_bar, commission)
        if order.order_type == OrderType.OCO:
            return self._fill_oco(order, current_bar, commission, avg_volume)
        if order.order_type == OrderType.TAKE_PROFIT_MARKET:
            return self._fill_take_profit_market(order, current_bar, commission, avg_volume)
        return None

    # -- Slippage ------------------------------------------------------------

    def _compute_slippage(
        self,
        price: Decimal,
        order_qty: Decimal,
        avg_volume: Decimal | None,
    ) -> Decimal:
        """Compute slippage amount as an absolute price delta.

        slippage_frac = spread_factor/2 + sqrt(qty / avg_volume) * volume_impact_factor
        slippage_abs  = slippage_frac * price
        """
        spread_half = self._slippage.spread_factor / Decimal("2")
        if avg_volume and avg_volume > 0:
            ratio = float(order_qty / avg_volume)
            vol_impact = Decimal(str(math.sqrt(abs(ratio)))) * self._slippage.volume_impact_factor
        else:
            vol_impact = Decimal("0")
        slippage_frac = spread_half + vol_impact
        return (slippage_frac * price).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)

    # -- Fee calculation -----------------------------------------------------

    @staticmethod
    def calculate_fee(
        quantity: Decimal,
        price: Decimal,
        fee_rate: Decimal,
    ) -> Decimal:
        """fee = quantity * price * fee_rate"""
        return (quantity * price * fee_rate).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)

    # -- Order type handlers -------------------------------------------------

    def _fill_market(
        self,
        order: OrderRequest,
        current_bar: OHLCV,
        next_bar: OHLCV | None,
        commission: CommissionConfig,
        avg_volume: Decimal | None,
    ) -> OrderFill | None:
        """Market order: fill at next bar open + slippage."""
        if next_bar is None:
            return None
        slippage = self._compute_slippage(next_bar.open, order.quantity, avg_volume)
        if order.side == Side.BUY:
            fill_price = next_bar.open + slippage
        else:
            fill_price = next_bar.open - slippage
            fill_price = max(fill_price, Decimal("0"))
        fee_rate = commission.fee_rate(order.market_type, is_maker=False)
        fee = self.calculate_fee(order.quantity, fill_price, fee_rate)
        return OrderFill(
            order_id=order.request_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=fill_price,
            fee=fee,
            fee_currency="USDT",
            timestamp=next_bar.timestamp,
            exchange_id=order.exchange_id,
        )

    def _fill_limit(
        self,
        order: OrderRequest,
        current_bar: OHLCV,
        commission: CommissionConfig,
    ) -> OrderFill | None:
        """Limit order: fill if price crosses limit during the bar."""
        if order.price is None:
            return None
        limit_price = order.price
        if order.side == Side.BUY:
            # Buy limit: fill if bar low <= limit price
            if current_bar.low <= limit_price:
                fill_price = min(limit_price, current_bar.open)
                fee_rate = commission.fee_rate(order.market_type, is_maker=True)
                fee = self.calculate_fee(order.quantity, fill_price, fee_rate)
                return OrderFill(
                    order_id=order.request_id,
                    symbol=order.symbol,
                    side=order.side,
                    quantity=order.quantity,
                    price=fill_price,
                    fee=fee,
                    fee_currency="USDT",
                    timestamp=current_bar.timestamp,
                    exchange_id=order.exchange_id,
                )
        else:
            # Sell limit: fill if bar high >= limit price
            if current_bar.high >= limit_price:
                fill_price = max(limit_price, current_bar.open)
                fee_rate = commission.fee_rate(order.market_type, is_maker=True)
                fee = self.calculate_fee(order.quantity, fill_price, fee_rate)
                return OrderFill(
                    order_id=order.request_id,
                    symbol=order.symbol,
                    side=order.side,
                    quantity=order.quantity,
                    price=fill_price,
                    fee=fee,
                    fee_currency="USDT",
                    timestamp=current_bar.timestamp,
                    exchange_id=order.exchange_id,
                )
        return None

    def _fill_stop_market(
        self,
        order: OrderRequest,
        current_bar: OHLCV,
        commission: CommissionConfig,
        avg_volume: Decimal | None,
    ) -> OrderFill | None:
        """Stop-market: trigger if price crosses stop, fill at stop + slippage."""
        if order.stop_price is None:
            return None
        stop = order.stop_price
        triggered = current_bar.high >= stop if order.side == Side.BUY else current_bar.low <= stop
        if not triggered:
            return None
        slippage = self._compute_slippage(stop, order.quantity, avg_volume)
        if order.side == Side.BUY:
            fill_price = stop + slippage
        else:
            fill_price = stop - slippage
            fill_price = max(fill_price, Decimal("0"))
        fee_rate = commission.fee_rate(order.market_type, is_maker=False)
        fee = self.calculate_fee(order.quantity, fill_price, fee_rate)
        return OrderFill(
            order_id=order.request_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=fill_price,
            fee=fee,
            fee_currency="USDT",
            timestamp=current_bar.timestamp,
            exchange_id=order.exchange_id,
        )

    def _fill_stop_limit(
        self,
        order: OrderRequest,
        current_bar: OHLCV,
        commission: CommissionConfig,
    ) -> OrderFill | None:
        """Stop-limit: trigger at stop, fill at limit if within bar range."""
        if order.stop_price is None or order.price is None:
            return None
        stop = order.stop_price
        limit_price = order.price
        # Check stop trigger
        if order.side == Side.BUY:
            if current_bar.high < stop:
                return None
            # Stop triggered — now check if limit is achievable
            if current_bar.high < limit_price:
                return None
            fill_price = limit_price
        else:
            if current_bar.low > stop:
                return None
            if current_bar.low > limit_price:
                return None
            fill_price = limit_price
        fee_rate = commission.fee_rate(order.market_type, is_maker=True)
        fee = self.calculate_fee(order.quantity, fill_price, fee_rate)
        return OrderFill(
            order_id=order.request_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=fill_price,
            fee=fee,
            fee_currency="USDT",
            timestamp=current_bar.timestamp,
            exchange_id=order.exchange_id,
        )

    def _fill_take_profit_market(
        self,
        order: OrderRequest,
        current_bar: OHLCV,
        commission: CommissionConfig,
        avg_volume: Decimal | None,
    ) -> OrderFill | None:
        """Take-profit market: trigger at stop_price, fill with slippage."""
        if order.stop_price is None:
            return None
        tp_price = order.stop_price
        triggered = False
        if order.side == Side.SELL:
            # Long TP: triggers when price rises to TP
            triggered = current_bar.high >= tp_price
        else:
            # Short TP: triggers when price falls to TP
            triggered = current_bar.low <= tp_price
        if not triggered:
            return None
        slippage = self._compute_slippage(tp_price, order.quantity, avg_volume)
        if order.side == Side.SELL:
            fill_price = tp_price - slippage
            fill_price = max(fill_price, Decimal("0"))
        else:
            fill_price = tp_price + slippage
        fee_rate = commission.fee_rate(order.market_type, is_maker=False)
        fee = self.calculate_fee(order.quantity, fill_price, fee_rate)
        return OrderFill(
            order_id=order.request_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=fill_price,
            fee=fee,
            fee_currency="USDT",
            timestamp=current_bar.timestamp,
            exchange_id=order.exchange_id,
        )

    def _fill_oco(
        self,
        order: OrderRequest,
        current_bar: OHLCV,
        commission: CommissionConfig,
        avg_volume: Decimal | None,
    ) -> OrderFill | None:
        """OCO: check both stop-loss and take-profit, fill whichever triggers first.

        Uses open->high->low->close price sequence heuristic:
        - If close > open (bullish bar): assume open -> low -> high -> close
          (dip first, then rally)
        - If close <= open (bearish bar): assume open -> high -> low -> close
          (rally first, then drop)

        ``stop_price`` = stop-loss level, ``price`` = take-profit level.
        """
        if order.stop_price is None or order.price is None:
            return None

        stop_price = order.stop_price
        tp_price = order.price

        # Determine which triggers: stop-loss or take-profit
        stop_triggered = False
        tp_triggered = False

        if order.side == Side.SELL:
            # Closing a long: stop below, TP above
            stop_triggered = current_bar.low <= stop_price
            tp_triggered = current_bar.high >= tp_price
        else:
            # Closing a short: stop above, TP below
            stop_triggered = current_bar.high >= stop_price
            tp_triggered = current_bar.low <= tp_price

        if not stop_triggered and not tp_triggered:
            return None

        # Both triggered — use bar direction to decide priority
        if stop_triggered and tp_triggered:
            if order.side == Side.SELL:
                # Long position: stop is low, TP is high
                if current_bar.close > current_bar.open:
                    # Bullish: went low first (stop hit first)
                    return self._make_oco_stop_fill(
                        order, stop_price, current_bar, commission, avg_volume
                    )
                else:
                    # Bearish/flat: went high first (TP hit first)
                    return self._make_oco_tp_fill(order, tp_price, current_bar, commission)
            else:
                # Short position: stop is high, TP is low
                if current_bar.close <= current_bar.open:
                    # Bearish: went high first (stop hit first)
                    return self._make_oco_stop_fill(
                        order, stop_price, current_bar, commission, avg_volume
                    )
                else:
                    # Bullish: went low first (TP hit first)
                    return self._make_oco_tp_fill(order, tp_price, current_bar, commission)

        if stop_triggered:
            return self._make_oco_stop_fill(order, stop_price, current_bar, commission, avg_volume)
        # tp_triggered
        return self._make_oco_tp_fill(order, tp_price, current_bar, commission)

    def _make_oco_stop_fill(
        self,
        order: OrderRequest,
        stop_price: Decimal,
        bar: OHLCV,
        commission: CommissionConfig,
        avg_volume: Decimal | None,
    ) -> OrderFill:
        """Fill the stop-loss leg of an OCO with slippage."""
        slippage = self._compute_slippage(stop_price, order.quantity, avg_volume)
        if order.side == Side.SELL:
            fill_price = stop_price - slippage
            fill_price = max(fill_price, Decimal("0"))
        else:
            fill_price = stop_price + slippage
        fee_rate = commission.fee_rate(order.market_type, is_maker=False)
        fee = self.calculate_fee(order.quantity, fill_price, fee_rate)
        return OrderFill(
            order_id=order.request_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=fill_price,
            fee=fee,
            fee_currency="USDT",
            timestamp=bar.timestamp,
            exchange_id=order.exchange_id,
        )

    def _make_oco_tp_fill(
        self,
        order: OrderRequest,
        tp_price: Decimal,
        bar: OHLCV,
        commission: CommissionConfig,
    ) -> OrderFill:
        """Fill the take-profit leg of an OCO (limit-like, no slippage)."""
        fee_rate = commission.fee_rate(order.market_type, is_maker=True)
        fee = self.calculate_fee(order.quantity, tp_price, fee_rate)
        return OrderFill(
            order_id=order.request_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=tp_price,
            fee=fee,
            fee_currency="USDT",
            timestamp=bar.timestamp,
            exchange_id=order.exchange_id,
        )
