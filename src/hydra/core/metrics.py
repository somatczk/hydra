"""Prometheus metric contract for the Hydra engine.

Every metric the engine exports is declared once, in ``CONTRACT``. Producers only
call the setter helpers below; they never create collectors. A metric listed here
with a later workstream as producer is defined now so that rules, Grafana panels
and producers agree on one name, type, label set and unit. No label set is
pre-initialised. Unlabelled counters and histograms register at init, because 0 is
their true value at startup and rate() then sees the first increment. Every other
family registers on its first write, so a gauge never reports a made-up 0 before
its producer has set it.

Units are in the name (``_seconds``, ``_ratio``, ``_timestamp_seconds``) except
USDT amounts and base-asset quantities, which are noted below. Timestamps are
Unix seconds; Prometheus rules compare them against ``time()``, so a frozen
process cannot hide a stale feed.

Contract (name: type, unit / labels / producer):

- hydra_build_info: gauge, 1
  labels: version
  producer: core.metrics at init
- hydra_trades_total: counter, fills
  labels: symbol, side, strategy_id, exchange_id
  producer: OrderManager, once per fill
- hydra_trade_pnl: histogram, USDT
  labels: symbol, strategy_id
  producer: OrderManager, net PnL of closing fills (PaperFill.net_pnl)
- hydra_position_size: gauge, base asset
  labels: session_id, strategy_id, exchange_id, symbol
  producer: P1-E session_wiring on fill
- hydra_portfolio_value: gauge, USDT
  labels: -
  producer: P1-C RiskService (account equity)
- hydra_drawdown_ratio: gauge, ratio
  labels: -
  producer: P1-C RiskService
- hydra_daily_pnl: gauge, USDT
  labels: -
  producer: P1-C RiskService
- hydra_portfolio_heat_ratio: gauge, ratio
  labels: -
  producer: P1-C RiskService
- hydra_signal_count: counter, signals
  labels: strategy_id, signal_type
  producer: StrategyEngine
- hydra_spot_short_ignored_total: counter, signals
  labels: strategy_id, signal_type
  producer: P1-E signal path
- hydra_entries_off_schedule_total: counter, signals
  labels: strategy_id
  producer: StrategyEngine
- hydra_event_bus_latency_seconds: histogram, seconds
  labels: -
  producer: InMemoryEventBus.publish
- hydra_event_handler_errors_total: counter, errors
  labels: event_type
  producer: P1-B event bus isolation
- hydra_exchange_api_latency_seconds: histogram, seconds
  labels: exchange_id, endpoint, outcome
  producer: ExchangeClient
- hydra_exchange_api_errors_total: counter, errors
  labels: exchange_id, endpoint, error_type
  producer: ExchangeClient
- hydra_ws_reconnects_total: counter, reconnects
  labels: exchange_id
  producer: feed, ExchangeClient
- hydra_order_fill_latency_seconds: histogram, seconds
  labels: -
  producer: OrderManager
- hydra_ml_inference_latency_seconds: histogram, seconds
  labels: model_name
  producer: ml.serving
- hydra_ml_overlay_decisions_total: counter, decisions
  labels: strategy_id, model_name, decision
  producer: ml.consumers (the P5-C overlay seam)
- hydra_ml_drift_psi: gauge, PSI
  labels: model_name
  producer: trader main, the startup drift task (P5-D)
- hydra_ml_drift_nan_ratio: gauge, ratio
  labels: model_name
  producer: trader main, the startup drift task (P5-D)
- hydra_feed_last_message_timestamp_seconds: gauge, unix seconds
  labels: exchange_id, symbol, timeframe
  producer: P1-B feed
- hydra_feed_last_closed_bar_timestamp_seconds: gauge, unix seconds
  labels: exchange_id, symbol, timeframe
  producer: P1-B feed
- hydra_feed_bar_interval_seconds: gauge, seconds
  labels: exchange_id, symbol, timeframe
  producer: P1-B feed
- hydra_feed_bars_total: counter, bars
  labels: exchange_id, symbol, timeframe, source
  producer: P1-B feed
- hydra_feed_bars_rejected_total: counter, bars
  labels: exchange_id, symbol, reason
  producer: P1-B feed
- hydra_feed_anomalies_total: counter, bars
  labels: exchange_id, symbol, kind
  producer: P1-B feed
- hydra_feed_persist_errors_total: counter, errors
  labels: exchange_id
  producer: P1-B feed
- hydra_circuit_breaker_active_tier: gauge, tier
  labels: scope
  producer: P1-C RiskService
- hydra_kill_switch_active: gauge, 1
  labels: -
  producer: P1-C RiskService
- hydra_risk_rejections_total: counter, orders
  labels: check, intent
  producer: P1-C OrderManager
- hydra_risk_state_persist_failures_total: counter, errors
  labels: -
  producer: P1-C RiskService
- hydra_risk_lock_wait_seconds: histogram, seconds
  labels: -
  producer: P1-C RiskService
- hydra_risk_lock_timeouts_total: counter, orders
  labels: -
  producer: P1-C RiskService
- hydra_session_desired: gauge, 1
  labels: session_id, strategy_id, mode
  producer: P1-G1 HealthMonitor
- hydra_session_supervised: gauge, 1
  labels: session_id, strategy_id, mode
  producer: P1-G1 HealthMonitor
- hydra_sessions_errored_total: counter, sessions
  labels: strategy_id
  producer: P1-E
- hydra_paper_persist_dirty: gauge, 1
  labels: session_id
  producer: P1-D/P1-E paper executor
- hydra_live_protective_gap: gauge, 1
  labels: session_id, strategy_id, exchange_id, symbol
  producer: P3-B session_wiring live coverage hook
- hydra_live_reconcile_state: gauge, 0 ok | 1 mismatch | 2 unknown | 3 never
  labels: session_id
  producer: P3-A2 reconcile loop
- hydra_ready: gauge, 1
  labels: check
  producer: P1-G1 HealthMonitor
- hydra_health_monitor_last_run_timestamp_seconds: gauge, unix seconds
  labels: -
  producer: P1-G1 HealthMonitor
- hydra_data_cache_bytes: gauge, bytes
  labels: -
  producer: P6-C2 DataCacheSweeper
- hydra_data_cache_rows: gauge, rows
  labels: -
  producer: P6-C2 DataCacheSweeper
- hydra_data_cache_pairs: gauge, pairs
  labels: -
  producer: P6-C2 DataCacheSweeper
- hydra_data_cache_budget_bytes: gauge, bytes
  labels: -
  producer: P6-C2 DataCacheSweeper
- hydra_data_sweep_last_timestamp_seconds: gauge, unix seconds
  labels: -
  producer: P6-C2 DataCacheSweeper
- hydra_data_sweep_errors_total: counter, errors
  labels: -
  producer: P6-C2 DataCacheSweeper
- hydra_data_sweep_deleted_rows_total: counter, rows
  labels: -
  producer: P6-C2 DataCacheSweeper
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

from prometheus_client import REGISTRY, Counter, Gauge, Histogram

MetricKind = Literal["counter", "gauge", "histogram"]

EXCHANGE_CALL_OUTCOMES = ("ok", "error", "timeout", "cancelled")
FEED_BAR_SOURCES = ("ws", "rest_gap", "warmup")
RISK_INTENTS = ("open", "reduce")
# trader: the split-API monitor's remote-trader check (D5)
READY_CHECKS = ("db", "migrations", "sessions", "feeds", "risk", "trader")
SPOT_SHORT_SIGNALS = ("entry_short", "exit_short")
CIRCUIT_BREAKER_TIERS = range(5)

_MAX_UNIX_SECONDS = 1e11


@dataclass(frozen=True)
class MetricSpec:
    """One exported metric family: what it is called, measures and who writes it."""

    name: str
    kind: MetricKind
    help: str
    unit: str
    producer: str
    labels: tuple[str, ...] = ()
    buckets: tuple[float, ...] = ()

    @property
    def samples(self) -> tuple[str, ...]:
        """Series names Prometheus stores for this family (what PromQL can select)."""
        if self.kind == "counter":
            return (self.name if self.name.endswith("_total") else f"{self.name}_total",)
        if self.kind == "histogram":
            suffixes = (
                ("_bucket", "_count") if self.buckets[0] < 0 else ("_bucket", "_count", "_sum")
            )
            return tuple(self.name + s for s in suffixes)
        return (self.name,)


_LATENCY_BUCKETS_EXCHANGE = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_FEED_LABELS = ("exchange_id", "symbol", "timeframe")
_SESSION_LABELS = ("session_id", "strategy_id", "mode")

CONTRACT: tuple[MetricSpec, ...] = (
    MetricSpec(
        "hydra_build_info",
        "gauge",
        "Always 1; the version label is the running hydra release",
        "1",
        "core.metrics at init",
        ("version",),
    ),
    MetricSpec(
        "hydra_trades_total",
        "counter",
        "Executed fills by symbol, side, strategy and exchange",
        "fills",
        "OrderManager, once per fill",
        ("symbol", "side", "strategy_id", "exchange_id"),
    ),
    MetricSpec(
        "hydra_trade_pnl",
        "histogram",
        "Net PnL in USDT of each closing fill, fees included; opening fills are not observed "
        "(no _sum series: the buckets are negative)",
        "USDT",
        "OrderManager, net PnL of closing fills (PaperFill.net_pnl)",
        ("symbol", "strategy_id"),
        (-500, -200, -100, -50, -20, -10, 0, 10, 20, 50, 100, 200, 500, 1000),
    ),
    MetricSpec(
        "hydra_position_size",
        "gauge",
        "Open position size in base-asset units per session, strategy, exchange and symbol",
        "base asset",
        "P1-E session_wiring on fill",
        ("session_id", "strategy_id", "exchange_id", "symbol"),
    ),
    MetricSpec(
        "hydra_portfolio_value",
        "gauge",
        "Paper account equity in USDT",
        "USDT",
        "P1-C RiskService (account equity)",
    ),
    MetricSpec(
        "hydra_drawdown_ratio",
        "gauge",
        "Account drawdown from peak equity as a fraction (0.05 = 5%)",
        "ratio",
        "P1-C RiskService",
    ),
    MetricSpec(
        "hydra_daily_pnl",
        "gauge",
        "Account PnL since 00:00 UTC in USDT, realized plus unrealized",
        "USDT",
        "P1-C RiskService",
    ),
    MetricSpec(
        "hydra_portfolio_heat_ratio",
        "gauge",
        "Gross open exposure across sessions divided by account equity",
        "ratio",
        "P1-C RiskService",
    ),
    MetricSpec(
        "hydra_signal_count",
        "counter",
        "Signals published by strategy and signal type (exported as hydra_signal_count_total)",
        "signals",
        "StrategyEngine",
        ("strategy_id", "signal_type"),
    ),
    MetricSpec(
        "hydra_spot_short_ignored_total",
        "counter",
        "Short signals dropped before OrderManager because the session trades spot",
        "signals",
        "P1-E signal path",
        ("strategy_id", "signal_type"),
    ),
    MetricSpec(
        "hydra_entries_off_schedule_total",
        "counter",
        "Entry signals dropped before publication because the bar is outside the schedule",
        "signals",
        "StrategyEngine",
        ("strategy_id",),
    ),
    MetricSpec(
        "hydra_event_bus_latency_seconds",
        "histogram",
        "Time to run every subscriber callback for one published in-memory event",
        "seconds",
        "InMemoryEventBus.publish",
        (),
        (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
    ),
    MetricSpec(
        "hydra_event_handler_errors_total",
        "counter",
        "Event-bus subscriber callbacks that raised, by event type",
        "errors",
        "P1-B event bus isolation",
        ("event_type",),
    ),
    MetricSpec(
        "hydra_exchange_api_latency_seconds",
        "histogram",
        "Latency of outbound exchange REST calls, every outcome (ok, error, timeout, cancelled)",
        "seconds",
        "ExchangeClient",
        ("exchange_id", "endpoint", "outcome"),
        _LATENCY_BUCKETS_EXCHANGE,
    ),
    MetricSpec(
        "hydra_exchange_api_errors_total",
        "counter",
        "Outbound exchange REST calls that raised or timed out, by exception class",
        "errors",
        "ExchangeClient",
        ("exchange_id", "endpoint", "error_type"),
    ),
    MetricSpec(
        "hydra_ws_reconnects_total",
        "counter",
        "WebSocket exchange instances recreated after repeated errors",
        "reconnects",
        "feed, ExchangeClient",
        ("exchange_id",),
    ),
    MetricSpec(
        "hydra_order_fill_latency_seconds",
        "histogram",
        "Time from OrderManager submit to the fill being recorded",
        "seconds",
        "OrderManager",
        (),
        (0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
    ),
    MetricSpec(
        "hydra_ml_inference_latency_seconds",
        "histogram",
        "Latency of ML model inference",
        "seconds",
        "ml.serving",
        ("model_name",),
        (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5),
    ),
    MetricSpec(
        "hydra_ml_overlay_decisions_total",
        "counter",
        "ML overlay decisions at the signal seam: pass, veto, shadow_veto or "
        "error (error counts the fail-closed refusals)",
        "decisions",
        "ml.consumers (the P5-C overlay seam)",
        ("strategy_id", "model_name", "decision"),
    ),
    MetricSpec(
        "hydra_ml_drift_psi",
        "gauge",
        "The startup drift measurement's max per-feature quantile-PSI of a "
        "Production model's current window against its stored reference deciles",
        "PSI",
        "trader main, the startup drift task (P5-D)",
        ("model_name",),
    ),
    MetricSpec(
        "hydra_ml_drift_nan_ratio",
        "gauge",
        "The worst per-feature NaN rate of the same drift window (a fully-NaN "
        "feature scores 1 even at PSI 0)",
        "ratio",
        "trader main, the startup drift task (P5-D)",
        ("model_name",),
    ),
    MetricSpec(
        "hydra_feed_last_message_timestamp_seconds",
        "gauge",
        "Wall-clock Unix time of the last market-data message for a stream",
        "unix seconds",
        "P1-B feed",
        _FEED_LABELS,
    ),
    MetricSpec(
        "hydra_feed_last_closed_bar_timestamp_seconds",
        "gauge",
        "Open time (exchange clock, Unix seconds) of the last closed bar emitted for a stream",
        "unix seconds",
        "P1-B feed",
        _FEED_LABELS,
    ),
    MetricSpec(
        "hydra_feed_bar_interval_seconds",
        "gauge",
        "Bar length of a stream, for closed-bar staleness rules",
        "seconds",
        "P1-B feed",
        _FEED_LABELS,
    ),
    MetricSpec(
        "hydra_feed_bars_total",
        "counter",
        "Closed bars delivered by source (ws, rest_gap, warmup)",
        "bars",
        "P1-B feed",
        (*_FEED_LABELS, "source"),
    ),
    MetricSpec(
        "hydra_feed_bars_rejected_total",
        "counter",
        "Closed bars dropped by ingest validation, by reason",
        "bars",
        "P1-B feed",
        ("exchange_id", "symbol", "reason"),
    ),
    MetricSpec(
        "hydra_feed_anomalies_total",
        "counter",
        "Closed bars flagged as anomalous but kept, by kind",
        "bars",
        "P1-B feed",
        ("exchange_id", "symbol", "kind"),
    ),
    MetricSpec(
        "hydra_feed_persist_errors_total",
        "counter",
        "Failed attempts to persist closed bars to ts.ohlcv_1m",
        "errors",
        "P1-B feed",
        ("exchange_id",),
    ),
    MetricSpec(
        "hydra_circuit_breaker_active_tier",
        "gauge",
        "Active drawdown circuit-breaker tier, 0 (none) to 4",
        "tier",
        "P1-C RiskService",
        ("scope",),
    ),
    MetricSpec(
        "hydra_kill_switch_active",
        "gauge",
        "1 while the kill switch is latched",
        "1",
        "P1-C RiskService",
    ),
    MetricSpec(
        "hydra_risk_rejections_total",
        "counter",
        "Orders rejected by risk checks, by check name and intent (open, reduce)",
        "orders",
        "P1-C OrderManager",
        ("check", "intent"),
    ),
    MetricSpec(
        "hydra_risk_state_persist_failures_total",
        "counter",
        "Failed writes of the persisted risk state",
        "errors",
        "P1-C RiskService",
    ),
    MetricSpec(
        "hydra_risk_lock_wait_seconds",
        "histogram",
        "Time an order waited for the RiskService lock, which is held across create_order",
        "seconds",
        "P1-C RiskService",
        (),
        (0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    ),
    MetricSpec(
        "hydra_risk_lock_timeouts_total",
        "counter",
        "Orders refused because the RiskService lock was not acquired in time",
        "orders",
        "P1-C RiskService",
    ),
    MetricSpec(
        "hydra_session_desired",
        "gauge",
        "1 when the database marks a trading session as running",
        "1",
        "P1-G1 HealthMonitor",
        _SESSION_LABELS,
    ),
    MetricSpec(
        "hydra_session_supervised",
        "gauge",
        "1 when a running trading session has a live task in this process",
        "1",
        "P1-G1 HealthMonitor",
        _SESSION_LABELS,
    ),
    MetricSpec(
        "hydra_sessions_errored_total",
        "counter",
        "Trading sessions that transitioned to error",
        "sessions",
        "P1-E",
        ("strategy_id",),
    ),
    MetricSpec(
        "hydra_paper_persist_dirty",
        "gauge",
        "1 while a paper session holds fills not yet persisted (new entries refused)",
        "1",
        "P1-D/P1-E paper executor",
        ("session_id",),
    ),
    MetricSpec(
        "hydra_live_protective_gap",
        "gauge",
        "1 while a live session holds a position with no confirmed exchange-side protective order",
        "1",
        "P3-B session_wiring live coverage hook",
        ("session_id", "strategy_id", "exchange_id", "symbol"),
    ),
    MetricSpec(
        "hydra_live_reconcile_state",
        "gauge",
        "A live session's reconcile state: 0 ok, 1 mismatch, 2 unknown, 3 never",
        "0 ok | 1 mismatch | 2 unknown | 3 never",
        "P3-A2 reconcile loop",
        ("session_id",),
    ),
    MetricSpec(
        "hydra_ready",
        "gauge",
        "1 when a readiness check passes (db, migrations, sessions, feeds, risk, trader)",
        "1",
        "P1-G1 HealthMonitor",
        ("check",),
    ),
    MetricSpec(
        "hydra_health_monitor_last_run_timestamp_seconds",
        "gauge",
        "Wall-clock Unix time the health monitor last completed a run",
        "unix seconds",
        "P1-G1 HealthMonitor",
    ),
    MetricSpec(
        "hydra_data_cache_bytes",
        "gauge",
        "Estimated bytes of auto-fetched cached bars (rows x data.row_bytes_estimate)",
        "bytes",
        "P6-C2 DataCacheSweeper",
    ),
    MetricSpec(
        "hydra_data_cache_rows",
        "gauge",
        "Auto-fetched bar rows held by every cached dataset",
        "rows",
        "P6-C2 DataCacheSweeper",
    ),
    MetricSpec(
        "hydra_data_cache_pairs",
        "gauge",
        "Distinct exchange and symbol pairs with cached auto-fetched data",
        "pairs",
        "P6-C2 DataCacheSweeper",
    ),
    MetricSpec(
        "hydra_data_cache_budget_bytes",
        "gauge",
        "The configured cache budget in bytes that sweeps hold the auto cache under",
        "bytes",
        "P6-C2 DataCacheSweeper",
    ),
    MetricSpec(
        "hydra_data_sweep_last_timestamp_seconds",
        "gauge",
        "Wall-clock Unix time the cache sweeper last completed a cycle",
        "unix seconds",
        "P6-C2 DataCacheSweeper",
    ),
    MetricSpec(
        "hydra_data_sweep_errors_total",
        "counter",
        "Cache sweep cycles that failed or were cancelled after hanging past their interval",
        "errors",
        "P6-C2 DataCacheSweeper",
    ),
    MetricSpec(
        "hydra_data_sweep_deleted_rows_total",
        "counter",
        "Auto-fetched bar rows deleted by cache sweeps",
        "rows",
        "P6-C2 DataCacheSweeper",
    ),
)

_SPECS: dict[str, MetricSpec] = {spec.name: spec for spec in CONTRACT}
_collectors: dict[str, Counter | Gauge | Histogram] = {}
_session_positions: dict[str, dict[tuple[str, str, str], float]] = {}
_live_gaps: dict[str, set[tuple[str, str, str]]] = {}
_initialized: bool = False

_wall_clock: Callable[[], float] = time.time
_monotonic: Callable[[], float] = time.monotonic


def _build(spec: MetricSpec) -> Counter | Gauge | Histogram:
    if spec.kind == "counter":
        return Counter(spec.name, spec.help, labelnames=spec.labels)
    if spec.kind == "gauge":
        return Gauge(spec.name, spec.help, labelnames=spec.labels)
    return Histogram(spec.name, spec.help, labelnames=spec.labels, buckets=spec.buckets)


def _ensure_initialized() -> None:
    """Export ``hydra_build_info`` and the unlabelled counters and histograms at 0."""
    global _initialized
    if _initialized:
        return
    _initialized = True

    from hydra import __version__

    _gauge("hydra_build_info").labels(version=__version__).set(1)
    for spec in CONTRACT:
        if not spec.labels and spec.kind != "gauge":
            _collector(spec.name)


class _Muted:
    """Stands in for every collector inside ``muted()``: each write is dropped."""

    def labels(self, *values: str, **labels: str) -> _Muted:
        return self

    def inc(self, amount: float = 1) -> None:
        return None

    def dec(self, amount: float = 1) -> None:
        return None

    def set(self, value: float) -> None:
        return None

    def observe(self, amount: float) -> None:
        return None

    def remove(self, *values: str) -> None:
        return None


_MUTED = _Muted()
_muted: ContextVar[bool] = ContextVar("hydra_metrics_muted", default=False)


@contextlib.contextmanager
def muted() -> Iterator[None]:
    """Drop every metric write made in this context (task and thread local): backtests."""
    token = _muted.set(True)
    try:
        yield
    finally:
        _muted.reset(token)


def is_muted() -> bool:
    return _muted.get()


def _collector(name: str) -> Counter | Gauge | Histogram:
    _ensure_initialized()
    collector = _collectors.get(name)
    if collector is None:
        collector = _collectors[name] = _build(_SPECS[name])
    return collector


def _counter(name: str) -> Counter | _Muted:
    if _muted.get():
        return _MUTED
    collector = _collector(name)
    if not isinstance(collector, Counter):
        raise TypeError(f"{name} is not a counter")
    return collector


def _gauge(name: str) -> Gauge | _Muted:
    if _muted.get():
        return _MUTED
    collector = _collector(name)
    if not isinstance(collector, Gauge):
        raise TypeError(f"{name} is not a gauge")
    return collector


def _histogram(name: str) -> Histogram | _Muted:
    if _muted.get():
        return _MUTED
    collector = _collector(name)
    if not isinstance(collector, Histogram):
        raise TypeError(f"{name} is not a histogram")
    return collector


def _check(value: str, allowed: tuple[str, ...], label: str) -> str:
    if value not in allowed:
        raise ValueError(f"{label}={value!r} not in {allowed}")
    return value


def _unix_seconds(value: float, what: str) -> float:
    if not 0 <= value < _MAX_UNIX_SECONDS:
        raise ValueError(f"{what} must be Unix seconds, got {value}")
    return value


# ---------------------------------------------------------------------------
# Trading and portfolio
# ---------------------------------------------------------------------------


def record_trade(
    symbol: str,
    side: str,
    strategy_id: str,
    exchange_id: str,
    pnl: float | None = None,
) -> None:
    """Count one fill and, when given, observe its realized PnL in USDT."""
    _counter("hydra_trades_total").labels(
        symbol=symbol, side=side, strategy_id=strategy_id, exchange_id=exchange_id
    ).inc()
    if pnl is not None:
        _histogram("hydra_trade_pnl").labels(symbol=symbol, strategy_id=strategy_id).observe(pnl)


def update_position(symbol: str, exchange_id: str, size: float) -> None:
    """Deprecated no-op: a value without a session label is last-writer-wins across sessions.

    Kept so the legacy call sites still import; P1-E calls ``set_session_position``.
    """


def set_session_position(
    session_id: str, strategy_id: str, exchange_id: str, symbol: str, size: float
) -> None:
    """Set one session's open position size in base-asset units."""
    _gauge("hydra_position_size").labels(
        session_id=session_id, strategy_id=strategy_id, exchange_id=exchange_id, symbol=symbol
    ).set(size)
    if not _muted.get():
        _session_positions.setdefault(session_id, {})[(strategy_id, exchange_id, symbol)] = size


