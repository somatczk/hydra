"""BacktestRunner: replays stored bars through a paper session's own components.

Each bar is published on a ``SessionRuntime`` bus exactly as the live feed publishes a closed
bar, so fills (market orders at the signal bar's close plus slippage, resting stops and OCOs on
later bars), sizing, pre-trade checks, protective exits, fees and the spot ledger are the paper
session's code. Every declared non-primary timeframe (``extra_bars``) is replayed on the same
merged stream the live feed delivers: ordered by each bar's close time, the higher timeframe
first on ties, so a confirmation bar closing at T is in the context before the primary bar
closing at T is evaluated and nothing closes in the future of what a decision saw. Breakers run
on bar time with the drawdown tiers off (decision 2); daily-loss halt and loss-streak pause stay
on. Metric writes are muted for the whole run.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal

from hydra.backtest.fills import CommissionConfig
from hydra.backtest.metrics import BacktestResult, Trade, TransactionRecord, calculate_metrics
from hydra.core import metrics
from hydra.core.config import HydraConfig
from hydra.core.events import BarEvent
from hydra.core.types import OHLCV, Direction, MarketType, Symbol, Timeframe
from hydra.data.bars import close_time, timeframe_seconds
from hydra.execution.client_factory import ClientFactory, validate_venue
from hydra.execution.fill_rules import DEFAULT_MAX_PARTICIPATION
from hydra.execution.paper_trading import PaperFill, PaperTradingExecutor
from hydra.execution.perp_trading import (
    FundingRow,
    PaperExecutor,
    PerpPaperTradingExecutor,
    static_funding_reader,
)
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


class _QuietWhileMuted(logging.Filter):
    """Per-bar rejections and breaker transitions of a backtest stay out of the engine's logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.ERROR or not metrics.is_muted()


for _name in (
    "hydra.execution.order_manager",
    "hydra.execution.session_wiring",
    "hydra.risk.circuit_breakers",
    "hydra.strategy.engine",
):
    logging.getLogger(_name).addFilter(_QuietWhileMuted())


