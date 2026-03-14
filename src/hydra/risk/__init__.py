"""M08: Pre-trade risk checks, position sizing, circuit breakers."""

from __future__ import annotations

__all__ = [
    "CircuitBreakerManager",
    "ExchangeSafetyManager",
    "PortfolioState",
    "PositionSizer",
    "PreTradeRiskManager",
]

_IMPORT_MAP: dict[str, tuple[str, str]] = {
    "CircuitBreakerManager": ("hydra.risk.circuit_breakers", "CircuitBreakerManager"),
    "ExchangeSafetyManager": ("hydra.risk.exchange_safety", "ExchangeSafetyManager"),
    "PortfolioState": ("hydra.risk.pretrade", "PortfolioState"),
    "PreTradeRiskManager": ("hydra.risk.pretrade", "PreTradeRiskManager"),
    "PositionSizer": ("hydra.risk.sizing", "PositionSizer"),
}


def __getattr__(name: str) -> object:
    if name in _IMPORT_MAP:
        module_path, attr = _IMPORT_MAP[name]
        import importlib

        mod = importlib.import_module(module_path)
        return getattr(mod, attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