def update_portfolio(value: float, drawdown_ratio: float, daily_pnl: float) -> None:
    """Set account equity (USDT), drawdown (fraction) and daily PnL (USDT).

    Equity and daily PnL are set even when the drawdown is rejected as out of range.
    """
    _gauge("hydra_portfolio_value").set(value)
    _gauge("hydra_daily_pnl").set(daily_pnl)
    if not 0 <= drawdown_ratio <= 1:
        raise ValueError(f"drawdown_ratio must be a fraction in [0, 1], got {drawdown_ratio}")
    _gauge("hydra_drawdown_ratio").set(drawdown_ratio)


def set_portfolio_heat(ratio: float) -> None:
    """Set gross open exposure divided by account equity."""
    if ratio < 0:
        raise ValueError(f"heat ratio must be >= 0, got {ratio}")
    _gauge("hydra_portfolio_heat_ratio").set(ratio)


def record_signal(strategy_id: str, signal_type: str) -> None:
    """Count one published strategy signal."""
    _counter("hydra_signal_count").labels(strategy_id=strategy_id, signal_type=signal_type).inc()


def record_spot_short_ignored(strategy_id: str, signal_type: str) -> None:
    """Count a short signal dropped on a spot session."""
    _check(signal_type, SPOT_SHORT_SIGNALS, "signal_type")
    _counter("hydra_spot_short_ignored_total").labels(
        strategy_id=strategy_id, signal_type=signal_type
    ).inc()


