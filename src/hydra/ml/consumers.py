"""The ML consumption layer's session glue (P5-C): the veto-only overlay and
the fail-closed session-start gate both consumers ride.

Two consumers share one attach pipeline (:mod:`hydra.ml.serving`):

- the **overlay** — any strategy with ``ml_overlay.enabled`` gets its entry
  signals gated at the session's ``_on_signal`` seam: the Production model's
  confidence for the signal's own direction must be at least the configured
  threshold or the entry is vetoed. Veto-only: the overlay never generates,
  resizes or bypasses, and every failure mode is fail-closed — an entry the
  model could not bless simply does not happen. Shadow mode records the
  would-veto and lets the entry through.
- the **ML-native strategy** (:class:`~hydra.strategy.builtin.ml_ensemble.
  MLEnsembleStrategy`) consumes the Production model's predictions as its
  signal source; its predictor is attached into
  :mod:`hydra.strategy.ml_access` here, before the session runs, and the
  strategy pins it at ``on_start`` (fail-closed absent).

``validate_session_ml`` is the session-start gate: it runs the pure config
checks first (the frozen 1h/4h/1d grid and the history window), then attaches
the Production model — so a session refuses to start with no Production
version, a missing artifact on the models mount, unreadable bytes or a
feature-spec mismatch, never at save time and never silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from weakref import WeakKeyDictionary

from hydra.core.config import HydraConfig
from hydra.core.events import EntrySignal
from hydra.core.logging import get_logger
from hydra.core.metrics import record_ml_overlay_decision
from hydra.core.types import OHLCV, Timeframe
from hydra.ml.features import DEFAULT_FEATURE_SPEC, build_live_features
from hydra.ml.serving import AttachedModel, MlServingError, attach_production
from hydra.strategy.builtin.ml_ensemble import STRATEGY_CLASS as ML_NATIVE_CLASS
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext
from hydra.strategy.engine import _import_strategy_class
from hydra.strategy.ml_access import attach_provider

logger = get_logger(__name__)

OVERLAY_GRID: tuple[Timeframe, Timeframe, Timeframe] = (
    Timeframe.H1,
    Timeframe.H4,
    Timeframe.D1,
)
"""The frozen feature spec's grid: the 1h primary plus the 4h/1d higher TFs."""


# ---------------------------------------------------------------------------
# The live vector (the SM-14 parity seam: one computation path, shared)
# ---------------------------------------------------------------------------


def live_vector(bars_1h: list[OHLCV], bars_4h: list[OHLCV], bars_1d: list[OHLCV]) -> Any:
    """The live feature vector from the frozen spec — the training path's last row."""
    return build_live_features(DEFAULT_FEATURE_SPEC, bars_1h, bars_4h, bars_1d)


def _overlay_bars(
    context: StrategyContext, symbol: str
) -> tuple[list[OHLCV], list[OHLCV], list[OHLCV]]:
    """The spec's windows of 1h/4h/1d bars for *symbol* from the session context."""
    return (
        context.bars(symbol, Timeframe.H1, DEFAULT_FEATURE_SPEC.window_1h),
        context.bars(symbol, Timeframe.H4, DEFAULT_FEATURE_SPEC.window_4h),
        context.bars(symbol, Timeframe.D1, DEFAULT_FEATURE_SPEC.window_1d),
    )


# ---------------------------------------------------------------------------
# The veto-only overlay
# ---------------------------------------------------------------------------


@runtime_checkable
class SignalHost(Protocol):
    """What the seam hands the overlay: the running session's config and context."""

    cfg: StrategyConfig
    context: StrategyContext


@dataclass(frozen=True)
class MLOverlay:
    """One session's pinned overlay: the attached model, threshold and mode."""

    model: AttachedModel
    threshold: float
    shadow: bool


_OVERLAYS: WeakKeyDictionary[SignalHost, MLOverlay] = WeakKeyDictionary()


def _record(strategy_id: str, model_name: str, decision: str, **fields: Any) -> None:
    """One observable overlay decision: the counter and the structured log line."""
    record_ml_overlay_decision(strategy_id, model_name, decision)
    logger.info(
        "ml_overlay_decision",
        strategy=strategy_id,
        model=model_name,
        decision=decision,
        **fields,
    )


