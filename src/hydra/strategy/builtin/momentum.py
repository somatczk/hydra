"""Momentum RSI + MACD strategy.

Entry: RSI < 30 AND MACD histogram crosses above 0 --> BUY (long).
       RSI > 70 AND MACD histogram crosses below 0 --> SELL (short).
Exit:  ATR-based trailing stop loss.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np

from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import Direction, Symbol
from hydra.indicators.library import atr, macd, rsi
from hydra.strategy.base import BaseStrategy


class MomentumRSIMACDStrategy(BaseStrategy):
    """RSI + MACD momentum strategy."""

    @property
    def required_history(self) -> int:
        return self._config.parameters.get("required_history", 50)

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        signals: list[EntrySignal | ExitSignal] = []
        symbol = str(bar.symbol)
        tf = bar.timeframe

        params = self._config.parameters
        rsi_period = params.get("rsi_period", 14)
        rsi_oversold = params.get("rsi_oversold", 30)
        rsi_overbought = params.get("rsi_overbought", 70)
        macd_fast = params.get("macd_fast", 12)
        macd_slow = params.get("macd_slow", 26)
        macd_signal = params.get("macd_signal", 9)
        atr_period = params.get("atr_period", 14)

        bars = self._context.bars(symbol, tf, self.required_history)
        if len(bars) < self.required_history:
            return signals

        close = np.array([float(b.close) for b in bars], dtype=np.float64)
        high = np.array([float(b.high) for b in bars], dtype=np.float64)
        low = np.array([float(b.low) for b in bars], dtype=np.float64)

        rsi_vals = rsi(close, rsi_period)
        _, _, hist = macd(close, macd_fast, macd_slow, macd_signal)
        atr_vals = atr(high, low, close, atr_period)

        if len(rsi_vals) < 2 or len(hist) < 2:
            return signals

        cur_rsi = rsi_vals[-1]
        cur_hist = hist[-1]
        prev_hist = hist[-2]
        cur_atr = atr_vals[-1]

        if np.isnan(cur_rsi) or np.isnan(cur_hist) or np.isnan(prev_hist):
            return signals

        # Long entry: RSI oversold + MACD histogram crosses above 0
        if cur_rsi < rsi_oversold and prev_hist <= 0 and cur_hist > 0:
            strength = Decimal(str(round((rsi_oversold - cur_rsi) / rsi_oversold, 4)))
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

        # Short entry: RSI overbought + MACD histogram crosses below 0
        if cur_rsi > rsi_overbought and prev_hist >= 0 and cur_hist < 0:
            strength = Decimal(str(round((cur_rsi - rsi_overbought) / (100 - rsi_overbought), 4)))
            signals.append(
                EntrySignal(
                    symbol=Symbol(symbol),
                    direction=Direction.SHORT,
                    strength=strength,
                    strategy_id=self.strategy_id,
                    exchange_id=self._config.exchange.exchange_id,
                    market_type=self._config.exchange.market_type,
                )
            )

        # ATR-based exit check
        position = self._context.position(symbol)
        if position is not None and not np.isnan(cur_atr):
            stop_multiplier = params.get("atr_stop_multiplier", 2.0)
            entry_price = float(position.avg_entry_price)
            current_price = float(bars[-1].close)

            if position.direction == Direction.LONG:
                stop_price = entry_price - stop_multiplier * cur_atr
                if current_price <= stop_price:
                    signals.append(
                        ExitSignal(
                            symbol=Symbol(symbol),
                            direction=Direction.FLAT,
                            strategy_id=self.strategy_id,
                            exchange_id=self._config.exchange.exchange_id,
                            reason="ATR stop loss (long)",
                        )
                    )
            elif position.direction == Direction.SHORT:
                stop_price = entry_price + stop_multiplier * cur_atr
                if current_price >= stop_price:
                    signals.append(
                        ExitSignal(
                            symbol=Symbol(symbol),
                            direction=Direction.FLAT,
                            strategy_id=self.strategy_id,
                            exchange_id=self._config.exchange.exchange_id,
                            reason="ATR stop loss (short)",
                        )
                    )

        return signals
