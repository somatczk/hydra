"""BacktestRunner: replays stored bars through a paper session's own components.

Each bar is published on a ``SessionRuntime`` bus exactly as the live feed publishes a closed
bar, so fills (market orders at the signal bar's close plus slippage, resting stops and OCOs on
later bars), sizing, pre-trade checks, protective exits, fees and the spot ledger are the paper
session's code. Breakers run on bar time with the drawdown tiers off (decision 2); daily-loss
halt and loss-streak pause stay on. Metric writes are muted for the whole run.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

from hydra.backtest.fills import CommissionConfig
from hydra.backtest.metrics import BacktestResult, Trade, TransactionRecord, calculate_metrics
from hydra.core import metrics
from hydra.core.config import HydraConfig
from hydra.core.events import BarEvent
from hydra.core.types import OHLCV, Direction, MarketType, Symbol, Timeframe
from hydra.data.bars import timeframe_seconds
from hydra.execution.client_factory import ClientFactory, validate_venue
from hydra.execution.paper_trading import PaperFill, PaperTradingExecutor
from hydra.execution.session_manager import TradingSession
from hydra.execution.session_wiring import SessionRuntime
from hydra.risk.market_rules import MarketRulesCache
from hydra.risk.pretrade import RiskConfig
from hydra.risk.service import RiskService
from hydra.risk.state import InMemoryRiskStateStore
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext

logger = logging.getLogger(__name__)

_MARKET_RULES = MarketRulesCache()
_ENGINE_CONFIG = HydraConfig.model_construct()
_NO_DRAWDOWN_TIERS = Decimal(1000)  # tier 1 needs a 200x drawdown: unreachable on spot
_YIELD_EVERY = 100


class StrategyError(RuntimeError):
    """The strategy raised during the run; the backtest has no result."""


class BacktestRunner:
    """Runs one strategy over stored bars on the paper session core."""

    def __init__(self, market_rules: MarketRulesCache | None = None) -> None:
        self._market_rules = market_rules or _MARKET_RULES

    async def run(
        self,
        strategy_class: type[BaseStrategy],
        strategy_config: StrategyConfig,
        bars: list[OHLCV],
        initial_capital: Decimal = Decimal("100000"),
        commission: CommissionConfig | None = None,
        symbol: str = "BTCUSDT",
        timeframe: Timeframe = Timeframe.H1,
        on_progress: Callable[[float], None] | None = None,
        *,
        warmup_bars: int = 0,
        # shortcut: fixed slippage, no participation cap or partials; revisit with P3 live fills
        slippage_pct: Decimal = Decimal("0.001"),
        risk_config: RiskConfig | None = None,
    ) -> BacktestResult:
        """Replay ``bars[warmup_bars:]``; the first ``warmup_bars`` only seed the context.

        Equity has one point per traded bar (the last after the end-of-data close-out) and
        ``Trade.pnl`` is net of both fees. Raises StrategyError when the strategy raises,
        UnsupportedVenueError for anything but Binance SPOT.
        """
        cfg = strategy_config.model_copy(
            update={
                "symbols": [symbol],
                "enabled": True,
                "timeframes": strategy_config.timeframes.model_copy(update={"primary": timeframe}),
            }
        )
        exchange_id = validate_venue(cfg.exchange.exchange_id, cfg.exchange.market_type)
        traded = bars[warmup_bars:]
        if not traded:
            return calculate_metrics(equity_curve=[initial_capital], trades=[], timeframe=timeframe)
        comm = commission or CommissionConfig()
        step = timedelta(seconds=timeframe_seconds(timeframe))
        now = [traded[0].timestamp]

        with metrics.muted():
            session_id = f"backtest-{uuid.uuid4()}"
            risk = RiskService(
                InMemoryRiskStateStore(
                    paper_capital=initial_capital,
                    config=replace(
                        risk_config or RiskConfig(), max_drawdown_pct=_NO_DRAWDOWN_TIERS
                    ),
                ),
                scope="backtest",
                market_rules=self._market_rules,
                clock=lambda: now[0],
            )
            await risk.load()
            await risk.recovery_complete()
            executor = PaperTradingExecutor(
                exchange_id=exchange_id,
                initial_balances={"USDT": initial_capital},
                slippage_pct=slippage_pct,
                strategy_id=cfg.id,
                session_id=session_id,
                maker_fee=comm.fee_rate(MarketType.SPOT, is_maker=True),
                taker_fee=comm.fee_rate(MarketType.SPOT, is_maker=False),
                market_rules=await self._market_rules.get(exchange_id, [symbol]),
            )
            failures: list[str] = []

            async def on_failure(_session: TradingSession, reason: str) -> None:
                failures.append(reason)

            runtime = SessionRuntime(
                TradingSession(
                    session_id=session_id,
                    strategy_id=cfg.id,
                    trading_mode="paper",
                    exchange_id=exchange_id,
                    symbols=[symbol],
                    timeframe=str(timeframe),
                    paper_capital=initial_capital,
                ),
                cfg,
                executor=executor,
                risk=risk,
                clients=ClientFactory(),
                hydra_config=_ENGINE_CONFIG,
                db_pool=None,
                required_history=int(
                    strategy_class(config=cfg, context=StrategyContext()).required_history
                ),
                on_failure=on_failure,
                clock=lambda: now[0].timestamp(),
            )
            await runtime.build(strategy_class)
            warm = bars[:warmup_bars][-(runtime.context.max_bars or warmup_bars) :]
            await runtime.engine.warm_up({(symbol, timeframe): warm} if warm else {})
            await risk.register_session(
                session_id,
                executor,
                allocated=initial_capital,
                symbols=[symbol],
                exchange_id=exchange_id,
                strategy_id=cfg.id,
            )
            await runtime.seed_context()
            await runtime.wire()
            if warm:
                now[0] = warm[-1].timestamp + step
                await runtime.prime({(symbol, timeframe): warm})

            equity: list[Decimal] = []
            timestamps: list[datetime] = []
            stopped_reason: str | None = None
            for i, bar in enumerate(traded):
                if i % _YIELD_EVERY == 0:
                    await asyncio.sleep(0)
                    if on_progress is not None:
                        on_progress(i / len(traded))
                now[0] = bar.timestamp + step
                await runtime.bus.publish(
                    BarEvent(
                        symbol=Symbol(symbol),
                        timeframe=timeframe,
                        ohlcv=bar,
                        exchange_id=exchange_id,
                    )
                )
                if runtime.engine.errors:
                    await runtime.shutdown()
                    raise StrategyError(runtime.engine.last_error)
                equity.append(await executor.get_equity())
                timestamps.append(bar.timestamp)
                if runtime.closing:
                    await asyncio.sleep(0)
                    stopped_reason = failures[0] if failures else "session_failed"
                    break

            await runtime.flatten()
            await runtime.shutdown()
            equity[-1] = await executor.get_equity()

        trades, transactions = _trades(executor.get_fills())
        result = calculate_metrics(
            equity_curve=equity,
            trades=trades,
            timeframe=timeframe,
            timestamps=timestamps,
            benchmark_prices=[b.close for b in traded[: len(equity)]],
            initial_capital=initial_capital,
        )
        result.stopped_reason = stopped_reason
        result.transactions = transactions
        return result


def _trades(fills: list[PaperFill]) -> tuple[list[Trade], list[TransactionRecord]]:
    """Round trips and per-fill transactions; a trade is one SELL against the open lot."""
    trades: list[Trade] = []
    transactions: list[TransactionRecord] = []
    held = Decimal("0")
    entry_time: datetime | None = None
    trade_id = 0
    for f in fills:
        if f.status != "FILLED":
            continue
        if f.side == "BUY":
            if held == 0:
                trade_id += 1
                entry_time = f.timestamp
            held += f.quantity
        else:
            held -= f.quantity
            gross = f.gross_pnl or Decimal("0")
            net = f.net_pnl if f.net_pnl is not None else gross - f.fee
            trades.append(
                Trade(
                    entry_time=entry_time or f.timestamp,
                    exit_time=f.timestamp,
                    symbol=f.symbol,
                    direction=str(Direction.LONG),
                    entry_price=f.price - gross / f.quantity,
                    exit_price=f.price,
                    quantity=f.quantity,
                    pnl=net,
                    fees=gross - net,
                )
            )
        transactions.append(
            {
                "trade_id": trade_id,
                "type": "entry" if f.side == "BUY" else "exit",
                "time": f.timestamp.isoformat(),
                "side": "Buy" if f.side == "BUY" else "Sell",
                "price": float(f.price),
                "quantity": float(f.quantity),
                "fee": float(f.fee),
                "pnl": None if f.side == "BUY" else float(trades[-1].pnl),
            }
        )
        if held == 0:
            entry_time = None
    return trades, transactions


if __name__ == "__main__":
    import asyncio
    import signal as _signal

    from hydra.core.logging import setup_logging

    setup_logging(level="INFO", log_format="json")

    async def _worker() -> None:
        """Backtest worker: waits for jobs (placeholder)."""
        stop = asyncio.Event()
        loop = asyncio.get_event_loop()
        for sig in (_signal.SIGTERM, _signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        logger.info("Backtest worker started, waiting for jobs...")
        await stop.wait()
        logger.info("Backtest worker shutting down.")

    asyncio.run(_worker())
