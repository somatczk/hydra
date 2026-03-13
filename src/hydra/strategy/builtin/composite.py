"""Composite strategy: weighted voting across sub-strategies.

Takes a list of sub-strategy IDs, collects their signals, and applies
weighted voting with a configurable minimum agreement threshold.
"""

from __future__ import annotations

from decimal import Decimal

from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import Direction, Symbol
from hydra.strategy.base import BaseStrategy


class CompositeStrategy(BaseStrategy):
    """Aggregates signals from multiple sub-strategies via weighted voting."""

    @property
    def required_history(self) -> int:
        return self._config.parameters.get("required_history", 1)

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        signals: list[EntrySignal | ExitSignal] = []
        symbol = str(bar.symbol)

        params = self._config.parameters
        sub_strategy_ids: list[str] = params.get("sub_strategies", [])
        weights: dict[str, float] = params.get("weights", {})
        min_agreement: float = params.get("min_agreement", 0.5)
        default_weight: float = params.get("default_weight", 1.0)

        if not sub_strategy_ids:
            return signals

        # Collect signals from sub-strategies via the sub_signals parameter
        # In the engine, sub-strategy signals are stored in parameters
        sub_signals: list[EntrySignal | ExitSignal] = params.get("_sub_signals", [])

        if not sub_signals:
            return signals

        # Tally weighted votes per direction
        long_score = 0.0
        short_score = 0.0
        exit_score = 0.0
        total_weight = 0.0

        for sig in sub_signals:
            sid = sig.strategy_id
            w = weights.get(sid, default_weight)
            total_weight += w
            if isinstance(sig, EntrySignal):
                if sig.direction == Direction.LONG:
                    long_score += w
                elif sig.direction == Direction.SHORT:
                    short_score += w
            elif isinstance(sig, ExitSignal):
                exit_score += w

        if total_weight == 0:
            return signals

        # Check agreement threshold
        long_ratio = long_score / total_weight
        short_ratio = short_score / total_weight
        exit_ratio = exit_score / total_weight

        if long_ratio >= min_agreement:
            signals.append(
                EntrySignal(
                    symbol=Symbol(symbol),
                    direction=Direction.LONG,
                    strength=Decimal(str(round(long_ratio, 4))),
                    strategy_id=self.strategy_id,
                    exchange_id=self._config.exchange.exchange_id,
                    market_type=self._config.exchange.market_type,
                )
            )
        elif short_ratio >= min_agreement:
            signals.append(
                EntrySignal(
                    symbol=Symbol(symbol),
                    direction=Direction.SHORT,
                    strength=Decimal(str(round(short_ratio, 4))),
                    strategy_id=self.strategy_id,
                    exchange_id=self._config.exchange.exchange_id,
                    market_type=self._config.exchange.market_type,
                )
            )

        if exit_ratio >= min_agreement:
            signals.append(
                ExitSignal(
                    symbol=Symbol(symbol),
                    direction=Direction.FLAT,
                    strategy_id=self.strategy_id,
                    exchange_id=self._config.exchange.exchange_id,
                    reason="Composite agreement to exit",
                )
            )

        return signals
