"""Live market-data feed over ccxt.pro: closed bars only, each delivered once and in order.

Per exchange the feed runs one watch loop (``watch_ohlcv_for_symbols`` over every symbol and
timeframe), one dispatcher that drains a bounded queue into the event bus, one watchdog, one
REST gap-retry task and, with a repository, one persistence task. A failing subscriber never
tears down the WebSocket; a subscriber slower than the bar rate back-pressures the watch loop
once the queue is full, and the feed reports ``consumer_lag``.

- ``ClosedBarAssembler`` emits candle T when a later candle arrives (ccxt.pro drops
  Binance's closed flag). Closed bars are validated, counted, persisted with an upsert into
  ``ts.ohlcv_1m`` under the config symbol and published as ``BarEvent``.
- When timeframes of one symbol close at the same boundary, the higher timeframe is
  published first; a bar waits at most ``mtf_barrier_s`` for its siblings (never blocking).
- After any WebSocket error the held candles are dropped and, before the next watch, the
  missed closed bars are fetched over REST and delivered with ``replay=True`` except the
  newest per stream. Replay bars are persisted always but published only with
  ``publish_replay=True``. A range REST cannot fill marks the stream stale (``gap_unfilled``)
  and is retried in the background while live bars keep flowing.
- The watchdog publishes ``FeedStatusEvent('stale')`` on WebSocket silence (and forces a
  reconnect), an overdue closed bar or a lagging consumer, and ``'live'`` once recovered.
- ``warm_up`` returns stored history (topped up over REST) for the strategy context.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from hydra.core import metrics
from hydra.core.events import BarEvent, Event, FeedStatusEvent, PriceEvent
from hydra.core.logging import get_logger
from hydra.core.types import OHLCV, ExchangeId, Symbol, Timeframe
from hydra.data.backfill import error_is, fetch_closed_bars
from hydra.data.bars import (
    ClosedBarAssembler,
    close_time,
    last_closed_open,
    timeframe_seconds,
)
from hydra.data.normalizer import DataNormalizer

if TYPE_CHECKING:
    from hydra.data.storage import MarketDataRepository

logger = get_logger(__name__)

# Reconnect backoff parameters
_INITIAL_BACKOFF_S = 1.0
_MAX_BACKOFF_S = 60.0
_BACKOFF_FACTOR = 2.0
_STABLE_THRESHOLD_S = 30.0  # sustained success before resetting backoff
_MAX_CONSECUTIVE_ERRORS = 5  # recreate the exchange instance after this many failures
_RATE_LIMIT_BACKOFF_S = 60.0
_PRICE_INTERVAL_S = 1.0
_PERSIST_BACKOFF_S = 0.5
_PERSIST_BACKOFF_MAX_S = 30.0
_INLINE_REST_RETRIES = 0  # the watch loop never waits out a REST backoff
_GAP_RETRY_INITIAL_S = 5.0
_GAP_RETRY_MAX_S = 300.0
_GAP_EMPTY_ATTEMPTS = 5  # REST answered without the bars this often: the exchange has none
_PERSIST_BATCH = 500
_PERSIST_DRAIN_S = 5.0

type Sleep = Callable[[float], Awaitable[None]]
type Clock = Callable[[], float]
type RestFactory = Callable[[str], Any]
type StreamKey = tuple[str, Timeframe]
type StatusKey = tuple[str, Timeframe | None]
type PersistRow = tuple[str, str, Timeframe, OHLCV]

_EXCHANGE_SCOPE: StatusKey = ("", None)

# Feeds of every session share the freshness gauges; forget them only with the last owner.
_STREAM_OWNERS: Counter[tuple[str, str, str]] = Counter()


class InsufficientHistoryError(Exception):
    """Fewer closed bars are available than the strategy needs before it may trade."""

    def __init__(
        self, exchange_id: str, symbol: str, timeframe: Timeframe, have: int, need: int
    ) -> None:
        self.exchange_id = exchange_id
        self.symbol = symbol
        self.timeframe = timeframe
        self.have = have
        self.need = need
        super().__init__(
            f"{exchange_id} {symbol} {timeframe}: {have} closed bars available, {need} required"
        )


def _default_rest_factory(exchange_id: str) -> Any:
    import ccxt.async_support as ccxt_async

    cls = getattr(ccxt_async, exchange_id)
    return cls(
        {
            "enableRateLimit": True,
            "options": {"defaultType": "spot", "fetchMarkets": {"types": ["spot"]}},
        }
    )


def resolve_spot_symbol(markets: dict[str, Any], symbol: str, exchange_id: str = "") -> str:
    """Unified spot symbol for a config symbol (exchange id like ``BTCUSDT`` or unified)."""
    matches = {
        m["symbol"]
        for m in markets.values()
        if m.get("spot") is True and symbol in (m.get("id"), m.get("symbol"))
    }
    if len(matches) != 1:
        found = sorted(matches) or "no spot market"
        msg = f"{exchange_id}: symbol {symbol!r} must match exactly one spot market, got {found}"
        raise ValueError(msg)
    unified = matches.pop()
    if markets[unified].get("active") is False:
        msg = f"{exchange_id}: spot market {unified} is not active"
        raise ValueError(msg)
    return unified


def _reject_reason(errors: list[str]) -> str:
    text = " ".join(errors)
    if "volume" in text:
        return "negative_volume"
    if "timezone" in text:
        return "no_timezone"
    return "ohlc_inconsistent"


def _anomaly_kind(description: str) -> str:
    for prefix, kind in (
        ("price spike", "price_spike"),
        ("zero volume", "zero_volume"),
        ("negative volume", "negative_volume"),
        ("timestamp", "timestamp_not_advancing"),
    ):
        if description.startswith(prefix):
            return kind
    return "other"


@dataclass(eq=False)
class _Gap:
    start: datetime
    end: datetime
    empty_answers: int = 0


@dataclass(eq=False)
class _Stream:
    symbol: str
    unified: str
    timeframe: Timeframe
    tf_s: int
    assembler: ClosedBarAssembler
    started_at: float
    delivered: datetime | None = None
    newest: datetime | None = None
    previous: OHLCV | None = None
    refill_from: datetime | None = None
    gaps: list[_Gap] = field(default_factory=list)

    def next_missing(self) -> datetime | None:
        """Open time of the first bar not yet delivered, if known."""
        if self.delivered is not None:
            return self.delivered + timedelta(seconds=self.tf_s)
        return self.refill_from


@dataclass(eq=False)
class _Parked:
    deadline: float
    events: list[tuple[int, BarEvent]] = field(default_factory=list)


@dataclass(eq=False)
class _Feed:
    exchange_id: ExchangeId
    streams: dict[StreamKey, _Stream]
    by_unified: dict[tuple[str, str], _Stream]
    queue: asyncio.Queue[tuple[float, Event]]
    persist_queue: asyncio.Queue[PersistRow]
    markets: dict[str, Any]
    last_message: float
    resync_needed: bool = False
    reconnect_requested: bool = False
    last_forced_close: float | None = None
    stale: dict[StatusKey, set[str]] = field(default_factory=dict)
    parked: dict[tuple[str, datetime], _Parked] = field(default_factory=dict)
    last_price_at: dict[str, float] = field(default_factory=dict)
    inflight_since: float | None = None
    pending_watch: asyncio.Future[Any] | None = None
    outbox: deque[FeedStatusEvent] = field(default_factory=deque)
    gap_wake: asyncio.Event = field(default_factory=asyncio.Event)
    persist_inflight: int = 0

    @property
    def subscriptions(self) -> list[list[str]]:
        return [[s.unified, str(s.timeframe)] for s in self.streams.values()]

    @property
    def min_tf_s(self) -> int:
        return min(s.tf_s for s in self.streams.values())


class ExchangeFeedManager:
    """Live closed-bar feed for one or more exchanges (see the module docstring).

    Parameters
    ----------
    event_bus:
        An object satisfying the ``EventBus`` protocol (``publish()`` method).
    normalizer:
        ``DataNormalizer`` for conversion, validation and anomaly checks.
    exchange_factories:
        Exchange id -> callable returning a ccxt.pro instance (the WebSocket client).
    repository:
        Where closed bars are upserted and warm-up history is read; ``None`` disables both.
    rest_factory:
        Exchange id -> ccxt async REST instance for warm-up and gap fills. Defaults to a
        rate-limited spot instance.
    history_window:
        Bars returned per stream by ``warm_up`` (at least ``min_bars``, see ``window_for``).
    publish_replay:
        Publish catch-up bars (``replay=True``) to the bus. Off by default: they are still
        persisted, but a session must ignore replay bars in every bar handler before it opts in.
    stale_after_bars, ws_silence_s, watchdog_interval_s, mtf_barrier_s, queue_maxsize:
        Watchdog and dispatch thresholds.
    clock, sleep:
        Wall clock in Unix seconds and the backoff sleep (injectable for tests).
    """

    def __init__(
        self,
        event_bus: Any,
        normalizer: DataNormalizer | None = None,
        exchange_factories: dict[str, Any] | None = None,
        *,
        repository: MarketDataRepository | None = None,
        rest_factory: RestFactory | None = None,
        history_window: int = 1000,
        publish_replay: bool = False,
        stale_after_bars: int = 2,
        ws_silence_s: float = 60.0,
        watchdog_interval_s: float = 5.0,
        mtf_barrier_s: float = 5.0,
        queue_maxsize: int = 1024,
        clock: Clock = time.time,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._event_bus = event_bus
        self._normalizer = normalizer or DataNormalizer()
        self._exchange_factories = exchange_factories or {}
        self._exchanges: dict[str, Any] = {}
        self._tasks: dict[str, list[asyncio.Task[None]]] = {}
        self._running: dict[ExchangeId, bool] = {}
        self._feeds: dict[str, _Feed] = {}
        self._rest: dict[str, Any] = {}
        self._rest_markets: dict[str, dict[str, Any]] = {}
        self._warm_tails: dict[tuple[str, str, Timeframe], datetime] = {}
        self._repository = repository
        self._rest_factory = rest_factory or _default_rest_factory
        self._history_window = history_window
        self._publish_replay = publish_replay
        self._stale_after_bars = stale_after_bars
        self._ws_silence_s = ws_silence_s
        self._watchdog_interval_s = watchdog_interval_s
        self._mtf_barrier_s = mtf_barrier_s
        self._queue_maxsize = queue_maxsize
        self._clock = clock
        self._sleep = sleep

    @property
    def history_window(self) -> int:
        """Default bars per stream that ``warm_up`` loads."""
        return self._history_window

    def window_for(self, min_bars: int) -> int:
        """Bars per stream ``warm_up(min_bars=...)`` returns; size the live context to it."""
        return max(self._history_window, min_bars)

    def persist_backlog(self, exchange_id: str) -> int:
        """Accepted closed bars not yet written to the repository."""
        feed = self._feeds.get(exchange_id)
        if feed is None:
            return 0
        return feed.persist_queue.qsize() + feed.persist_inflight

    def is_live(self, exchange_id: str) -> bool:
        """True while connected with no stale reason in any scope (entries may run)."""
        return exchange_id in self._feeds and not self.stale_reasons(exchange_id)

    def stale_reasons(self, exchange_id: str) -> dict[str, list[str]]:
        """Current stale reasons by scope (``"*"`` = whole exchange, else ``symbol/tf``)."""
        feed = self._feeds.get(exchange_id)
        if feed is None:
            return {}
        return {
            "*" if key == _EXCHANGE_SCOPE else f"{key[0]}/{key[1]}": sorted(reasons)
            for key, reasons in feed.stale.items()
            if reasons
        }

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def connect(
        self,
        exchange_id: ExchangeId,
        symbols: list[str],
        timeframes: list[Timeframe],
    ) -> None:
        """Resolve the spot markets and start the watch, dispatch and watchdog tasks.

        Raises ``ValueError`` when a symbol is not exactly one active spot market.
        """
        if self._running.get(exchange_id, False):
            logger.warning("Already connected", exchange=exchange_id)
            return

        exchange = await self._get_or_create_exchange(exchange_id)
        try:
            markets = dict(await exchange.load_markets())
            feed = self._build_feed(exchange_id, markets, symbols, timeframes)
        except BaseException:
            self._exchanges.pop(exchange_id, None)
            with contextlib.suppress(Exception):
                await exchange.close()
            raise

        for stream in feed.streams.values():
            _STREAM_OWNERS[(exchange_id, stream.symbol, str(stream.timeframe))] += 1
            metrics.set_feed_bar_interval(
                exchange_id, stream.symbol, str(stream.timeframe), stream.tf_s
            )
        self._feeds[exchange_id] = feed
        self._running[exchange_id] = True
        tasks = [
            asyncio.create_task(self._watch_loop(feed), name=f"ohlcv-{exchange_id}"),
            asyncio.create_task(self._dispatch_loop(feed), name=f"dispatch-{exchange_id}"),
            asyncio.create_task(self._watchdog_loop(feed), name=f"watchdog-{exchange_id}"),
            asyncio.create_task(self._gap_loop(feed), name=f"gaps-{exchange_id}"),
        ]
        if self._repository is not None:
            tasks.append(
                asyncio.create_task(self._persist_loop(feed), name=f"persist-{exchange_id}")
            )
        self._tasks[exchange_id] = tasks

        logger.info(
            "Exchange feed started",
            exchange=exchange_id,
            symbols=symbols,
            timeframes=[str(tf) for tf in timeframes],
            persisting=self._repository is not None,
        )

    async def disconnect(self, exchange_id: ExchangeId) -> None:
        """Stop the feed: cancel its tasks, persist bars already accepted, close clients."""
        self._running[exchange_id] = False

        tasks = self._tasks.pop(exchange_id, [])
        persist = [t for t in tasks if t.get_name().startswith("persist-")]
        for task in tasks:
            if task not in persist:
                task.cancel()
        feed = self._feeds.get(exchange_id)
        if persist and feed is not None:
            try:
                await asyncio.wait_for(feed.persist_queue.join(), _PERSIST_DRAIN_S)
            except TimeoutError:
                logger.error(
                    "Feed stopped with unpersisted closed bars",
                    exchange=exchange_id,
                    pending=self.persist_backlog(exchange_id),
                )
        for task in persist:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        for instance in (self._exchanges.pop(exchange_id, None), self._rest.pop(exchange_id, None)):
            if instance is not None and hasattr(instance, "close"):
                with contextlib.suppress(Exception):
                    await instance.close()
        self._rest_markets.pop(exchange_id, None)

        feed = self._feeds.pop(exchange_id, None)
        if feed is not None:
            for stream in feed.streams.values():
                key = (exchange_id, stream.symbol, str(stream.timeframe))
                _STREAM_OWNERS[key] -= 1
                if _STREAM_OWNERS[key] <= 0:
                    del _STREAM_OWNERS[key]
                    metrics.forget_feed(*key)

        logger.info("Exchange feed disconnected", exchange=exchange_id)

    async def disconnect_all(self) -> None:
        """Shutdown all exchange connections."""
        exchange_ids: list[ExchangeId] = list(self._running.keys())
        for eid in exchange_ids:
            await self.disconnect(eid)
        for rest_id in list(self._rest):
            rest = self._rest.pop(rest_id)
            with contextlib.suppress(Exception):
                await rest.close()

    # ------------------------------------------------------------------
    # Warm-up
    # ------------------------------------------------------------------

    async def warm_up(
        self,
        symbols: Sequence[str],
        timeframes: Sequence[Timeframe],
        min_bars: int,
        *,
        exchange_id: ExchangeId = "binance",
    ) -> dict[tuple[str, Timeframe], list[OHLCV]]:
        """Closed history per (symbol, timeframe), ending at the newest closed bar.

        Reads ``window_for(min_bars)`` stored bars, REST-fills the window from its first
        missing bar (a stale tail or an interior hole), upserts the fill and reads the window
        back, so the context holds exactly the stored rows a backtest would read. Without a
        repository the REST bars are returned unpersisted. Raises ``InsufficientHistoryError``
        below ``min_bars``.
        """
        window = self.window_for(min_bars)
        now = datetime.fromtimestamp(self._clock(), UTC)
        out: dict[tuple[str, Timeframe], list[OHLCV]] = {}
        if self._repository is None:
            logger.warning(
                "Feed has no repository; warm-up bars are fetched over REST and not persisted",
                exchange=exchange_id,
            )
        for symbol in symbols:
            for timeframe in dict.fromkeys(timeframes):
                bars = await self._warm_stream(exchange_id, symbol, timeframe, window, now)
                if len(bars) < min_bars:
                    raise InsufficientHistoryError(
                        exchange_id, symbol, timeframe, len(bars), min_bars
                    )
                out[(symbol, timeframe)] = bars
                if bars:
                    self._seed_tail(exchange_id, symbol, timeframe, bars[-1].timestamp)
                    for _ in bars:
                        metrics.record_feed_bar(exchange_id, symbol, str(timeframe), "warmup")
                    metrics.mark_closed_bar(
                        exchange_id, symbol, str(timeframe), bars[-1].timestamp.timestamp()
                    )
        return out

    async def _warm_stream(
        self,
        exchange_id: ExchangeId,
        symbol: str,
        timeframe: Timeframe,
        window: int,
        now: datetime,
    ) -> list[OHLCV]:
        end = last_closed_open(now, timeframe)
        tf = timedelta(seconds=timeframe_seconds(timeframe))
        first = end - (window - 1) * tf
        repo = self._repository
        stored: list[OHLCV] = []
        if repo is not None:
            stored = await repo.get_latest_bars(exchange_id, symbol, timeframe, window, end)
        have = {b.timestamp for b in stored}
        start = next((first + i * tf for i in range(window) if first + i * tf not in have), None)
        if start is None:
            return stored
        fetched = await self._fetch_rest(exchange_id, symbol, timeframe, start, end, now)
        valid = [b for b in fetched if self._valid(exchange_id, symbol, b)]
        logger.info(
            "Warm-up REST fill",
            exchange=exchange_id,
            symbol=symbol,
            timeframe=str(timeframe),
            stored=len(stored),
            fetched=len(valid),
            start=start.isoformat(),
            end=end.isoformat(),
        )
        if repo is None:
            return valid[-window:]
        if valid:
            await repo.upsert_closed_bars([(exchange_id, symbol, timeframe, b) for b in valid])
        bars = await repo.get_latest_bars(exchange_id, symbol, timeframe, window, end)
        holes = window - sum(1 for b in bars if b.timestamp >= first)
        if holes:
            logger.warning(
                "Warm-up window still has missing bars after the REST fill",
                exchange=exchange_id,
                symbol=symbol,
                timeframe=str(timeframe),
                missing=holes,
            )
        return bars

    def _seed_tail(
        self, exchange_id: ExchangeId, symbol: str, timeframe: Timeframe, tail: datetime
    ) -> None:
        self._warm_tails[(exchange_id, symbol, timeframe)] = tail
        feed = self._feeds.get(exchange_id)
        stream = feed.streams.get((symbol, timeframe)) if feed else None
        if feed is not None and stream is not None:
            stream.assembler.seed(tail)
            if stream.delivered is None or tail > stream.delivered:
                stream.delivered = tail
            if stream.newest is None or tail > stream.newest:
                stream.newest = tail
            feed.resync_needed = True

    # ------------------------------------------------------------------
    # Internal: exchange lifecycle
    # ------------------------------------------------------------------

    async def _get_or_create_exchange(self, exchange_id: ExchangeId) -> Any:
        """Get or create a CCXT Pro exchange instance."""
        if exchange_id not in self._exchanges:
            factory = self._exchange_factories.get(exchange_id)
            if factory is None:
                msg = f"No exchange factory registered for {exchange_id}"
                raise ValueError(msg)
            self._exchanges[exchange_id] = factory()
        return self._exchanges[exchange_id]

    async def _new_ws_instance(self, feed: _Feed) -> Any:
        """Create a replacement WebSocket instance, reusing the loaded markets."""
        exchange = await self._get_or_create_exchange(feed.exchange_id)
        if hasattr(exchange, "set_markets"):
            exchange.set_markets(feed.markets)
        metrics.record_ws_reconnect(feed.exchange_id)
        logger.info("Recreated exchange instance", exchange=feed.exchange_id)
        return exchange

    async def _drop_ws_instance(self, feed: _Feed) -> None:
        old = self._exchanges.pop(feed.exchange_id, None)
        if old is not None and hasattr(old, "close"):
            with contextlib.suppress(Exception):
                await old.close()

    async def _rest_exchange(self, exchange_id: str) -> tuple[Any, dict[str, Any]]:
        rest = self._rest.get(exchange_id)
        if rest is None:
            rest = self._rest[exchange_id] = self._rest_factory(exchange_id)
        markets = self._rest_markets.get(exchange_id)
        if markets is None:
            markets = self._rest_markets[exchange_id] = dict(await rest.load_markets())
        return rest, markets

    async def _fetch_rest(
        self,
        exchange_id: ExchangeId,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        now: datetime,
        unified: str | None = None,
        max_retries: int = 5,
    ) -> list[OHLCV]:
        rest, markets = await self._rest_exchange(exchange_id)
        unified = unified or resolve_spot_symbol(markets, symbol, exchange_id)
        return await fetch_closed_bars(
            rest,
            unified,
            timeframe,
            start,
            end,
            exchange_id=exchange_id,
            now=now,
            max_retries=max_retries,
            sleep=self._sleep,
        )

    def _build_feed(
        self,
        exchange_id: ExchangeId,
        markets: dict[str, Any],
        symbols: list[str],
        timeframes: list[Timeframe],
    ) -> _Feed:
        now = self._clock()
        streams: dict[StreamKey, _Stream] = {}
        resync = False
        for symbol in dict.fromkeys(symbols):
            unified = resolve_spot_symbol(markets, symbol, exchange_id)
            for timeframe in dict.fromkeys(timeframes):
                tail = self._warm_tails.get((exchange_id, symbol, timeframe))
                resync = resync or tail is not None
                streams[(symbol, timeframe)] = _Stream(
                    symbol=symbol,
                    unified=unified,
                    timeframe=timeframe,
                    tf_s=timeframe_seconds(timeframe),
                    assembler=ClosedBarAssembler(timeframe, last_emitted=tail),
                    started_at=now,
                    delivered=tail,
                    newest=tail,
                )
        if not streams:
            msg = f"{exchange_id}: connect() needs at least one symbol and timeframe"
            raise ValueError(msg)
        return _Feed(
            exchange_id=exchange_id,
            streams=streams,
            by_unified={(s.unified, str(s.timeframe)): s for s in streams.values()},
            queue=asyncio.Queue(maxsize=self._queue_maxsize),
            persist_queue=asyncio.Queue(),
            markets=markets,
            last_message=now,
            resync_needed=resync,
        )

    # ------------------------------------------------------------------
    # Internal: watch loop
    # ------------------------------------------------------------------

    async def _watch_loop(self, feed: _Feed) -> None:
        """Single watch loop for every stream of the exchange, with reconnect and backoff."""
        eid = feed.exchange_id
        backoff = _INITIAL_BACKOFF_S
        consecutive_errors = 0
        healthy_since: float | None = None

        while self._running.get(eid, False):
            try:
                exchange: Any = self._exchanges.get(eid)
                if exchange is None:
                    feed.reconnect_requested = False
                    self._mark_disrupted(feed)
                    exchange = await self._new_ws_instance(feed)
                if feed.resync_needed:
                    await self._resync(feed)
                    if self._exchanges.get(eid) is not exchange:
                        continue
                feed.pending_watch = asyncio.ensure_future(
                    exchange.watch_ohlcv_for_symbols(feed.subscriptions)
                )
                result = await feed.pending_watch
            except asyncio.CancelledError as exc:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                error: BaseException = exc
            except Exception as exc:
                error = exc
            else:
                now = self._clock()
                consecutive_errors = 0
                if healthy_since is None:
                    healthy_since = now
                elif now - healthy_since >= _STABLE_THRESHOLD_S:
                    backoff = _INITIAL_BACKOFF_S
                try:
                    await self._on_watch_result(feed, result, now)
                    await self._flush_expired(feed, now)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Failed to process watch result", exchange=eid)
                continue

            if not self._running.get(eid, False):
                break
            consecutive_errors += 1
            healthy_since = None
            self._mark_disrupted(feed)
            forced = feed.reconnect_requested
            logger.warning(
                "OHLCV watch error, reconnecting",
                exchange=eid,
                error=type(error).__name__,
                detail=str(error)[:200],
                backoff=backoff,
                consecutive_errors=consecutive_errors,
                forced=forced,
            )
            if forced or consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                consecutive_errors = 0
                await self._drop_ws_instance(feed)
            wait = backoff
            if error_is(error, "RateLimitExceeded", "DDoSProtection"):
                wait = max(wait, _RATE_LIMIT_BACKOFF_S)
            await self._sleep(wait)
            backoff = min(backoff * _BACKOFF_FACTOR, _MAX_BACKOFF_S)

    def _mark_disrupted(self, feed: _Feed) -> None:
        """Updates may have been missed: drop held candles and refill over REST next."""
        for stream in feed.streams.values():
            held = stream.assembler.held
            if held is not None and stream.delivered is None:
                stream.refill_from = held.timestamp
            stream.assembler.reset_after_reconnect()
        feed.resync_needed = True

    async def _on_watch_result(self, feed: _Feed, result: Any, now: float) -> None:
        eid = feed.exchange_id
        feed.last_message = now
        for unified, by_timeframe in dict(result).items():
            for tf_name, rows in dict(by_timeframe).items():
                stream = feed.by_unified.get((unified, tf_name))
                if stream is None:
                    continue
                metrics.mark_feed_message(eid, stream.symbol, tf_name, at=now)
                bars: list[OHLCV] = []
                for row in rows:
                    try:
                        bars.append(self._normalizer.normalize_ohlcv(list(row), eid))
                    except ValueError:
                        metrics.record_feed_bar_rejected(eid, stream.symbol, "malformed")
                        logger.warning("Malformed candle row", exchange=eid, row=repr(row))
                assembled = stream.assembler.feed(bars)
                for bar in assembled.closed:
                    if not self._claim_from_gaps(feed, stream, bar.timestamp):
                        continue
                    await self._fill_hole(feed, stream, bar.timestamp, now, newest_live=False)
                    await self._accept_closed(feed, stream, bar, source="ws", replay=False)
                held = stream.assembler.held
                if held is not None:
                    await self._fill_hole(feed, stream, held.timestamp, now, newest_live=True)
                if assembled.latest_price is not None:
                    self._offer_price(feed, stream, assembled.latest_price, now)
        if not feed.resync_needed:
            self._clear_stale(feed, _EXCHANGE_SCOPE, "ws_silence")

    async def _fill_hole(
        self, feed: _Feed, stream: _Stream, until: datetime, now: float, *, newest_live: bool
    ) -> None:
        """REST-fill closed bars skipped between the last delivered bar and ``until``.

        With ``newest_live`` (``until`` is the forming candle) the bar just before it is the
        newest closed bar and goes out with ``replay=False``.
        """
        start = stream.next_missing()
        if start is None or until <= start:
            return
        end = until - timedelta(seconds=stream.tf_s)
        missed = await self._fetch_or_defer(
            feed, stream, start, end, datetime.fromtimestamp(now, UTC)
        )
        for bar in missed:
            live = newest_live and bar.timestamp == end
            await self._accept_closed(feed, stream, bar, source="rest_gap", replay=not live)
        stream.refill_from = None

    async def _fetch_or_defer(
        self, feed: _Feed, stream: _Stream, start: datetime, end: datetime, now: datetime
    ) -> list[OHLCV]:
        """Closed bars in ``[start, end]`` over REST; what it cannot get goes to the gap task."""
        if stream.gaps:
            self._defer_gap(feed, stream, start, end)
            return []
        try:
            bars = await self._fetch_rest(
                feed.exchange_id,
                stream.symbol,
                stream.timeframe,
                start,
                end,
                now,
                stream.unified,
                max_retries=_INLINE_REST_RETRIES,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "REST gap fill failed; retrying in the background",
                exchange=feed.exchange_id,
                symbol=stream.symbol,
                timeframe=str(stream.timeframe),
                start=start.isoformat(),
                end=end.isoformat(),
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )
            self._defer_gap(feed, stream, start, end)
            return []
        if not bars or bars[-1].timestamp < end:
            rest_from = bars[-1].timestamp + timedelta(seconds=stream.tf_s) if bars else start
            self._defer_gap(feed, stream, rest_from, end)
        return bars

    def _defer_gap(self, feed: _Feed, stream: _Stream, start: datetime, end: datetime) -> None:
        """Hand ``[start, end]`` to the gap task; live bars after it keep flowing meanwhile."""
        tf = timedelta(seconds=stream.tf_s)
        last = stream.gaps[-1] if stream.gaps else None
        if last is not None and start <= last.end + tf:
            last.end = max(last.end, end)
        else:
            stream.gaps.append(_Gap(start, end))
        if stream.delivered is None or end > stream.delivered:
            stream.delivered = end
        stream.refill_from = None
        self._set_stale(feed, (stream.symbol, stream.timeframe), "gap_unfilled", 0.0)
        feed.gap_wake.set()

    def _claim_from_gaps(self, feed: _Feed, stream: _Stream, ts: datetime) -> bool:
        """Whether a WebSocket-closed bar is new; one inside a deferred range is cut out of it."""
        if stream.newest is not None and ts <= stream.newest:
            return False
        tf = timedelta(seconds=stream.tf_s)
        for i, gap in enumerate(stream.gaps):
            if not gap.start <= ts <= gap.end:
                continue
            if gap.start == gap.end:
                del stream.gaps[i]
            elif ts == gap.end:
                gap.end = ts - tf
            elif ts == gap.start:
                gap.start = ts + tf
            else:
                stream.gaps.insert(i + 1, _Gap(ts + tf, gap.end))
                gap.end = ts - tf
            if not stream.gaps:
                self._clear_stale(feed, (stream.symbol, stream.timeframe), "gap_unfilled")
            break
        return True

    async def _resync(self, feed: _Feed) -> None:
        """Fetch every stream's missed closed bars, then deliver them oldest first."""
        await self._flush_all(feed)
        now = self._clock()
        now_dt = datetime.fromtimestamp(now, UTC)
        pending: list[tuple[_Stream, OHLCV]] = []
        tail_deferred: set[_Stream] = set()
        for stream in feed.streams.values():
            start = stream.next_missing()
            if start is None:
                continue
            end = last_closed_open(now_dt, stream.timeframe)
            if start > end:
                continue
            bars = await self._fetch_or_defer(feed, stream, start, end, now_dt)
            pending.extend((stream, bar) for bar in bars)
            if stream.gaps and stream.gaps[-1].end >= end:
                tail_deferred.add(stream)

        newest: dict[_Stream, datetime] = {}
        for stream, bar in pending:
            newest[stream] = max(bar.timestamp, newest.get(stream, bar.timestamp))
        pending.sort(key=lambda p: (close_time(p[1].timestamp, p[0].timeframe), -p[0].tf_s))
        for stream, bar in pending:
            replay = bar.timestamp != newest[stream] or stream in tail_deferred
            await self._accept_closed(feed, stream, bar, source="rest_gap", replay=replay)
        for stream in feed.streams.values():
            stream.refill_from = None
        feed.resync_needed = False
        if pending:
            logger.info("Feed gap filled over REST", exchange=feed.exchange_id, bars=len(pending))
        for stream in feed.streams.values():
            self._check_overdue(feed, stream, now)

    async def _gap_loop(self, feed: _Feed) -> None:
        """Retry deferred ranges with capped backoff until filled or the exchange has none."""
        wait = _GAP_RETRY_INITIAL_S
        while self._running.get(feed.exchange_id, False):
            if not any(stream.gaps for stream in feed.streams.values()):
                feed.gap_wake.clear()
                await feed.gap_wake.wait()
                wait = _GAP_RETRY_INITIAL_S
                continue
            await self._sleep(wait)
            try:
                done = await self._retry_gaps(feed)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Feed gap retry failed", exchange=feed.exchange_id)
                done = False
            wait = _GAP_RETRY_INITIAL_S if done else min(wait * 2, _GAP_RETRY_MAX_S)

    async def _retry_gaps(self, feed: _Feed) -> bool:
        """One pass over the deferred ranges; False when one still needs another attempt."""
        done = True
        for stream in feed.streams.values():
            tf = timedelta(seconds=stream.tf_s)
            while stream.gaps:
                gap = stream.gaps[0]
                now_dt = datetime.fromtimestamp(self._clock(), UTC)
                try:
                    bars = await self._fetch_rest(
                        feed.exchange_id,
                        stream.symbol,
                        stream.timeframe,
                        gap.start,
                        gap.end,
                        now_dt,
                        stream.unified,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "Deferred gap still unfilled",
                        exchange=feed.exchange_id,
                        symbol=stream.symbol,
                        timeframe=str(stream.timeframe),
                        start=gap.start.isoformat(),
                        end=gap.end.isoformat(),
                        error=type(exc).__name__,
                    )
                    done = False
                    break
                if gap not in stream.gaps:
                    continue
                bars = [b for b in bars if gap.start <= b.timestamp <= gap.end]
                newest_closed = last_closed_open(now_dt, stream.timeframe)
                for bar in bars:
                    live = bar.timestamp == newest_closed and (
                        stream.newest is None or bar.timestamp > stream.newest
                    )
                    await self._accept_closed(feed, stream, bar, source="rest_gap", replay=not live)
                if gap not in stream.gaps or (bars and bars[-1].timestamp >= gap.end):
                    if gap in stream.gaps:
                        stream.gaps.remove(gap)
                    continue
                if bars:
                    gap.start = bars[-1].timestamp + tf
                gap.empty_answers += 1
                if gap.empty_answers >= _GAP_EMPTY_ATTEMPTS:
                    logger.error(
                        "Exchange has no closed bars for a feed gap; giving up on it",
                        exchange=feed.exchange_id,
                        symbol=stream.symbol,
                        timeframe=str(stream.timeframe),
                        start=gap.start.isoformat(),
                        end=gap.end.isoformat(),
                    )
                    stream.gaps.remove(gap)
                    continue
                done = False
                break
            if not stream.gaps:
                self._clear_stale(feed, (stream.symbol, stream.timeframe), "gap_unfilled")
        return done

    # ------------------------------------------------------------------
    # Internal: closed-bar path
    # ------------------------------------------------------------------

    def _valid(self, exchange_id: str, symbol: str, bar: OHLCV) -> bool:
        ok, errors = self._normalizer.validate_bar(bar)
        if not ok:
            metrics.record_feed_bar_rejected(exchange_id, symbol, _reject_reason(errors))
            logger.warning(
                "Rejected invalid closed bar",
                exchange=exchange_id,
                symbol=symbol,
                timestamp=bar.timestamp.isoformat(),
                errors=errors,
            )
        return ok

    async def _accept_closed(
        self, feed: _Feed, stream: _Stream, bar: OHLCV, *, source: str, replay: bool
    ) -> None:
        eid = feed.exchange_id
        late = stream.newest is not None and bar.timestamp < stream.newest
        if not late:
            if source == "rest_gap":
                stream.assembler.seed(bar.timestamp)
            if stream.delivered is None or bar.timestamp > stream.delivered:
                stream.delivered = bar.timestamp
            stream.newest = bar.timestamp
        if not self._valid(eid, stream.symbol, bar):
            return
        for anomaly in self._normalizer.detect_anomaly(bar, None if late else stream.previous):
            metrics.record_feed_anomaly(eid, stream.symbol, _anomaly_kind(anomaly))
            logger.warning(
                "Closed bar anomaly",
                exchange=eid,
                symbol=stream.symbol,
                timeframe=str(stream.timeframe),
                timestamp=bar.timestamp.isoformat(),
                anomaly=anomaly,
            )
        tf_name = str(stream.timeframe)
        metrics.record_feed_bar(eid, stream.symbol, tf_name, source)
        if not late:
            stream.previous = bar
            metrics.mark_closed_bar(eid, stream.symbol, tf_name, bar.timestamp.timestamp())
        if self._repository is not None:
            feed.persist_queue.put_nowait((eid, stream.symbol, stream.timeframe, bar))
        if replay and not self._publish_replay:
            return
        event = BarEvent(
            symbol=Symbol(stream.symbol),
            timeframe=stream.timeframe,
            ohlcv=bar,
            exchange_id=eid,
            replay=replay,
        )
        if source == "ws":
            await self._route(feed, stream, event, bar)
            self._check_overdue(feed, stream, self._clock())
        else:
            await self._put(feed, event)

    async def _route(self, feed: _Feed, stream: _Stream, event: BarEvent, bar: OHLCV) -> None:
        """Hold a bar while a sibling timeframe still has a candle closing at the same time."""
        closes = close_time(bar.timestamp, stream.timeframe)
        key = (stream.symbol, closes)
        waiting = any(
            other is not stream
            and other.symbol == stream.symbol
            and other.assembler.held is not None
            and close_time(other.assembler.held.timestamp, other.timeframe) == closes
            for other in feed.streams.values()
        )
        group = feed.parked.get(key)
        if waiting:
            if group is None:
                group = feed.parked[key] = _Parked(deadline=self._clock() + self._mtf_barrier_s)
            group.events.append((stream.tf_s, event))
            return
        if group is None:
            await self._put(feed, event)
            return
        group.events.append((stream.tf_s, event))
        await self._flush(feed, key)

    async def _flush(self, feed: _Feed, key: tuple[str, datetime]) -> None:
        group = feed.parked.pop(key, None)
        if group is None:
            return
        for _, event in sorted(group.events, key=lambda item: -item[0]):
            await self._put(feed, event)

    async def _flush_expired(self, feed: _Feed, now: float, *, block: bool = True) -> None:
        """Flush parked groups past their deadline; without ``block`` only those that fit."""
        for key in [k for k, group in feed.parked.items() if now >= group.deadline]:
            room = feed.queue.maxsize - feed.queue.qsize()
            if not block and room < len(feed.parked[key].events):
                continue
            await self._flush(feed, key)

    async def _flush_all(self, feed: _Feed) -> None:
        for key in sorted(feed.parked, key=lambda k: k[1]):
            await self._flush(feed, key)

    def _offer_price(self, feed: _Feed, stream: _Stream, price: Decimal, now: float) -> None:
        if now - feed.last_price_at.get(stream.symbol, float("-inf")) < _PRICE_INTERVAL_S:
            return
        feed.last_price_at[stream.symbol] = now
        self._offer(
            feed,
            PriceEvent(symbol=Symbol(stream.symbol), price=price, exchange_id=feed.exchange_id),
        )

    # ------------------------------------------------------------------
    # Internal: dispatch queue
    # ------------------------------------------------------------------

    async def _put(self, feed: _Feed, event: Event) -> None:
        """Enqueue a bar; when the queue is full the consumer is lagging, so wait for room."""
        if feed.queue.full():
            logger.error(
                "Feed dispatch queue full; waiting for the consumer",
                exchange=feed.exchange_id,
                size=feed.queue.qsize(),
            )
            self._set_stale(feed, _EXCHANGE_SCOPE, "consumer_lag", 0.0)
        await feed.queue.put((self._clock(), event))

    def _offer(self, feed: _Feed, event: Event) -> bool:
        """Enqueue a price event; False when the full queue dropped it."""
        try:
            feed.queue.put_nowait((self._clock(), event))
        except asyncio.QueueFull:
            logger.warning(
                "Feed dispatch queue full; dropped event",
                exchange=feed.exchange_id,
                event_type=event.event_type,
            )
            return False
        return True

    def _emit_status(self, feed: _Feed, event: FeedStatusEvent) -> None:
        """Queue a status event; one that does not fit waits in the outbox, in order."""
        feed.outbox.append(event)
        self._flush_outbox(feed)

    def _flush_outbox(self, feed: _Feed) -> None:
        while feed.outbox:
            try:
                feed.queue.put_nowait((self._clock(), feed.outbox[0]))
            except asyncio.QueueFull:
                return
            feed.outbox.popleft()

    async def _dispatch_loop(self, feed: _Feed) -> None:
        while True:
            enqueued_at, event = await feed.queue.get()
            self._flush_outbox(feed)
            feed.inflight_since = enqueued_at
            try:
                await self._event_bus.publish(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Feed event dispatch failed",
                    exchange=feed.exchange_id,
                    event_type=event.event_type,
                )
            finally:
                feed.inflight_since = None

    # ------------------------------------------------------------------
    # Internal: persistence
    # ------------------------------------------------------------------

    async def _persist_loop(self, feed: _Feed) -> None:
        repo = self._repository
        if repo is None:
            return
        while True:
            batch = [await feed.persist_queue.get()]
            while not feed.persist_queue.empty() and len(batch) < _PERSIST_BATCH:
                batch.append(feed.persist_queue.get_nowait())
            feed.persist_inflight = len(batch)
            try:
                await self._persist_batch(feed, repo, batch)
            finally:
                feed.persist_inflight = 0
                for _ in batch:
                    feed.persist_queue.task_done()

    async def _persist_batch(
        self, feed: _Feed, repo: MarketDataRepository, batch: list[PersistRow]
    ) -> None:
        """Upsert one batch, retrying with capped backoff until it lands or the feed stops."""
        attempt = 0
        while True:
            try:
                await repo.upsert_closed_bars(batch)
            except asyncio.CancelledError:
                raise
            except Exception:
                attempt += 1
                metrics.record_feed_persist_error(feed.exchange_id)
                logger.exception(
                    "Closed-bar persist failed; retrying",
                    exchange=feed.exchange_id,
                    bars=len(batch),
                    attempt=attempt,
                    backlog=feed.persist_queue.qsize() + len(batch),
                )
                await self._sleep(
                    min(_PERSIST_BACKOFF_S * 2 ** (attempt - 1), _PERSIST_BACKOFF_MAX_S)
                )
                continue
            if attempt:
                logger.info(
                    "Closed-bar persist recovered", exchange=feed.exchange_id, attempts=attempt
                )
            return

    # ------------------------------------------------------------------
    # Internal: watchdog and feed status
    # ------------------------------------------------------------------

    async def _watchdog_loop(self, feed: _Feed) -> None:
        while self._running.get(feed.exchange_id, False):
            await asyncio.sleep(self._watchdog_interval_s)
            try:
                await self._watchdog_tick(feed)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Feed watchdog tick failed", exchange=feed.exchange_id)

    async def _watchdog_tick(self, feed: _Feed) -> None:
        now = self._clock()
        self._flush_outbox(feed)
        silence = now - feed.last_message
        recently_forced = (
            feed.last_forced_close is not None
            and now - feed.last_forced_close <= self._ws_silence_s
        )
        if silence > self._ws_silence_s and not recently_forced:
            self._set_stale(feed, _EXCHANGE_SCOPE, "ws_silence", silence)
            feed.reconnect_requested = True
            feed.last_forced_close = now
            await self._drop_ws_instance(feed)
            if feed.pending_watch is not None and not feed.pending_watch.done():
                feed.pending_watch.cancel()

        for stream in feed.streams.values():
            self._check_overdue(feed, stream, now)

        if feed.inflight_since is not None and now - feed.inflight_since > feed.min_tf_s:
            self._set_stale(feed, _EXCHANGE_SCOPE, "consumer_lag", now - feed.inflight_since)
        elif feed.inflight_since is None and not feed.queue.full():
            self._clear_stale(feed, _EXCHANGE_SCOPE, "consumer_lag")

        await self._flush_expired(feed, now, block=False)

    def _check_overdue(self, feed: _Feed, stream: _Stream, now: float) -> None:
        last = stream.assembler.last_emitted
        if last is not None:
            age = now - close_time(last, stream.timeframe).timestamp()
        else:
            age = now - stream.started_at - stream.tf_s
        key: StatusKey = (stream.symbol, stream.timeframe)
        if age > self._stale_after_bars * stream.tf_s:
            self._set_stale(feed, key, "bar_overdue", age)
        else:
            self._clear_stale(feed, key, "bar_overdue")

    def _set_stale(self, feed: _Feed, key: StatusKey, reason: str, age: float) -> None:
        reasons = feed.stale.setdefault(key, set())
        if reason in reasons:
            return
        reasons.add(reason)
        logger.warning(
            "Market data feed stale",
            exchange=feed.exchange_id,
            symbol=key[0] or None,
            timeframe=str(key[1]) if key[1] else None,
            reason=reason,
            age_seconds=round(age, 1),
        )
        self._emit_status(feed, self._status_event(feed, key, "stale", reason, age))

    def _clear_stale(self, feed: _Feed, key: StatusKey, reason: str) -> None:
        reasons = feed.stale.get(key)
        if not reasons or reason not in reasons:
            return
        reasons.discard(reason)
        if reasons:
            return
        logger.info(
            "Market data feed live",
            exchange=feed.exchange_id,
            symbol=key[0] or None,
            timeframe=str(key[1]) if key[1] else None,
            recovered_from=reason,
        )
        self._emit_status(feed, self._status_event(feed, key, "live", reason, 0.0))

    @staticmethod
    def _status_event(
        feed: _Feed, key: StatusKey, status: str, reason: str, age: float
    ) -> FeedStatusEvent:
        return FeedStatusEvent(
            exchange_id=feed.exchange_id,
            symbol=Symbol(key[0]),
            timeframe=key[1],
            status="stale" if status == "stale" else "live",
            reason=reason,
            age_seconds=age,
        )
