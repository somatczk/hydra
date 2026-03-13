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
    FundingRateEvent,
    HeartbeatEvent,
    MarketDataEvent,
    OrderBookEvent,
    OrderCancelEvent,
    OrderEvent,
    OrderFillEvent,
    OrderRejectEvent,
    OrderRequestEvent,
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