def strategy_warmup(strategy_class: type[BaseStrategy], config: StrategyConfig) -> int:
    """The bars ``config`` needs before it can trade: its instance's ``required_history``.

    It includes a rule-based strategy's per-trial override headroom, so it is the one
    number every run uses both to size the context and to keep warm-up out of the metrics.
    """
    return int(strategy_class(config=config, context=StrategyContext()).required_history)


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
        slippage_pct: Decimal = Decimal("0.001"),
        risk_config: RiskConfig | None = None,
        funding: Mapping[str, Sequence[FundingRow]] | None = None,
        max_participation: Decimal = DEFAULT_MAX_PARTICIPATION,
        extra_bars: Mapping[Timeframe, Sequence[OHLCV]] | None = None,
    ) -> BacktestResult:
        """Replay ``bars[warmup_bars:]``; the first ``warmup_bars`` only seed the context.

        Metrics, equity and the benchmark cover the replayed bars only, so a caller with no
        bars before its window passes ``strategy_warmup`` as ``warmup_bars``; a window the
        warm-up consumes whole returns an empty result with no trades.

        Equity has one point per traded primary bar (the last after the end-of-data
        close-out) and ``Trade.pnl`` is net of both fees. Raises StrategyError when the
        strategy raises, UnsupportedVenueError for any pair ``validate_venue`` refuses —
        everything but Binance SPOT and Binance USD⌂-M FUTURES (``binanceusdm``). A
        FUTURES config runs on the perp paper executor (P4-C): the strategy's leverage,
        isolated margin, liquidation, and funding only from the rows *funding* carries
        (skip-when-absent); a SPOT config runs the spot executor exactly as before.
        ``extra_bars`` carries the declared non-primary timeframes for the same window;
        they seed the context in warm-up and are replayed merged with the primary stream
        (close-time order, higher timeframe first on ties), exactly as the live feed
        delivers them. Both executors cap every fill to ``max_participation`` of the
        bar's volume — the same default as a live paper session (parity).
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
            executor: PaperExecutor
            if cfg.exchange.market_type is MarketType.FUTURES:
                executor = PerpPaperTradingExecutor(
                    exchange_id=exchange_id,
                    initial_balances={"USDT": initial_capital},
                    slippage_pct=slippage_pct,
                    strategy_id=cfg.id,
                    session_id=session_id,
                    maker_fee=comm.fee_rate(MarketType.FUTURES, is_maker=True),
                    taker_fee=comm.fee_rate(MarketType.FUTURES, is_maker=False),
                    market_rules=await self._market_rules.get(exchange_id, [symbol]),
                    leverage=cfg.exchange.leverage,
                    funding_reader=static_funding_reader(funding or {}),
                    max_participation=max_participation,
                )
            else:
                executor = PaperTradingExecutor(
                    exchange_id=exchange_id,
                    initial_balances={"USDT": initial_capital},
                    slippage_pct=slippage_pct,
                    strategy_id=cfg.id,
                    session_id=session_id,
                    maker_fee=comm.fee_rate(MarketType.SPOT, is_maker=True),
                    taker_fee=comm.fee_rate(MarketType.SPOT, is_maker=False),
                    market_rules=await self._market_rules.get(exchange_id, [symbol]),
                    max_participation=max_participation,
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
                required_history=strategy_warmup(strategy_class, cfg),
                on_failure=on_failure,
                clock=lambda: now[0].timestamp(),
            )
            await runtime.build(strategy_class)
            warm = bars[:warmup_bars][-(runtime.context.max_bars or warmup_bars) :]
            seed = _warm_streams(
                symbol,
                timeframe,
                warm,
                traded[0].timestamp,
                extra_bars,
                runtime.context.max_bars,
            )
            await runtime.engine.warm_up(seed)
            await risk.register_session(
                session_id,
                executor,
                allocated=initial_capital,
                symbols=[symbol],
                exchange_id=exchange_id,
                strategy_id=cfg.id,
                leverage=cfg.exchange.leverage,
            )
            await runtime.seed_context()
            try:
                await runtime.wire()
            except Exception as exc:
                await runtime.shutdown()
                raise StrategyError(f"{cfg.id}: {exc!r}") from exc
            if warm:
                now[0] = warm[-1].timestamp + step
                await runtime.prime({(symbol, timeframe): warm})

            equity: list[Decimal] = []
            timestamps: list[datetime] = []
            stopped_reason: str | None = None
            merged = _merged_stream(symbol, timeframe, traded, extra_bars)
            done = 0
            for tf, bar in merged:
                if tf == timeframe:
                    if done % _YIELD_EVERY == 0:
                        await asyncio.sleep(0)
                        if on_progress is not None:
                            on_progress(done / len(traded))
                    done += 1
                now[0] = close_time(bar.timestamp, tf)
                await runtime.bus.publish(
                    BarEvent(
                        symbol=Symbol(symbol),
                        timeframe=tf,
                        ohlcv=bar,
                        exchange_id=exchange_id,
                    )
                )
                if runtime.engine.errors:
                    error = runtime.engine.last_error
                    await runtime.shutdown()
                    raise StrategyError(error)
                if tf != timeframe:
                    continue  # equity and decisions ride the primary stream only
                equity.append(await executor.get_equity())
                timestamps.append(bar.timestamp)
                if runtime.closing:
                    await asyncio.sleep(0)
                    stopped_reason = failures[0] if failures else "session_failed"
                    break

            await runtime.flatten()
            await runtime.shutdown()
            if runtime.engine.errors:
                raise StrategyError(runtime.engine.last_error)
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


def _merged_stream(
    symbol: str,
    primary: Timeframe,
    traded: Sequence[OHLCV],
    extras: Mapping[Timeframe, Sequence[OHLCV]] | None,
) -> list[tuple[Timeframe, OHLCV]]:
    """Every stream's bars for the traded window, in the live feed's delivery order.

    Ordered by each bar's close time with the higher timeframe first on ties — the
    exact ``_resync`` rule — so nothing is published before it closes (no lookahead)
    and a confirmation bar closing at T is in the context before the primary bar
    closing at T is evaluated. Extras beyond the primary window are clipped: the
    session the runner simulates never sees a bar closing after its last decision.
    """
    window_start = traded[0].timestamp
    last_close = close_time(traded[-1].timestamp, primary)
    out: list[tuple[Timeframe, OHLCV]] = [(primary, b) for b in traded]
    for tf, bars in (extras or {}).items():
        if tf == primary:
            continue
        out.extend(
            (tf, b) for b in bars if window_start < close_time(b.timestamp, tf) <= last_close
        )
    out.sort(key=lambda p: (close_time(p[1].timestamp, p[0]), -timeframe_seconds(p[0])))
    return out


def _warm_streams(
    symbol: str,
    primary: Timeframe,
    warm: list[OHLCV],
    window_start: datetime,
    extras: Mapping[Timeframe, Sequence[OHLCV]] | None,
    max_bars: int | None,
) -> dict[tuple[str, Timeframe], list[OHLCV]]:
    """The warm-up seed of every stream: history only, oldest first (the engine sorts)."""
    seed: dict[tuple[str, Timeframe], list[OHLCV]] = {}
    if warm:
        seed[(symbol, primary)] = warm
    for tf, bars in (extras or {}).items():
        if tf == primary:
            continue
        closed = [b for b in bars if close_time(b.timestamp, tf) <= window_start]
        if max_bars is not None:
            closed = closed[-max_bars:]
        if closed:
            seed[(symbol, tf)] = closed
    return seed


@dataclass(slots=True)
class _ExitSlices:
    """One logical exit (a single order's partial fills), accumulating into one trade."""

    direction: Direction
    symbol: str
    last_ts: datetime
    quantity: Decimal = Decimal("0")
    notional: Decimal = Decimal("0")
    gross: Decimal = Decimal("0")
    net: Decimal = Decimal("0")
    last_txn: int = -1

    def add(
        self, quantity: Decimal, price: Decimal, gross: Decimal, net: Decimal, ts: datetime
    ) -> None:
        self.quantity += quantity
        self.notional += quantity * price
        self.gross += gross
        self.net += net
        self.last_ts = ts

    def vwap(self) -> Decimal:
        return self.notional / self.quantity


def _trades(fills: list[PaperFill]) -> tuple[list[Trade], list[TransactionRecord]]:
    """Round trips and per-fill transactions, long or short (a SELL can open a short).

    A fill that closes position carries ``net_pnl`` (both executors' convention: opening
    fills never do) — that is the exit test, so a perp short's SELL entry is an entry.
    The direction is the sign held between entry and exit. FUNDING accruals ride the
    account but are not order fills and are skipped. One logical exit's slices — the
    partial fills of a single order the volume model spread over bars — aggregate into
    ONE trade: summed quantity, volume-weighted exit price, summed fees and PnL.
    """
    trades: list[Trade] = []
    transactions: list[TransactionRecord] = []
    held = Decimal("0")
    entry_time: datetime | None = None
    trade_id = 0
    exits: dict[str, _ExitSlices] = {}
    exit_order: list[str] = []

    def _flush() -> None:
        nonlocal entry_time
        for order_id in exit_order:
            slices = exits.pop(order_id)
            sign = Decimal("1") if slices.direction == Direction.LONG else Decimal("-1")
            vwap = slices.vwap()
            trades.append(
                Trade(
                    entry_time=entry_time or slices.last_ts,
                    exit_time=slices.last_ts,
                    symbol=slices.symbol,
                    direction=str(slices.direction),
                    entry_price=vwap - sign * slices.gross / slices.quantity,
                    exit_price=vwap,
                    quantity=slices.quantity,
                    pnl=slices.net,
                    fees=slices.gross - slices.net,
                )
            )
            transactions[slices.last_txn]["pnl"] = float(slices.net)
        exit_order.clear()
        entry_time = None

    for f in fills:
        if f.status != "FILLED" or f.side == "FUNDING":
            continue
        signed = f.quantity if f.side == "BUY" else -f.quantity
        if held == 0:
            trade_id += 1
            entry_time = f.timestamp
        closing = f.net_pnl is not None
        held_before = held
        held += signed
        if closing:
            direction = Direction.LONG if held_before > 0 else Direction.SHORT
            gross = f.gross_pnl or Decimal("0")
            net = f.net_pnl if f.net_pnl is not None else gross - f.fee
            slices = exits.get(f.order_id)
            if slices is None:
                slices = _ExitSlices(direction, f.symbol, f.timestamp)
                exits[f.order_id] = slices
                exit_order.append(f.order_id)
            slices.add(f.quantity, f.price, gross, net, f.timestamp)
        transactions.append(
            {
                "trade_id": trade_id,
                "type": "exit" if closing else "entry",
                "time": f.timestamp.isoformat(),
                "side": "Buy" if f.side == "BUY" else "Sell",
                "price": float(f.price),
                "quantity": float(f.quantity),
                "fee": float(f.fee),
                "pnl": None,
            }
        )
        if closing:
            exits[f.order_id].last_txn = len(transactions) - 1
        if held == 0 or (held_before != 0 and (held > 0) != (held_before > 0)):
            _flush()
            if held != 0:
                # a flip opened the opposite position with this same fill
                entry_time = f.timestamp
    _flush()  # an exit the flattened run never fully closed still reports
    return trades, transactions
