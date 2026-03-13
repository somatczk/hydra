"""Trend-following strategy using Supertrend + EMA alignment.

Entry: Supertrend direction is bullish AND EMA(50) > EMA(200) --> LONG.
       Supertrend direction is bearish AND EMA(50) < EMA(200) --> SHORT.
Multi-timeframe: optionally confirm on a higher timeframe.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np

from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import Direction, Symbol
from hydra.indicators.library import ema, supertrend
from hydra.strategy.base import BaseStrategy


class TrendFollowingSupertrend(BaseStrategy):
    """Supertrend + dual-EMA trend-following strategy."""

    @property
    def required_history(self) -> int:
        return self._config.parameters.get("required_history", 210)

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        signals: list[EntrySignal | ExitSignal] = []
        symbol = str(bar.symbol)
        tf = bar.timeframe

        params = self._config.parameters
        st_period = params.get("supertrend_period", 10)
        st_multiplier = params.get("supertrend_multiplier", 3.0)
        fast_ema_period = params.get("fast_ema", 50)
        slow_ema_period = params.get("slow_ema", 200)

        bars = self._context.bars(symbol, tf, self.required_history)
        if len(bars) < self.required_history:
            return signals

        close = np.array([float(b.close) for b in bars], dtype=np.float64)
        high = np.array([float(b.high) for b in bars], dtype=np.float64)
        low = np.array([float(b.low) for b in bars], dtype=np.float64)

        _, st_dir = supertrend(high, low, close, st_period, st_multiplier)
        fast_ema_vals = ema(close, fast_ema_period)
        slow_ema_vals = ema(close, slow_ema_period)

        if np.isnan(st_dir[-1]) or np.isnan(fast_ema_vals[-1]) or np.isnan(slow_ema_vals[-1]):
            return signals

        is_bullish_st = st_dir[-1] == 1.0
        is_bearish_st = st_dir[-1] == -1.0
        ema_bull = fast_ema_vals[-1] > slow_ema_vals[-1]
        ema_bear = fast_ema_vals[-1] < slow_ema_vals[-1]

        # Optional multi-timeframe confirmation
        htf_confirmed = True
        confirm_tf = self._config.timeframes.confirmation
        if confirm_tf is not None:
            htf_bars = self._context.bars(symbol, confirm_tf, st_period + 10)
            if len(htf_bars) >= st_period + 1:
                htf_close = np.array([float(b.close) for b in htf_bars], dtype=np.float64)
                htf_high = np.array([float(b.high) for b in htf_bars], dtype=np.float64)
                htf_low = np.array([float(b.low) for b in htf_bars], dtype=np.float64)
                _, htf_dir = supertrend(htf_high, htf_low, htf_close, st_period, st_multiplier)
                if not np.isnan(htf_dir[-1]) and (
                    (is_bullish_st and htf_dir[-1] != 1.0)
                    or (is_bearish_st and htf_dir[-1] != -1.0)
                ):
                    htf_confirmed = False

        if not htf_confirmed:
            return signals

        # Detect direction change (previous bar vs current)
        if len(st_dir) >= 2 and not np.isnan(st_dir[-2]):
            prev_bull = st_dir[-2] == 1.0
            cur_bull = st_dir[-1] == 1.0

            # Long entry
            if is_bullish_st and ema_bull and not prev_bull and cur_bull:
                signals.append(
                    EntrySignal(
                        symbol=Symbol(symbol),
                        direction=Direction.LONG,
                        strength=Decimal("0.7"),
                        strategy_id=self.strategy_id,
                        exchange_id=self._config.exchange.exchange_id,
                        market_type=self._config.exchange.market_type,
                    )
                )

            # Short entry
            if is_bearish_st and ema_bear and prev_bull and not cur_bull:
                signals.append(
                    EntrySignal(
                        symbol=Symbol(symbol),
                        direction=Direction.SHORT,
                        strength=Decimal("0.7"),
                        strategy_id=self.strategy_id,
                        exchange_id=self._config.exchange.exchange_id,
                        market_type=self._config.exchange.market_type,
                    )
                )

        # Exit on direction reversal
        position = self._context.position(symbol)
        if position is not None:
            if position.direction == Direction.LONG and is_bearish_st:
                signals.append(
                    ExitSignal(
                        symbol=Symbol(symbol),
                        direction=Direction.FLAT,
                        strategy_id=self.strategy_id,
                        exchange_id=self._config.exchange.exchange_id,
                        reason="Supertrend reversal (long exit)",
                    )
                )
            elif position.direction == Direction.SHORT and is_bullish_st:
                signals.append(
                    ExitSignal(
                        symbol=Symbol(symbol),
                        direction=Direction.FLAT,
                        strategy_id=self.strategy_id,
                        exchange_id=self._config.exchange.exchange_id,
                        reason="Supertrend reversal (short exit)",
                    )
                )

        return signals
