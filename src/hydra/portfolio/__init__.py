"""M09: Position tracking, PnL calculation, balance reconciliation."""

from __future__ import annotations

__all__ = [
    "BalanceReconciler",
    "Discrepancy",
    "PnLCalculator",
    "PositionTracker",
    "ReconciliationResult",
    "SyncAction",
    "SyncActionType",
]

_IMPORT_MAP: dict[str, tuple[str, str]] = {
    "PnLCalculator": ("hydra.portfolio.pnl", "PnLCalculator"),
    "PositionTracker": ("hydra.portfolio.positions", "PositionTracker"),
    "BalanceReconciler": ("hydra.portfolio.reconciliation", "BalanceReconciler"),
    "Discrepancy": ("hydra.portfolio.reconciliation", "Discrepancy"),
    "ReconciliationResult": ("hydra.portfolio.reconciliation", "ReconciliationResult"),
    "SyncAction": ("hydra.portfolio.reconciliation", "SyncAction"),
    "SyncActionType": ("hydra.portfolio.reconciliation", "SyncActionType"),
}


def __getattr__(name: str) -> object:
    if name in _IMPORT_MAP:
        module_path, attr = _IMPORT_MAP[name]
        import importlib

        mod = importlib.import_module(module_path)
        return getattr(mod, attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
