"""ML ensemble strategy.

Wraps ML model predictions as trading signals.  Uses a configurable
confidence threshold to filter weak predictions.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np

from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import Direction, Symbol
from hydra.strategy.base import BaseStrategy


class MLEnsembleStrategy(BaseStrategy):
    """Strategy that wraps ML model predictions as signals."""

    @property
    def required_history(self) -> int:
        return self._config.parameters.get("required_history", 100)

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        signals: list[EntrySignal | ExitSignal] = []
        symbol = str(bar.symbol)
        tf = bar.timeframe

        params = self._config.parameters
        confidence_threshold = params.get("confidence_threshold", 0.6)

        bars = self._context.bars(symbol, tf, self.required_history)
        if len(bars) < self.required_history:
            return signals

        # Build feature vector from bars
        close = np.array([float(b.close) for b in bars], dtype=np.float64)
        high = np.array([float(b.high) for b in bars], dtype=np.float64)
        low = np.array([float(b.low) for b in bars], dtype=np.float64)
        volume = np.array([float(b.volume) for b in bars], dtype=np.float64)

        # Attempt ML prediction via context or model reference
        # For now, use a simple momentum-based heuristic as placeholder
        prediction = self._compute_prediction(close, high, low, volume)

        direction_val = prediction.get("direction", 0)
        confidence = prediction.get("confidence", 0.0)

        if confidence < confidence_threshold:
            return signals

        if direction_val > 0:
            signals.append(
                EntrySignal(
                    symbol=Symbol(symbol),
                    direction=Direction.LONG,
                    strength=Decimal(str(round(confidence, 4))),
                    strategy_id=self.strategy_id,
                    exchange_id=self._config.exchange.exchange_id,
                    market_type=self._config.exchange.market_type,
                )
            )
        elif direction_val < 0:
            signals.append(
                EntrySignal(
                    symbol=Symbol(symbol),
                    direction=Direction.SHORT,
                    strength=Decimal(str(round(confidence, 4))),
                    strategy_id=self.strategy_id,
                    exchange_id=self._config.exchange.exchange_id,
                    market_type=self._config.exchange.market_type,
                )
            )

        return signals

    def _compute_prediction(
        self,
        close: np.ndarray,
        high: np.ndarray,
        low: np.ndarray,
        volume: np.ndarray,
    ) -> dict[str, float]:
        """Compute a simple momentum-based prediction as ML placeholder.

        In production this would call ``context.ml_predict()`` or an
        ONNX Runtime model.  Returns ``{"direction": +1/0/-1, "confidence": 0..1}``.
        """
        if len(close) < 20:
            return {"direction": 0, "confidence": 0.0}

        # Simple momentum: compare recent returns to longer-term
        short_ret = (close[-1] / close[-5] - 1) if close[-5] != 0 else 0
        long_ret = (close[-1] / close[-20] - 1) if close[-20] != 0 else 0

        if short_ret > 0 and long_ret > 0:
            return {"direction": 1, "confidence": min(abs(short_ret) * 10, 1.0)}
        if short_ret < 0 and long_ret < 0:
            return {"direction": -1, "confidence": min(abs(short_ret) * 10, 1.0)}
        return {"direction": 0, "confidence": 0.0}
