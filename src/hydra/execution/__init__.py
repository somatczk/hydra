"""M07: Order management, multi-exchange execution, paper trading."""

from __future__ import annotations

__all__ = [
    "ExchangeClient",
    "OrderManager",
    "PaperTradingExecutor",
]

_IMPORT_MAP: dict[str, tuple[str, str]] = {
    "ExchangeClient": ("hydra.execution.exchange_client", "ExchangeClient"),
    "OrderManager": ("hydra.execution.order_manager", "OrderManager"),
    "PaperTradingExecutor": ("hydra.execution.paper_trading", "PaperTradingExecutor"),
}


def __getattr__(name: str) -> object:
    if name in _IMPORT_MAP:
        module_path, attr = _IMPORT_MAP[name]
        import importlib

        mod = importlib.import_module(module_path)
        return getattr(mod, attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