def record_entry_off_schedule(strategy_id: str) -> None:
    """Count an entry signal dropped outside its strategy's schedule."""
    _counter("hydra_entries_off_schedule_total").labels(strategy_id=strategy_id).inc()


# ---------------------------------------------------------------------------
# Event bus, exchange and ML latency
# ---------------------------------------------------------------------------


def observe_event_bus_latency(seconds: float) -> None:
    """Record the time spent running one event's subscriber callbacks."""
    _histogram("hydra_event_bus_latency_seconds").observe(seconds)


def record_event_handler_error(event_type: str) -> None:
    """Count one subscriber callback that raised."""
    _counter("hydra_event_handler_errors_total").labels(event_type=event_type).inc()


def _call_outcome(exc: BaseException) -> str:
    if not isinstance(exc, Exception):
        return "cancelled"
    if isinstance(exc, TimeoutError) or any(
        cls.__name__ == "RequestTimeout" for cls in type(exc).__mro__
    ):
        return "timeout"
    return "error"


@contextlib.contextmanager
def observe_exchange_call(exchange_id: str, endpoint: str) -> Iterator[None]:
    """Time one outbound exchange call; the latency is recorded for every outcome.

    Errors and timeouts also increment ``hydra_exchange_api_errors_total`` with the
    exception class name. The exception always propagates.
    """
    latency = _histogram("hydra_exchange_api_latency_seconds")
    started = _monotonic()
    outcome = "cancelled"
    try:
        yield
        outcome = "ok"
    except BaseException as exc:
        outcome = _call_outcome(exc)
        if outcome in ("error", "timeout"):
            _counter("hydra_exchange_api_errors_total").labels(
                exchange_id=exchange_id, endpoint=endpoint, error_type=type(exc).__name__
            ).inc()
        raise
    finally:
        latency.labels(exchange_id=exchange_id, endpoint=endpoint, outcome=outcome).observe(
            _monotonic() - started
        )


