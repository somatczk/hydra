"""M08: Pre-trade risk checks, position sizing, circuit breakers, account RiskService."""

from __future__ import annotations

__all__ = [
    "CircuitBreakerManager",
    "InMemoryRiskStateStore",
    "MarketRules",
    "MarketRulesCache",
    "PgRiskStateStore",
    "PortfolioState",
    "PositionSizer",
    "PreTradeRiskManager",
    "RiskService",
    "size_entry",
]

_IMPORT_MAP: dict[str, tuple[str, str]] = {
    "CircuitBreakerManager": ("hydra.risk.circuit_breakers", "CircuitBreakerManager"),
    "InMemoryRiskStateStore": ("hydra.risk.state", "InMemoryRiskStateStore"),
    "MarketRules": ("hydra.risk.market_rules", "MarketRules"),
    "MarketRulesCache": ("hydra.risk.market_rules", "MarketRulesCache"),
    "PgRiskStateStore": ("hydra.risk.state", "PgRiskStateStore"),
    "PortfolioState": ("hydra.risk.pretrade", "PortfolioState"),
    "PreTradeRiskManager": ("hydra.risk.pretrade", "PreTradeRiskManager"),
    "PositionSizer": ("hydra.risk.sizing", "PositionSizer"),
    "RiskService": ("hydra.risk.service", "RiskService"),
    "size_entry": ("hydra.risk.sizing", "size_entry"),
}


def __getattr__(name: str) -> object:
    if name in _IMPORT_MAP:
        module_path, attr = _IMPORT_MAP[name]
        import importlib

        mod = importlib.import_module(module_path)
        return getattr(mod, attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
