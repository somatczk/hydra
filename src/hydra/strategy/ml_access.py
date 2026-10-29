"""The model-prediction channel strategies consume (the ML-native consumer).

The strategy layer may not import ``hydra.ml`` (the import-linter layers), so
the Production model's predictor is attached HERE by a higher layer — the
session-start validation, before the session runs — and a strategy pins its
predictor at ``on_start`` and fails closed when none is attached. The registry
is process-local: a restart re-attaches at session start and at recovery, so an
unattached name is a refusal, never a silent no-signal strategy.

Providers are keyed by (model name, registry version) and the first attach of a
key wins (attaching is idempotent); a later Production version attaches under
its own key, and a running strategy keeps the predictor it pinned, so a
mid-session promote never swaps the model under a live session.

A research job (backtest, hyperopt, walk-forward) instead binds its own
predictors with :func:`bound_providers`: a context variable, so the binding —
which may be a pinned non-Production version — is visible only inside that
job's task and never to a session, which resolves only the attached registry.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Protocol, runtime_checkable

from hydra.core.types import OHLCV


@runtime_checkable
class PredictionProvider(Protocol):
    """P(up) for one bar window, computed from the frozen feature spec.

    Implementations live above the strategy layer (``hydra.ml``); this protocol
    is the only shape a strategy sees.
    """

    def predict_up(self, bars_1h: list[OHLCV], bars_4h: list[OHLCV], bars_1d: list[OHLCV]) -> float:
        """The model's confidence in the up direction at the last 1h bar."""
        ...


class MlModelNotAttachedError(RuntimeError):
    """A strategy's model has no predictor attached in this process."""


_PROVIDERS: dict[tuple[str, int], PredictionProvider] = {}
_BOUND: ContextVar[Mapping[str, PredictionProvider] | None] = ContextVar(
    "hydra_ml_bound_providers", default=None
)


def attach_provider(model_name: str, version: int, provider: PredictionProvider) -> None:
    """Attach *provider* as the named model's *version* predictor (idempotent)."""
    key = (model_name, version)
    if key not in _PROVIDERS:
        _PROVIDERS[key] = provider


@contextmanager
def bound_providers(providers: Mapping[str, PredictionProvider]) -> Iterator[None]:
    """Resolve exactly these names to these predictors in the current context only."""
    token = _BOUND.set(providers)
    try:
        yield
    finally:
        _BOUND.reset(token)


def bound_provider(model_name: str) -> PredictionProvider | None:
    """The current context's bound predictor for the name, or None outside a binding."""
    bound = _BOUND.get()
    return None if bound is None else bound.get(model_name)


def provider_for(model_name: str) -> PredictionProvider:
    """The context's bound predictor, else the model's newest attached version.

    Raises :class:`MlModelNotAttachedError` when nothing is attached — the
    fail-closed refusal a session start surfaces as its own error. Inside a
    binding only the bound names resolve: a research job never falls back to
    whatever this process attached for someone else.
    """
    bound = _BOUND.get()
    if bound is not None:
        if model_name not in bound:
            raise MlModelNotAttachedError(f"model '{model_name}' is not bound for this run")
        return bound[model_name]
    attached = [(version, p) for (name, version), p in _PROVIDERS.items() if name == model_name]
    if not attached:
        raise MlModelNotAttachedError(
            f"no Production model '{model_name}' is attached in this process; "
            "the session-start validation attaches it before a session runs"
        )
    return max(attached, key=lambda pair: pair[0])[1]


def _reset_providers() -> None:
    """Test isolation: drop every attached predictor."""
    _PROVIDERS.clear()
