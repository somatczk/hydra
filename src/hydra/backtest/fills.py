"""Backtest commission rates. Fills come from the paper executor (``hydra.execution``)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from hydra.core.types import MarketType


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
