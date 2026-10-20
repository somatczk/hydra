"""Tests for hydra.core.events — event hierarchy, serialization, unique IDs."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal

import msgpack
import pytest

from hydra.core.events import (
    BarEvent,
    CircuitBreakerEvent,
    ConfigChangeEvent,
    DrawdownAlertEvent,
    EntrySignal,
    ErrorEvent,
    Event,
    ExitSignal,
    FeedStatusEvent,
    FundingRateEvent,
    HeartbeatEvent,
    MarketDataEvent,
    OrderBookEvent,
    OrderCancelEvent,
    OrderEvent,
    OrderFillEvent,
    OrderRejectEvent,
    OrderRequestEvent,
    PriceEvent,
    RiskCheckResult,
    RiskEvent,
    SignalEvent,
    SystemEvent,
    TradeEvent,
    event_from_dict,
    event_to_dict,
)
from hydra.core.types import (
    OHLCV,
    Direction,
    MarketType,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    Symbol,
    Timeframe,
)

# ---------------------------------------------------------------------------
# Base Event
# ---------------------------------------------------------------------------


class TestBaseEvent:
    def test_auto_id(self) -> None:
        e1 = Event()
        e2 = Event()
        assert e1.event_id != e2.event_id

    def test_auto_timestamp(self) -> None:
        e = Event()
        assert e.timestamp.tzinfo == UTC

    def test_event_type(self) -> None:
        e = Event()
        assert e.event_type == "event"

    def test_frozen(self) -> None:
        e = Event()
        with pytest.raises(FrozenInstanceError):
            e.event_id = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Hierarchy check
# ---------------------------------------------------------------------------


class TestEventHierarchy:
    def test_market_data_is_event(self) -> None:
        e = MarketDataEvent()
        assert isinstance(e, Event)
        assert e.event_type == "market_data"

    def test_bar_event(self) -> None:
        ts = datetime(2024, 1, 1, tzinfo=UTC)
        ohlcv = OHLCV(
            Decimal("1"), Decimal("2"), Decimal("0.5"), Decimal("1.5"), Decimal("100"), ts
        )
        bar = BarEvent(symbol=Symbol("BTCUSDT"), timeframe=Timeframe.H1, ohlcv=ohlcv)
        assert isinstance(bar, MarketDataEvent)
        assert isinstance(bar, Event)
        assert bar.event_type == "bar"
        assert bar.symbol == "BTCUSDT"

    def test_trade_event(self) -> None:
        t = TradeEvent(
            symbol=Symbol("BTCUSDT"),
            price=Decimal("65000"),
            quantity=Decimal("0.1"),
            side=Side.BUY,
        )
        assert isinstance(t, MarketDataEvent)
        assert t.event_type == "trade"

    def test_order_book_event(self) -> None:
        ob = OrderBookEvent(
            symbol=Symbol("BTCUSDT"),
            bids=((Decimal("65000"), Decimal("1.0")),),
            asks=((Decimal("65001"), Decimal("0.5")),),
        )
        assert isinstance(ob, MarketDataEvent)
        assert ob.event_type == "order_book"

    def test_funding_rate_event(self) -> None:
        fr = FundingRateEvent(
            symbol=Symbol("BTCUSDT"),
            rate=Decimal("0.0001"),
        )
        assert isinstance(fr, MarketDataEvent)
        assert fr.event_type == "funding_rate"

    def test_signal_event(self) -> None:
        s = SignalEvent()
        assert isinstance(s, Event)
        assert s.event_type == "signal"

    def test_entry_signal(self) -> None:
        es = EntrySignal(
            symbol=Symbol("BTCUSDT"),
            direction=Direction.LONG,
            strength=Decimal("0.85"),
            strategy_id="momentum",
        )
        assert isinstance(es, SignalEvent)
        assert es.event_type == "entry_signal"
        assert es.strength == Decimal("0.85")

    def test_exit_signal(self) -> None:
        xs = ExitSignal(
            symbol=Symbol("BTCUSDT"),
            strategy_id="momentum",
            reason="stop_loss",
        )
        assert isinstance(xs, SignalEvent)
        assert xs.event_type == "exit_signal"

    def test_order_event(self) -> None:
        oe = OrderEvent()
        assert isinstance(oe, Event)
        assert oe.event_type == "order"

    def test_order_request_event(self) -> None:
        req = OrderRequest(
            symbol=Symbol("BTCUSDT"),
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.01"),
            strategy_id="test",
            exchange_id="binance",
            market_type=MarketType.SPOT,
        )
        ore = OrderRequestEvent(order_request=req)
        assert isinstance(ore, OrderEvent)
        assert ore.event_type == "order_request"
        assert ore.order_request is not None
        assert ore.order_request.symbol == "BTCUSDT"

    def test_order_fill_event(self) -> None:
        ofe = OrderFillEvent(
            order_id="ord-1",
            symbol=Symbol("BTCUSDT"),
            side=Side.BUY,
            quantity=Decimal("0.5"),
            price=Decimal("65000"),
            fee=Decimal("0.001"),
            fee_currency="BTC",
            status=OrderStatus.FILLED,
        )
        assert isinstance(ofe, OrderEvent)
        assert ofe.event_type == "order_fill"

    def test_order_cancel_event(self) -> None:
        oce = OrderCancelEvent(order_id="ord-1", symbol=Symbol("BTCUSDT"), reason="user_request")
        assert isinstance(oce, OrderEvent)
        assert oce.event_type == "order_cancel"

    def test_order_reject_event(self) -> None:
        ore = OrderRejectEvent(
            order_id="ord-1", symbol=Symbol("BTCUSDT"), reason="insufficient_balance"
        )
        assert isinstance(ore, OrderEvent)
        assert ore.event_type == "order_reject"

    def test_risk_event(self) -> None:
        re_ = RiskEvent()
        assert isinstance(re_, Event)
        assert re_.event_type == "risk"

    def test_risk_check_result(self) -> None:
        rcr = RiskCheckResult(order_request_id="req-1", approved=False, reason="max_exposure")
        assert isinstance(rcr, RiskEvent)
        assert rcr.event_type == "risk_check_result"
        assert not rcr.approved

    def test_circuit_breaker_event(self) -> None:
        cb = CircuitBreakerEvent(tier=2, action="halt_new_trades", drawdown_pct=Decimal("0.05"))
        assert isinstance(cb, RiskEvent)
        assert cb.event_type == "circuit_breaker"

    def test_drawdown_alert_event(self) -> None:
        da = DrawdownAlertEvent(
            current_drawdown_pct=Decimal("0.04"),
            threshold_pct=Decimal("0.03"),
            message="Drawdown exceeded tier-1",
        )
        assert isinstance(da, RiskEvent)
        assert da.event_type == "drawdown_alert"

    def test_system_event(self) -> None:
        se = SystemEvent()
        assert isinstance(se, Event)
        assert se.event_type == "system"

    def test_heartbeat_event(self) -> None:
        hb = HeartbeatEvent(component="data_feed")
        assert isinstance(hb, SystemEvent)
        assert hb.event_type == "heartbeat"

    def test_config_change_event(self) -> None:
        cc = ConfigChangeEvent(section="trading", changes={"testnet": False})
        assert isinstance(cc, SystemEvent)
        assert cc.event_type == "config_change"
        assert cc.changes == {"testnet": False}

    def test_error_event(self) -> None:
        err = ErrorEvent(component="executor", error_type="ConnectionError", message="timed out")
        assert isinstance(err, SystemEvent)
        assert err.event_type == "error"


# ---------------------------------------------------------------------------
# Unique event IDs
# ---------------------------------------------------------------------------


class TestUniqueEventIds:
    def test_all_events_unique(self) -> None:
        events = [
            Event(),
            BarEvent(),
            TradeEvent(),
            EntrySignal(),
            OrderFillEvent(),
            RiskCheckResult(),
            HeartbeatEvent(),
        ]
        ids = [e.event_id for e in events]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Serialization / Deserialization (msgpack)
# ---------------------------------------------------------------------------


class TestEventSerialization:
    def test_base_event_roundtrip(self) -> None:
        e = Event()
        d = event_to_dict(e)
        restored = event_from_dict(d)
        assert restored.event_id == e.event_id
        assert restored.event_type == "event"

    def test_bar_event_roundtrip(self) -> None:
        ts = datetime(2024, 3, 15, 10, 0, 0, tzinfo=UTC)
        ohlcv = OHLCV(
            open=Decimal("65000.50"),
            high=Decimal("65500.00"),
            low=Decimal("64800.00"),
            close=Decimal("65200.00"),
            volume=Decimal("1234.5678"),
            timestamp=ts,
        )
        bar = BarEvent(
            symbol=Symbol("BTCUSDT"),
            timeframe=Timeframe.H1,
            ohlcv=ohlcv,
            exchange_id="binance",
        )
        d = event_to_dict(bar)
        restored = event_from_dict(d)
        assert isinstance(restored, BarEvent)
        assert restored.symbol == "BTCUSDT"
        assert restored.ohlcv is not None
        assert restored.ohlcv.open == Decimal("65000.50")
        assert restored.ohlcv.volume == Decimal("1234.5678")

    def test_entry_signal_roundtrip(self) -> None:
        sig = EntrySignal(
            symbol=Symbol("ETHUSDT"),
            direction=Direction.SHORT,
            strength=Decimal("0.72"),
            strategy_id="mean_revert",
            exchange_id="bybit",
        )
        d = event_to_dict(sig)
        restored = event_from_dict(d)
        assert isinstance(restored, EntrySignal)
        assert restored.direction == "SHORT"
        assert restored.strength == Decimal("0.72")

    def test_order_fill_roundtrip(self) -> None:
        fill = OrderFillEvent(
            order_id="ord-abc",
            symbol=Symbol("BTCUSDT"),
            side=Side.BUY,
            quantity=Decimal("0.5"),
            price=Decimal("65000"),
            fee=Decimal("32.50"),
            fee_currency="USDT",
        )
        d = event_to_dict(fill)
        restored = event_from_dict(d)
        assert isinstance(restored, OrderFillEvent)
        assert restored.order_id == "ord-abc"
        assert restored.fee == Decimal("32.50")

    def test_msgpack_binary_roundtrip(self) -> None:
        """Full roundtrip: event -> dict -> msgpack bytes -> dict -> event."""
        e = HeartbeatEvent(component="test")
        d = event_to_dict(e)
        packed = msgpack.packb(d, use_bin_type=True)
        unpacked = msgpack.unpackb(packed, raw=False)
        restored = event_from_dict(unpacked)
        assert isinstance(restored, HeartbeatEvent)
        assert restored.component == "test"
        assert restored.event_id == e.event_id

    def test_error_event_roundtrip(self) -> None:
        err = ErrorEvent(
            component="data_feed",
            error_type="TimeoutError",
            message="Connection timed out",
            traceback="Traceback ...",
        )
        d = event_to_dict(err)
        packed = msgpack.packb(d, use_bin_type=True)
        unpacked = msgpack.unpackb(packed, raw=False)
        restored = event_from_dict(unpacked)
        assert isinstance(restored, ErrorEvent)
        assert restored.error_type == "TimeoutError"
        assert restored.traceback == "Traceback ..."

    def test_circuit_breaker_roundtrip(self) -> None:
        cb = CircuitBreakerEvent(
            tier=3,
            action="flatten_all",
            drawdown_pct=Decimal("0.10"),
        )
        d = event_to_dict(cb)
        packed = msgpack.packb(d, use_bin_type=True)
        unpacked = msgpack.unpackb(packed, raw=False)
        restored = event_from_dict(unpacked)
        assert isinstance(restored, CircuitBreakerEvent)
        assert restored.tier == 3
        assert restored.drawdown_pct == Decimal("0.10")

    def test_config_change_roundtrip(self) -> None:
        cc = ConfigChangeEvent(section="trading", changes={"testnet": False})
        d = event_to_dict(cc)
        restored = event_from_dict(d)
        assert isinstance(restored, ConfigChangeEvent)
        assert restored.changes == {"testnet": False}

    def test_dict_has_class_marker(self) -> None:
        e = BarEvent()
        d = event_to_dict(e)
        assert d["__event_class__"] == "BarEvent"


# ---------------------------------------------------------------------------
# P1 additive contracts
# ---------------------------------------------------------------------------


def _msgpack_roundtrip(event: Event) -> Event:
    packed = msgpack.packb(event_to_dict(event), use_bin_type=True)
    return event_from_dict(msgpack.unpackb(packed, raw=False))


def _ohlcv() -> OHLCV:
    return OHLCV(
        open=Decimal("65000.50"),
        high=Decimal("65500.00"),
        low=Decimal("64800.00"),
        close=Decimal("65200.00"),
        volume=Decimal("12.5"),
        timestamp=datetime(2024, 3, 15, 10, 0, tzinfo=UTC),
    )


class TestBarEventReplay:
    def test_replay_defaults_false(self) -> None:
        bar = BarEvent(symbol=Symbol("BTCUSDT"), timeframe=Timeframe.H1, ohlcv=_ohlcv())
        assert bar.replay is False
        assert BarEvent().replay is False

    def test_replay_roundtrip(self) -> None:
        bar = BarEvent(
            symbol=Symbol("BTCUSDT"), timeframe=Timeframe.H1, ohlcv=_ohlcv(), replay=True
        )
        restored = _msgpack_roundtrip(bar)
        assert type(restored) is BarEvent
        assert restored.replay is True
        assert restored.timeframe is Timeframe.H1
        assert restored.ohlcv == _ohlcv()

    def test_replay_frozen(self) -> None:
        bar = BarEvent(replay=True)
        name = "replay"
        with pytest.raises(FrozenInstanceError):
            setattr(bar, name, False)


class TestPriceEvent:
    def test_defaults(self) -> None:
        pe = PriceEvent()
        assert isinstance(pe, MarketDataEvent)
        assert pe.event_type == "price"
        assert pe.symbol == ""
        assert pe.price == Decimal("0")
        assert pe.exchange_id == "binance"

    def test_msgpack_roundtrip(self) -> None:
        pe = PriceEvent(symbol=Symbol("BTCUSDT"), price=Decimal("65123.45"), exchange_id="binance")
        restored = _msgpack_roundtrip(pe)
        assert type(restored) is PriceEvent
        assert restored.event_id == pe.event_id
        assert restored.timestamp == pe.timestamp
        assert restored.symbol == "BTCUSDT"
        assert restored.price == Decimal("65123.45")
        assert isinstance(restored.price, Decimal)
        assert restored.event_type == "price"

    def test_frozen(self) -> None:
        pe = PriceEvent(price=Decimal("1"))
        name = "price"
        with pytest.raises(FrozenInstanceError):
            setattr(pe, name, Decimal("2"))


class TestFeedStatusEvent:
    def test_defaults(self) -> None:
        fs = FeedStatusEvent()
        assert isinstance(fs, SystemEvent)
        assert fs.event_type == "feed_status"
        assert fs.exchange_id == "binance"
        assert fs.symbol == ""
        assert fs.timeframe is None
        assert fs.status == "live"
        assert fs.reason == ""
        assert fs.age_seconds == 0.0

    def test_stream_stale_roundtrip(self) -> None:
        fs = FeedStatusEvent(
            exchange_id="binance",
            symbol=Symbol("BTCUSDT"),
            timeframe=Timeframe.H1,
            status="stale",
            reason="bar_overdue",
            age_seconds=7325.5,
        )
        restored = _msgpack_roundtrip(fs)
        assert type(restored) is FeedStatusEvent
        assert restored.event_id == fs.event_id
        assert restored.symbol == "BTCUSDT"
        assert isinstance(restored.timeframe, Timeframe)
        assert restored.timeframe is Timeframe.H1
        assert restored.status == "stale"
        assert restored.reason == "bar_overdue"
        assert restored.age_seconds == 7325.5

    def test_exchange_wide_roundtrip_keeps_none_timeframe(self) -> None:
        fs = FeedStatusEvent(status="stale", reason="ws_silence", age_seconds=61.0)
        restored = _msgpack_roundtrip(fs)
        assert type(restored) is FeedStatusEvent
        assert restored.timeframe is None
        assert restored.symbol == ""
        assert restored.reason == "ws_silence"

    def test_frozen(self) -> None:
        fs = FeedStatusEvent(status="stale")
        name = "status"
        with pytest.raises(FrozenInstanceError):
            setattr(fs, name, "live")

    @pytest.mark.parametrize("status", ["STALE", "degraded", ""])
    def test_rejects_unknown_status(self, status: str) -> None:
        with pytest.raises(ValueError, match="must be 'live' or 'stale'"):
            FeedStatusEvent(**{"status": status})

    def test_rejects_unknown_status_from_wire(self) -> None:
        payload = event_to_dict(FeedStatusEvent(status="stale", reason="bar_overdue"))
        payload["status"] = "degraded"
        packed = msgpack.packb(payload, use_bin_type=True)
        with pytest.raises(ValueError, match="got 'degraded'"):
            event_from_dict(msgpack.unpackb(packed, raw=False))


class TestOrderFillEventP1Fields:
    def test_existing_runner_constructor_defaults(self) -> None:
        fill = OrderFillEvent(
            order_id="ord-1",
            symbol=Symbol("BTCUSDT"),
            side=Side.BUY,
            quantity=Decimal("0.5"),
            price=Decimal("65000"),
            fee=Decimal("32.5"),
            fee_currency="USDT",
            exchange_id="binance",
        )
        assert fill.session_id == ""
        assert fill.strategy_id == ""
        assert fill.tag == ""
        assert fill.reduce_only is False
        assert fill.realized_pnl is None
        assert fill.gross_pnl is None
        assert fill.status == OrderStatus.FILLED

    def test_existing_order_manager_constructor(self) -> None:
        fill = OrderFillEvent(
            order_id="ord-2",
            symbol=Symbol("BTCUSDT"),
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            quantity=Decimal("0.5"),
            price=Decimal("66000"),
            fee=Decimal("0"),
            fee_currency="",
            exchange_id="binance",
            status=OrderStatus.FILLED,
        )
        assert fill.order_type == OrderType.LIMIT
        assert fill.realized_pnl is None

    def test_closing_fill_roundtrip_keeps_decimals(self) -> None:
        # entry 0.5 @ 65000, entry fee 32.5; exit 0.5 @ 66000, exit fee 33
        gross = (Decimal("66000") - Decimal("65000")) * Decimal("0.5")
        net = gross - Decimal("33") - Decimal("32.5")
        fill = OrderFillEvent(
            order_id="ord-3",
            symbol=Symbol("BTCUSDT"),
            side=Side.SELL,
            quantity=Decimal("0.5"),
            price=Decimal("66000"),
            fee=Decimal("33"),
            fee_currency="USDT",
            session_id="sess-1",
            strategy_id="momentum",
            tag="stop_loss",
            reduce_only=True,
            realized_pnl=net,
            gross_pnl=gross,
        )
        restored = _msgpack_roundtrip(fill)
        assert isinstance(restored, OrderFillEvent)
        assert restored.session_id == "sess-1"
        assert restored.strategy_id == "momentum"
        assert restored.tag == "stop_loss"
        assert restored.reduce_only is True
        assert restored.side is Side.SELL
        assert restored.order_type is OrderType.MARKET
        assert restored.status is OrderStatus.FILLED
        assert restored.gross_pnl == Decimal("500.0")
        assert restored.realized_pnl == Decimal("434.5")
        assert isinstance(restored.realized_pnl, Decimal)
        assert isinstance(restored.gross_pnl, Decimal)

    def test_negative_pnl_roundtrip(self) -> None:
        fill = OrderFillEvent(realized_pnl=Decimal("-1.25"), gross_pnl=Decimal("-0.75"))
        restored = _msgpack_roundtrip(fill)
        assert isinstance(restored, OrderFillEvent)
        assert restored.realized_pnl == Decimal("-1.25")
        assert restored.gross_pnl == Decimal("-0.75")

    def test_opening_fill_roundtrip_keeps_none(self) -> None:
        fill = OrderFillEvent(order_id="ord-4", session_id="sess-1", side=Side.BUY)
        restored = _msgpack_roundtrip(fill)
        assert isinstance(restored, OrderFillEvent)
        assert restored.realized_pnl is None
        assert restored.gross_pnl is None
        assert restored.session_id == "sess-1"

    def test_frozen(self) -> None:
        fill = OrderFillEvent(realized_pnl=Decimal("1"))
        name = "realized_pnl"
        with pytest.raises(FrozenInstanceError):
            setattr(fill, name, Decimal("2"))


class TestOrderRequestEventP1Fields:
    def test_reduce_only_and_tag_roundtrip(self) -> None:
        req = OrderRequest(
            symbol=Symbol("BTCUSDT"),
            side=Side.SELL,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.5"),
            strategy_id="momentum",
            exchange_id="binance",
            market_type=MarketType.SPOT,
            reduce_only=True,
            tag="flatten",
        )
        restored = _msgpack_roundtrip(OrderRequestEvent(order_request=req))
        assert isinstance(restored, OrderRequestEvent)
        assert restored.order_request is not None
        assert restored.order_request.reduce_only is True
        assert restored.order_request.tag == "flatten"
        assert restored.order_request.side is Side.SELL
        assert restored.order_request.order_type is OrderType.MARKET
        assert restored.order_request.market_type is MarketType.SPOT
        assert restored.order_request.quantity == Decimal("0.5")
        assert restored.order_request.request_id == req.request_id

    def test_default_request_roundtrip(self) -> None:
        req = OrderRequest(
            symbol=Symbol("BTCUSDT"),
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.01"),
            strategy_id="momentum",
            exchange_id="binance",
            market_type=MarketType.SPOT,
        )
        restored = _msgpack_roundtrip(OrderRequestEvent(order_request=req))
        assert isinstance(restored, OrderRequestEvent)
        assert restored.order_request is not None
        assert restored.order_request.reduce_only is False
        assert restored.order_request.tag == ""


class TestEnumReconstruction:
    def test_trade_event_side(self) -> None:
        restored = _msgpack_roundtrip(TradeEvent(side=Side.SELL, price=Decimal("1")))
        assert isinstance(restored, TradeEvent)
        assert restored.side is Side.SELL

    def test_entry_signal_direction_and_market_type(self) -> None:
        sig = EntrySignal(direction=Direction.SHORT, market_type=MarketType.FUTURES)
        restored = _msgpack_roundtrip(sig)
        assert isinstance(restored, EntrySignal)
        assert restored.direction is Direction.SHORT
        assert restored.market_type is MarketType.FUTURES

    def test_exit_signal_direction(self) -> None:
        restored = _msgpack_roundtrip(ExitSignal(direction=Direction.LONG))
        assert isinstance(restored, ExitSignal)
        assert restored.direction is Direction.LONG

    def test_unknown_enum_value_stays_str(self) -> None:
        payload = event_to_dict(BarEvent(timeframe=Timeframe.H1))
        payload["timeframe"] = "7h"
        restored = event_from_dict(payload)
        assert isinstance(restored, BarEvent)
        assert restored.timeframe == "7h"
        assert not isinstance(restored.timeframe, Timeframe)

    def test_literal_fields_stay_str(self) -> None:
        restored = _msgpack_roundtrip(FeedStatusEvent(exchange_id="bybit", status="stale"))
        assert isinstance(restored, FeedStatusEvent)
        assert type(restored.exchange_id) is str
        assert type(restored.status) is str
