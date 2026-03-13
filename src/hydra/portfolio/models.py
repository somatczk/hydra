"""SQLAlchemy async models for the portfolio module.

Heavy imports (sqlalchemy) are deferred to avoid import-time side effects
when running in lightweight contexts (e.g. backtesting without a database).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Mapped


def _build_models() -> dict[str, Any]:
    """Lazily construct SQLAlchemy models.

    Called once on first access via the module-level helper ``get_models()``.
    This keeps ``import hydra.portfolio.models`` free of heavy imports.
    """
    from datetime import UTC
    from datetime import datetime as dt_cls
    from decimal import Decimal as Dec

    from sqlalchemy import DateTime, Index, Integer, Numeric, String
    from sqlalchemy.orm import DeclarativeBase, mapped_column

    class Base(DeclarativeBase):
        pass

    class PositionRecord(Base):
        __tablename__ = "positions"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        symbol: Mapped[str] = mapped_column(String(32), nullable=False)
        direction: Mapped[str] = mapped_column(String(8), nullable=False)
        quantity: Mapped[Dec] = mapped_column(Numeric(24, 8), nullable=False)
        avg_entry_price: Mapped[Dec] = mapped_column(Numeric(24, 8), nullable=False)
        unrealized_pnl: Mapped[Dec] = mapped_column(
            Numeric(24, 8), nullable=False, default=Dec("0")
        )
        realized_pnl: Mapped[Dec] = mapped_column(Numeric(24, 8), nullable=False, default=Dec("0"))
        strategy_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
        exchange_id: Mapped[str] = mapped_column(String(16), nullable=False)
        market_type: Mapped[str] = mapped_column(String(16), nullable=False, default="SPOT")
        entry_time: Mapped[dt_cls] = mapped_column(
            DateTime(timezone=True), nullable=False, default=lambda: dt_cls.now(UTC)
        )
        updated_at: Mapped[dt_cls] = mapped_column(
            DateTime(timezone=True),
            nullable=False,
            default=lambda: dt_cls.now(UTC),
            onupdate=lambda: dt_cls.now(UTC),
        )

        __table_args__ = (
            Index("ix_positions_symbol_exchange", "symbol", "exchange_id"),
            Index("ix_positions_strategy", "strategy_id"),
        )

    class TradeRecord(Base):
        __tablename__ = "trades"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        symbol: Mapped[str] = mapped_column(String(32), nullable=False)
        direction: Mapped[str] = mapped_column(String(8), nullable=False)
        entry_price: Mapped[Dec] = mapped_column(Numeric(24, 8), nullable=False)
        exit_price: Mapped[Dec] = mapped_column(Numeric(24, 8), nullable=False)
        quantity: Mapped[Dec] = mapped_column(Numeric(24, 8), nullable=False)
        pnl: Mapped[Dec] = mapped_column(Numeric(24, 8), nullable=False)
        fees: Mapped[Dec] = mapped_column(Numeric(24, 8), nullable=False, default=Dec("0"))
        funding_cost: Mapped[Dec] = mapped_column(Numeric(24, 8), nullable=False, default=Dec("0"))
        strategy_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
        exchange_id: Mapped[str] = mapped_column(String(16), nullable=False)
        entry_time: Mapped[dt_cls] = mapped_column(DateTime(timezone=True), nullable=False)
        exit_time: Mapped[dt_cls] = mapped_column(DateTime(timezone=True), nullable=False)

        __table_args__ = (
            Index("ix_trades_symbol_exchange", "symbol", "exchange_id"),
            Index("ix_trades_strategy", "strategy_id"),
            Index("ix_trades_exit_time", "exit_time"),
        )

    class BalanceSnapshot(Base):
        __tablename__ = "balance_snapshots"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        exchange_id: Mapped[str] = mapped_column(String(16), nullable=False)
        asset: Mapped[str] = mapped_column(String(16), nullable=False)
        free: Mapped[Dec] = mapped_column(Numeric(24, 8), nullable=False)
        locked: Mapped[Dec] = mapped_column(Numeric(24, 8), nullable=False, default=Dec("0"))
        total: Mapped[Dec] = mapped_column(Numeric(24, 8), nullable=False)
        timestamp: Mapped[dt_cls] = mapped_column(
            DateTime(timezone=True), nullable=False, default=lambda: dt_cls.now(UTC)
        )

        __table_args__ = (
            Index("ix_balance_snapshots_exchange_asset", "exchange_id", "asset"),
            Index("ix_balance_snapshots_timestamp", "timestamp"),
        )

    class DailyPnL(Base):
        __tablename__ = "daily_pnl"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        date: Mapped[dt_cls] = mapped_column(DateTime(timezone=True), nullable=False)
        realized_pnl: Mapped[Dec] = mapped_column(Numeric(24, 8), nullable=False)
        unrealized_pnl: Mapped[Dec] = mapped_column(Numeric(24, 8), nullable=False)
        total_pnl: Mapped[Dec] = mapped_column(Numeric(24, 8), nullable=False)
        fees: Mapped[Dec] = mapped_column(Numeric(24, 8), nullable=False, default=Dec("0"))
        funding: Mapped[Dec] = mapped_column(Numeric(24, 8), nullable=False, default=Dec("0"))
        strategy_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
        exchange_id: Mapped[str] = mapped_column(String(16), nullable=False)

        __table_args__ = (
            Index("ix_daily_pnl_date_exchange", "date", "exchange_id"),
            Index("ix_daily_pnl_strategy", "strategy_id"),
        )

    return {
        "Base": Base,
        "PositionRecord": PositionRecord,
        "TradeRecord": TradeRecord,
        "BalanceSnapshot": BalanceSnapshot,
        "DailyPnL": DailyPnL,
    }


# ---------------------------------------------------------------------------
# Public accessor
# ---------------------------------------------------------------------------

_models_cache: dict[str, Any] | None = None


def get_models() -> dict[str, Any]:
    """Return a dict of all SQLAlchemy model classes, building them lazily."""
    global _models_cache
    if _models_cache is None:
        _models_cache = _build_models()
    return _models_cache