async def veto_entry(
    host: SignalHost,
    pool: Any,
    hydra_config: HydraConfig,
    signal: EntrySignal,
) -> bool:
    """The seam: True when the entry is vetoed, False when it passes.

    Called once per entry signal from ``SessionRuntime._on_signal`` with the
    runtime itself as *host*. No overlay config (or disabled): False at zero
    cost — the session behaves exactly as before. Otherwise the Production
    model is attached once per session and pinned; per signal the live feature
    vector is computed from the context's 1h/4h/1d bars and the model's
    confidence for the signal's own direction (LONG → P(up), SHORT → 1 -
    P(up)) is compared against the threshold — at the threshold the entry
    passes. Any failure fails closed: the decision is recorded as ``error``
    and the entry is vetoed, never a silent pass-through.
    """
    overlay_cfg = host.cfg.ml_overlay
    if overlay_cfg is None or not overlay_cfg.enabled:
        return False
    strategy_id = host.cfg.id
    symbol = str(signal.symbol)
    try:
        overlay = _OVERLAYS.get(host)
        if overlay is None:
            attached = await attach_production(
                pool, Path(hydra_config.ml.models_dir), overlay_cfg.model_name
            )
            overlay = MLOverlay(
                model=attached,
                threshold=overlay_cfg.confidence_threshold,
                shadow=overlay_cfg.shadow,
            )
            _OVERLAYS[host] = overlay
        p_up = overlay.model.predict_up(live_vector(*_overlay_bars(host.context, symbol)))
        confidence = p_up if signal.direction.value == "LONG" else 1.0 - p_up
        passed = confidence >= overlay.threshold
        decision = "pass" if passed else ("shadow_veto" if overlay.shadow else "veto")
        _record(
            strategy_id,
            overlay.model.name,
            decision,
            symbol=symbol,
            confidence=round(confidence, 6),
            threshold=overlay.threshold,
            direction=signal.direction.value,
            shadow=overlay.shadow,
        )
        return not passed and not overlay.shadow
    except Exception:
        _record(strategy_id, overlay_cfg.model_name, "error", symbol=symbol)
        logger.exception("ml_overlay_failed", strategy=strategy_id, model=overlay_cfg.model_name)
        return True


def _reset_overlays() -> None:
    """Test isolation: drop every pinned overlay."""
    _OVERLAYS.clear()


# ---------------------------------------------------------------------------
# The ML-native strategy's predictor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveFeaturesPredictor:
    """The provider the ML-native strategy consumes: attached model + live features.

    Implements ``hydra.strategy.ml_access.PredictionProvider`` structurally —
    the strategy layer never imports ``hydra.ml``.
    """

    model: AttachedModel

    def predict_up(self, bars_1h: list[OHLCV], bars_4h: list[OHLCV], bars_1d: list[OHLCV]) -> float:
        """The model's P(up) at the last 1h bar of the given windows."""
        return float(self.model.predict_up(live_vector(bars_1h, bars_4h, bars_1d)))


def attach_native_provider(attached: AttachedModel) -> None:
    """Register *attached* as its name's predictor for the ML-native strategy.

    The session-start validation calls this after a successful attach; a
    backtest harness serving a fixture model calls it directly.
    """
    attach_provider(attached.name, attached.version, LiveFeaturesPredictor(attached))


# ---------------------------------------------------------------------------
# The fail-closed session-start gate
# ---------------------------------------------------------------------------


def _require_frozen_grid(cfg: StrategyConfig) -> None:
    """Both consumers read the frozen 1h/4h/1d spec off the session's context."""
    declared = {str(tf) for tf in cfg.all_timeframes()}
    if str(cfg.timeframes.primary) != str(OVERLAY_GRID[0]) or not {
        str(tf) for tf in OVERLAY_GRID[1:]
    }.issubset(declared):
        raise ValueError(
            f"strategy {cfg.id} must trade the frozen ML feature grid "
            "(timeframes primary 1h with 4h and 1d streams; the model's "
            "features are computed from exactly those bars)"
        )


def _required_history(cfg: StrategyConfig) -> int:
    """The strategy instance's own required history (a throwaway instantiate)."""
    cls = _import_strategy_class(cfg.strategy_class)
    return int(cls(config=cfg, context=StrategyContext()).required_history)


async def validate_session_ml(pool: Any, models_dir: Path, cfg: StrategyConfig) -> None:
    """The session-start validation: every model the session will serve must be
    attachable NOW. Raises ``ValueError`` for a config that cannot carry the
    frozen spec and :class:`~hydra.ml.serving.MlServingError` when the model
    cannot be served — never at save time, never a silent pass-through.

    Also attaches the ML-native strategy's predictor, so a session that
    starts here is a session whose model is already loaded.
    """
    overlay = cfg.ml_overlay
    native = cfg.strategy_class == ML_NATIVE_CLASS
    if (overlay is None or not overlay.enabled) and not native:
        return
    _require_frozen_grid(cfg)
    history = _required_history(cfg)
    if history < DEFAULT_FEATURE_SPEC.window_1h:
        raise ValueError(
            f"strategy {cfg.id} requires {history} bars of history but the "
            f"frozen feature spec's 1h window is {DEFAULT_FEATURE_SPEC.window_1h}; "
            "raise parameters.required_history before starting the session"
        )
    if pool is None:
        raise MlServingError(
            "unavailable",
            f"strategy {cfg.id} consumes a Production model but the model "
            "registry is unreachable (no database pool); the session refuses "
            "to start without the model",
        )
    if overlay is not None and overlay.enabled:
        await attach_production(pool, models_dir, overlay.model_name)
    if native:
        model_name = str(cfg.parameters.get("model_name", ""))
        attach_native_provider(await attach_production(pool, models_dir, model_name))
