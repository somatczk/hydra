"""Breakout strategy.

Entry: Price breaks above N-bar high with volume spike (>2x average) --> BUY.
       Price breaks below N-bar low with volume spike --> SELL.
Support/resistance via rolling max/min.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np

from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import Direction, Symbol
from hydra.indicators.library import sma
from hydra.strategy.base import BaseStrategy


class BreakoutStrategy(BaseStrategy):
    """N-bar breakout with volume confirmation."""

    @property
    def required_history(self) -> int:
        return self._config.parameters.get("required_history", 30)

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        signals: list[EntrySignal | ExitSignal] = []
        symbol = str(bar.symbol)
        tf = bar.timeframe

        params = self._config.parameters
        lookback = params.get("lookback_period", 20)
        vol_multiplier = params.get("vol_multiplier", 2.0)
        vol_sma_period = params.get("vol_sma_period", 20)

        bars = self._context.bars(symbol, tf, self.required_history)
        if len(bars) < self.required_history:
            return signals

        close = np.array([float(b.close) for b in bars], dtype=np.float64)
        high = np.array([float(b.high) for b in bars], dtype=np.float64)
        low = np.array([float(b.low) for b in bars], dtype=np.float64)
        volume = np.array([float(b.volume) for b in bars], dtype=np.float64)

        vol_avg = sma(volume, vol_sma_period)
        if np.isnan(vol_avg[-1]):
            return signals

        cur_price = close[-1]
        cur_vol = volume[-1]
        avg_vol = vol_avg[-1]
        vol_spike = cur_vol > vol_multiplier * avg_vol

        # N-bar high/low (excluding current bar)
        if len(high) < lookback + 1:
            return signals
        n_bar_high = np.max(high[-(lookback + 1) : -1])
        n_bar_low = np.min(low[-(lookback + 1) : -1])

        # Breakout above resistance
        if cur_price > n_bar_high and vol_spike:
            signals.append(
                EntrySignal(
                    symbol=Symbol(symbol),
                    direction=Direction.LONG,
                    strength=Decimal("0.8"),
                    strategy_id=self.strategy_id,
                    exchange_id=self._config.exchange.exchange_id,
                    market_type=self._config.exchange.market_type,
                )
            )

        # Breakdown below support
        if cur_price < n_bar_low and vol_spike:
            signals.append(
                EntrySignal(
                    symbol=Symbol(symbol),
                    direction=Direction.SHORT,
                    strength=Decimal("0.8"),
                    strategy_id=self.strategy_id,
                    exchange_id=self._config.exchange.exchange_id,
                    market_type=self._config.exchange.market_type,
                )
            )

        return signals