def record_ws_reconnect(exchange_id: str) -> None:
    """Count one WebSocket exchange instance recreation."""
    _counter("hydra_ws_reconnects_total").labels(exchange_id=exchange_id).inc()


def observe_order_fill_latency(seconds: float) -> None:
    """Record submit-to-fill latency."""
    _histogram("hydra_order_fill_latency_seconds").observe(seconds)


def observe_ml_inference_latency(model_name: str, seconds: float) -> None:
    """Record ML model inference latency."""
    _histogram("hydra_ml_inference_latency_seconds").labels(model_name=model_name).observe(seconds)


OVERLAY_DECISIONS = ("pass", "veto", "shadow_veto", "error")


def record_ml_overlay_decision(strategy_id: str, model_name: str, decision: str) -> None:
    """Count one overlay decision at the signal seam (P5-C)."""
    _check(decision, OVERLAY_DECISIONS, "decision")
    _counter("hydra_ml_overlay_decisions_total").labels(
        strategy_id=strategy_id, model_name=model_name, decision=decision
    ).inc()


def set_ml_drift(model_name: str, psi: float, nan_ratio: float) -> None:
    """Set one Production model's measured drift PSI and worst-feature NaN rate (P5-D).

    The trader's startup drift task is the only producer; ``hydra.ml`` never
    writes these gauges (§8).
    """
    if psi < 0:
        raise ValueError(f"psi must be >= 0, got {psi}")
    if not 0 <= nan_ratio <= 1:
        raise ValueError(f"nan_ratio must be a fraction in [0, 1], got {nan_ratio}")
    _gauge("hydra_ml_drift_psi").labels(model_name=model_name).set(psi)
    _gauge("hydra_ml_drift_nan_ratio").labels(model_name=model_name).set(nan_ratio)


