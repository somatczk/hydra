"""The ML-native strategy: a real consumer of the Production model (P5-C).

The P0-era momentum heuristic is gone. The strategy consumes the Production
model's predictions as its only signal source: the session-start validation
attaches the model's predictor (registry → artifact → onnxruntime, fail-closed)
before the session runs, the strategy pins it at ``on_start`` — refusing to
run when it is absent — and every closed 1h bar computes the frozen feature
spec's live vector and asks the model for P(up):

- flat and P(up) at or above ``entry_threshold``: a LONG entry whose strength
  is the confidence;
- flat and P(up) at or below ``1 - entry_threshold``: a SHORT entry (the
  session's own spot guard drops it on spot sessions — no special-casing);
- holding LONG and P(up) at or below ``exit_threshold``: a full exit; holding
  SHORT and P(up) at or above ``1 - exit_threshold``: a full exit.

At the threshold the decision fires, mirroring the overlay's edge (an entry is
vetoed only *below* its threshold). The signals ride the same rails as every
other strategy: the engine publishes them, the session sizes, checks and
protects them exactly as before. The strategy layer never imports ``hydra.ml``
— it sees the predictor through ``hydra.strategy.ml_access``.
"""

from __future__ import annotations

from decimal import Decimal

from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import Direction, Symbol, Timeframe
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext
from hydra.strategy.ml_access import PredictionProvider, provider_for

STRATEGY_CLASS = "hydra.strategy.builtin.ml_ensemble.MLEnsembleStrategy"

DEFAULT_REQUIRED_HISTORY = 2000
"""The frozen feature spec's 1h window; a smaller declared history refuses at start."""

_GRID = (Timeframe.H1, Timeframe.H4, Timeframe.D1)


class MLEnsembleStrategy(BaseStrategy):
    """Entries from the Production model's predictions, exits by its inverse band."""

    def __init__(self, config: StrategyConfig, context: StrategyContext) -> None:
        super().__init__(config, context)
        params = config.parameters
        model_name = params.get("model_name")
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError(
                f"strategy {config.id} consumes an ML model: parameters.model_name "
                "must name the Production model it trades on"
            )
        self._model_name = model_name
        self._entry_threshold = float(params.get("entry_threshold", 0.6))
        self._exit_threshold = float(params.get("exit_threshold", 1.0 - self._entry_threshold))
        if not 0.5 < self._entry_threshold < 1.0:
            raise ValueError(
                f"entry_threshold must be in (0.5, 1.0) so the long and short "
                f"entry bands do not overlap, got {self._entry_threshold}"
            )
        if not 0.0 < self._exit_threshold < self._entry_threshold:
            raise ValueError(
                f"exit_threshold must be in (0, entry_threshold), got "
                f"{self._exit_threshold} with entry_threshold {self._entry_threshold}"
            )
        declared = {str(tf) for tf in config.all_timeframes()}
        if str(config.timeframes.primary) != str(_GRID[0]) or not {
            str(tf) for tf in _GRID[1:]
        }.issubset(declared):
            raise ValueError(
                f"strategy {config.id} consumes the frozen 1h/4h/1d feature grid: "
                "timeframes must declare primary 1h plus 4h and 1d streams"
            )
        self._predictor: PredictionProvider | None = None

    @property
    def required_history(self) -> int:
        return int(self._config.parameters.get("required_history", DEFAULT_REQUIRED_HISTORY))

    async def on_start(self) -> None:
        """Pin the model's predictor; a session without one refuses to run."""
        self._predictor = provider_for(self._model_name)

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        predictor = self._predictor
        if predictor is None:
            raise RuntimeError(
                f"strategy {self.strategy_id} has no attached model "
                f"'{self._model_name}'; the session-start validation attaches it"
            )
        symbol = str(bar.symbol)
        window = self.required_history
        p_up = predictor.predict_up(
            self._context.bars(symbol, Timeframe.H1, window),
            self._context.bars(symbol, Timeframe.H4, window),
            self._context.bars(symbol, Timeframe.D1, window),
        )
        position = self._context.position(symbol)
        if position is None or position.quantity <= 0:
            if p_up >= self._entry_threshold:
                return [self._entry(Direction.LONG, symbol, p_up)]
            if p_up <= 1.0 - self._entry_threshold:
                return [self._entry(Direction.SHORT, symbol, 1.0 - p_up)]
            return []
        if position.direction == Direction.LONG:
            if p_up <= self._exit_threshold:
                return [self._exit(symbol)]
        elif p_up >= 1.0 - self._exit_threshold:
            return [self._exit(symbol)]
        return []

    def _entry(self, direction: Direction, symbol: str, confidence: float) -> EntrySignal:
        return EntrySignal(
            symbol=Symbol(symbol),
            direction=direction,
            strength=Decimal(str(round(confidence, 4))),
            strategy_id=self.strategy_id,
            exchange_id=self._config.exchange.exchange_id,
            market_type=self._config.exchange.market_type,
        )

    def _exit(self, symbol: str) -> ExitSignal:
        return ExitSignal(
            symbol=Symbol(symbol),
            strategy_id=self.strategy_id,
            exchange_id=self._config.exchange.exchange_id,
            reason="ml_native_exit",
        )
