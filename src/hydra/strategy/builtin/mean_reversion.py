"""Mean-reversion Bollinger Band strategy.

Entry: Price touches lower BB AND volume > 1.5x SMA(20) volume --> BUY.
Exit:  Price touches upper BB --> SELL / exit.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np

from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import Direction, Symbol
from hydra.indicators.library import bollinger_bands, sma
from hydra.strategy.base import BaseStrategy


class MeanReversionBBStrategy(BaseStrategy):
    """Bollinger Band mean-reversion strategy."""

    @property
    def required_history(self) -> int:
        return self._config.parameters.get("required_history", 30)

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        signals: list[EntrySignal | ExitSignal] = []
        symbol = str(bar.symbol)
        tf = bar.timeframe

        params = self._config.parameters
        bb_period = params.get("bb_period", 20)
        bb_std = params.get("bb_std_dev", 2.0)
        vol_sma_period = params.get("vol_sma_period", 20)
        vol_multiplier = params.get("vol_multiplier", 1.5)

        bars = self._context.bars(symbol, tf, self.required_history)
        if len(bars) < self.required_history:
            return signals

        close = np.array([float(b.close) for b in bars], dtype=np.float64)
        volume = np.array([float(b.volume) for b in bars], dtype=np.float64)

        upper, _middle, lower = bollinger_bands(close, bb_period, bb_std)
        vol_sma = sma(volume, vol_sma_period)

        if np.isnan(upper[-1]) or np.isnan(lower[-1]) or np.isnan(vol_sma[-1]):
            return signals

        cur_price = close[-1]
        cur_vol = volume[-1]
        avg_vol = vol_sma[-1]

        # Long entry: price at or below lower BB + volume spike
        if cur_price <= lower[-1] and cur_vol > vol_multiplier * avg_vol:
            bb_width = upper[-1] - lower[-1]
            if bb_width > 0:
                strength = Decimal(
                    str(round(min((lower[-1] - cur_price) / bb_width + 0.5, 1.0), 4))
                )
            else:
                strength = Decimal("0.5")
            signals.append(
                EntrySignal(
                    symbol=Symbol(symbol),
                    direction=Direction.LONG,
                    strength=strength,
                    strategy_id=self.strategy_id,
                    exchange_id=self._config.exchange.exchange_id,
                    market_type=self._config.exchange.market_type,
                )
            )

        # Exit long: price at or above upper BB
        position = self._context.position(symbol)
        if position is not None and position.direction == Direction.LONG and cur_price >= upper[-1]:
            signals.append(
                ExitSignal(
                    symbol=Symbol(symbol),
                    direction=Direction.FLAT,
                    strategy_id=self.strategy_id,
                    exchange_id=self._config.exchange.exchange_id,
                    reason="Price reached upper Bollinger Band",
                )
            )

        return signals