# ---------------------------------------------------------------------------
# Market-data feed
# ---------------------------------------------------------------------------


def mark_feed_message(
    exchange_id: str, symbol: str, timeframe: str, *, at: float | None = None
) -> None:
    """Record that a market-data message arrived now (or at ``at``, Unix seconds)."""
    ts = _unix_seconds(_wall_clock() if at is None else at, "at")
    _gauge("hydra_feed_last_message_timestamp_seconds").labels(
        exchange_id=exchange_id, symbol=symbol, timeframe=timeframe
    ).set(ts)


def mark_closed_bar(exchange_id: str, symbol: str, timeframe: str, bar_open_ts: float) -> None:
    """Record the open time (Unix seconds, not ms) of the newest closed bar."""
    _gauge("hydra_feed_last_closed_bar_timestamp_seconds").labels(
        exchange_id=exchange_id, symbol=symbol, timeframe=timeframe
    ).set(_unix_seconds(bar_open_ts, "bar_open_ts"))


def set_feed_bar_interval(exchange_id: str, symbol: str, timeframe: str, seconds: float) -> None:
    """Publish a stream's bar length so closed-bar rules can scale with it."""
    if seconds <= 0:
        raise ValueError(f"bar interval must be > 0, got {seconds}")
    _gauge("hydra_feed_bar_interval_seconds").labels(
        exchange_id=exchange_id, symbol=symbol, timeframe=timeframe
    ).set(seconds)


def forget_feed(exchange_id: str, symbol: str, timeframe: str) -> None:
    """Remove a stopped stream's freshness series so no stale alert fires for it."""
    for name in (
        "hydra_feed_last_message_timestamp_seconds",
        "hydra_feed_last_closed_bar_timestamp_seconds",
        "hydra_feed_bar_interval_seconds",
    ):
        _gauge(name).remove(exchange_id, symbol, timeframe)


def record_feed_bar(exchange_id: str, symbol: str, timeframe: str, source: str) -> None:
    """Count one delivered closed bar by source (ws, rest_gap, warmup)."""
    _check(source, FEED_BAR_SOURCES, "source")
    _counter("hydra_feed_bars_total").labels(
        exchange_id=exchange_id, symbol=symbol, timeframe=timeframe, source=source
    ).inc()


def record_feed_bar_rejected(exchange_id: str, symbol: str, reason: str) -> None:
    """Count one closed bar dropped by validation."""
    _counter("hydra_feed_bars_rejected_total").labels(
        exchange_id=exchange_id, symbol=symbol, reason=reason
    ).inc()


def record_feed_anomaly(exchange_id: str, symbol: str, kind: str) -> None:
    """Count one closed bar flagged anomalous."""
    _counter("hydra_feed_anomalies_total").labels(
        exchange_id=exchange_id, symbol=symbol, kind=kind
    ).inc()


def record_feed_persist_error(exchange_id: str) -> None:
    """Count one failed closed-bar persist attempt."""
    _counter("hydra_feed_persist_errors_total").labels(exchange_id=exchange_id).inc()


# ---------------------------------------------------------------------------
# Auto-fetched data cache (P6-C2)
# ---------------------------------------------------------------------------


def set_data_cache(rows: int, bytes: int, pairs: int, budget_bytes: int) -> None:
    """Set the cache snapshot a sweep leaves behind: rows, estimated bytes, pairs, budget."""
    for name, value in (
        ("rows", rows),
        ("bytes", bytes),
        ("pairs", pairs),
        ("budget_bytes", budget_bytes),
    ):
        if value < 0:
            raise ValueError(f"{name} must be >= 0, got {value}")
    _gauge("hydra_data_cache_rows").set(rows)
    _gauge("hydra_data_cache_bytes").set(bytes)
    _gauge("hydra_data_cache_pairs").set(pairs)
    _gauge("hydra_data_cache_budget_bytes").set(budget_bytes)


def mark_data_sweep(*, at: float | None = None) -> None:
    """Record that the cache sweeper completed a cycle now (or at ``at``)."""
    ts = _unix_seconds(_wall_clock() if at is None else at, "at")
    _gauge("hydra_data_sweep_last_timestamp_seconds").set(ts)


def record_data_sweep_error() -> None:
    """Count one cache sweep cycle that failed or hung past its interval."""
    _counter("hydra_data_sweep_errors_total").inc()


def record_data_sweep_deleted(rows: int) -> None:
    """Count the auto-fetched bar rows one sweep deleted."""
    if rows < 0:
        raise ValueError(f"rows must be >= 0, got {rows}")
    _counter("hydra_data_sweep_deleted_rows_total").inc(rows)


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------


def set_circuit_breaker_tier(scope: str, tier: int) -> None:
    """Set the active circuit-breaker tier (0 = none, 4 = highest)."""
    if tier not in CIRCUIT_BREAKER_TIERS:
        raise ValueError(f"tier must be 0-4, got {tier}")
    _gauge("hydra_circuit_breaker_active_tier").labels(scope=scope).set(tier)


def set_kill_switch_active(active: bool) -> None:
    """Set 1 while the kill switch is latched, else 0."""
    _gauge("hydra_kill_switch_active").set(1.0 if active else 0.0)


def record_risk_rejection(check: str, intent: str) -> None:
    """Count one order rejected by the named risk check."""
    _check(intent, RISK_INTENTS, "intent")
    _counter("hydra_risk_rejections_total").labels(check=check, intent=intent).inc()


def record_risk_state_persist_failure() -> None:
    """Count one failed risk-state write."""
    _counter("hydra_risk_state_persist_failures_total").inc()


def observe_risk_lock_wait(seconds: float) -> None:
    """Record how long an order waited for the RiskService lock."""
    _histogram("hydra_risk_lock_wait_seconds").observe(seconds)


def record_risk_lock_timeout() -> None:
    """Count one order refused because the RiskService lock timed out."""
    _counter("hydra_risk_lock_timeouts_total").inc()


# ---------------------------------------------------------------------------
# Sessions and health
# ---------------------------------------------------------------------------


def set_session_state(
    session_id: str, strategy_id: str, mode: str, *, desired: bool, supervised: bool
) -> None:
    """Set whether a session should run (DB) and whether this process runs it."""
    labels = {"session_id": session_id, "strategy_id": strategy_id, "mode": mode}
    _gauge("hydra_session_desired").labels(**labels).set(1.0 if desired else 0.0)
    _gauge("hydra_session_supervised").labels(**labels).set(1.0 if supervised else 0.0)


def forget_session(session_id: str, strategy_id: str, mode: str) -> None:
    """Remove every per-session series of a session that is neither desired nor running.

    A live protective-gap series is kept while the session still held a position at its last
    fill (R13): an unprotected remainder must keep paging until an operator resolves it.
    """
    _gauge("hydra_session_desired").remove(session_id, strategy_id, mode)
    _gauge("hydra_session_supervised").remove(session_id, strategy_id, mode)
    _gauge("hydra_paper_persist_dirty").remove(session_id)
    with contextlib.suppress(KeyError):
        _gauge("hydra_live_reconcile_state").remove(session_id)
    positions = _gauge("hydra_position_size")
    held = _session_positions.pop(session_id, {})
    for position_strategy, exchange_id, symbol in held:
        positions.remove(session_id, position_strategy, exchange_id, symbol)
    gaps = _live_gaps.pop(session_id, set())
    if not any(held.values()):
        for gap_strategy, exchange_id, symbol in gaps:
            with contextlib.suppress(KeyError):
                _gauge("hydra_live_protective_gap").remove(
                    session_id, gap_strategy, exchange_id, symbol
                )


def record_session_errored(strategy_id: str) -> None:
    """Count one session transition to error."""
    _counter("hydra_sessions_errored_total").labels(strategy_id=strategy_id).inc()


def set_paper_persist_dirty(session_id: str, dirty: bool) -> None:
    """Set 1 while a paper session has unpersisted fills."""
    _gauge("hydra_paper_persist_dirty").labels(session_id=session_id).set(1.0 if dirty else 0.0)


def set_live_protective_gap(
    session_id: str, strategy_id: str, exchange_id: str, symbol: str, gap: bool
) -> None:
    """Set 1 while a live position has no confirmed exchange-side stop; else clear it."""
    labels = (strategy_id, exchange_id, symbol)
    if gap:
        _gauge("hydra_live_protective_gap").labels(
            session_id=session_id, strategy_id=strategy_id, exchange_id=exchange_id, symbol=symbol
        ).set(1)
        if not _muted.get():
            _live_gaps.setdefault(session_id, set()).add(labels)
        return
    with contextlib.suppress(KeyError):
        _gauge("hydra_live_protective_gap").remove(session_id, *labels)
    _live_gaps.get(session_id, set()).discard(labels)


LIVE_RECONCILE_STATES = {"ok": 0.0, "mismatch": 1.0, "unknown": 2.0, "never": 3.0}


def set_live_reconcile_state(session_id: str, state: str) -> None:
    """Set a live session's reconcile state (0 ok, 1 mismatch, 2 unknown, 3 never).

    The mismatch value is the alert's; ``never`` is exported so an un-reconciled session
    is visible as such, not as a missing series.
    """
    try:
        value = LIVE_RECONCILE_STATES[state]
    except KeyError:
        raise ValueError(f"unknown reconcile state {state!r}") from None
    _gauge("hydra_live_reconcile_state").labels(session_id=session_id).set(value)


def set_ready(check: str, ok: bool) -> None:
    """Set one readiness check result."""
    _check(check, READY_CHECKS, "check")
    _gauge("hydra_ready").labels(check=check).set(1.0 if ok else 0.0)


def mark_health_monitor_run(*, at: float | None = None) -> None:
    """Record that the health monitor completed a run now (or at ``at``)."""
    ts = _unix_seconds(_wall_clock() if at is None else at, "at")
    _gauge("hydra_health_monitor_last_run_timestamp_seconds").set(ts)


def update_reconciliation(exchange_id: str, symbol: str, mismatch: bool) -> None:
    """Deprecated no-op: nothing reconciles against the exchange until P3.

    Kept so the legacy call site still imports; exporting its hard-coded 0 would
    claim a reconciliation that never ran.
    """


# ---------------------------------------------------------------------------
# Registry reset, used only in tests
# ---------------------------------------------------------------------------


def _reset_metrics() -> None:
    """Unregister every collector. For test isolation only."""
    global _initialized
    for collector in _collectors.values():
        with contextlib.suppress(KeyError):
            REGISTRY.unregister(collector)
    _collectors.clear()
    _session_positions.clear()
    _live_gaps.clear()
    _initialized = False
