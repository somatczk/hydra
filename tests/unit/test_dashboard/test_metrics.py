"""Tests for Hydra Prometheus metrics definitions and helpers."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_prometheus_registry():
    """Ensure a clean Prometheus registry for every test."""
    from hydra.dashboard.metrics import _reset_metrics

    _reset_metrics()
    yield
    _reset_metrics()


@pytest.fixture()
def client() -> TestClient:
    from hydra.dashboard.api import app

    return TestClient(app)


# ---------------------------------------------------------------------------
# Metric definition tests
# ---------------------------------------------------------------------------


class TestMetricDefinitions:
    """Verify that all expected metric collectors are registered."""

    def test_trades_total_is_counter(self) -> None:
        from prometheus_client import Counter

        from hydra.dashboard.metrics import get_trades_total

        metric = get_trades_total()
        assert isinstance(metric, Counter)
        # prometheus_client strips the _total suffix from Counter._name
        assert metric._name == "hydra_trades"

    def test_trade_pnl_is_histogram(self) -> None:
        from prometheus_client import Histogram

        from hydra.dashboard.metrics import get_trade_pnl

        metric = get_trade_pnl()
        assert isinstance(metric, Histogram)
        assert metric._name == "hydra_trade_pnl"

    def test_position_size_is_gauge(self) -> None:
        from prometheus_client import Gauge

        from hydra.dashboard.metrics import get_position_size

        metric = get_position_size()
        assert isinstance(metric, Gauge)
        assert metric._name == "hydra_position_size"

    def test_portfolio_value_is_gauge(self) -> None:
        from prometheus_client import Gauge

        from hydra.dashboard.metrics import get_portfolio_value

        metric = get_portfolio_value()
        assert isinstance(metric, Gauge)

    def test_drawdown_pct_is_gauge(self) -> None:
        from prometheus_client import Gauge

        from hydra.dashboard.metrics import get_drawdown_pct

        metric = get_drawdown_pct()
        assert isinstance(metric, Gauge)

    def test_daily_pnl_is_gauge(self) -> None:
        from prometheus_client import Gauge

        from hydra.dashboard.metrics import get_daily_pnl

        metric = get_daily_pnl()
        assert isinstance(metric, Gauge)

    def test_signal_count_is_counter(self) -> None:
        from prometheus_client import Counter

        from hydra.dashboard.metrics import get_signal_count

        metric = get_signal_count()
        assert isinstance(metric, Counter)

    def test_event_bus_latency_is_histogram(self) -> None:
        from prometheus_client import Histogram

        from hydra.dashboard.metrics import get_event_bus_latency

        metric = get_event_bus_latency()
        assert isinstance(metric, Histogram)

    def test_exchange_api_latency_is_histogram(self) -> None:
        from prometheus_client import Histogram

        from hydra.dashboard.metrics import get_exchange_api_latency

        metric = get_exchange_api_latency()
        assert isinstance(metric, Histogram)

    def test_ws_reconnects_is_counter(self) -> None:
        from prometheus_client import Counter

        from hydra.dashboard.metrics import get_ws_reconnects

        metric = get_ws_reconnects()
        assert isinstance(metric, Counter)

    def test_order_fill_latency_is_histogram(self) -> None:
        from prometheus_client import Histogram

        from hydra.dashboard.metrics import get_order_fill_latency

        metric = get_order_fill_latency()
        assert isinstance(metric, Histogram)

    def test_ml_inference_latency_is_histogram(self) -> None:
        from prometheus_client import Histogram

        from hydra.dashboard.metrics import get_ml_inference_latency

        metric = get_ml_inference_latency()
        assert isinstance(metric, Histogram)

    def test_data_gap_is_gauge(self) -> None:
        from prometheus_client import Gauge

        from hydra.dashboard.metrics import get_data_gap

        metric = get_data_gap()
        assert isinstance(metric, Gauge)

    def test_reconciliation_mismatch_is_gauge(self) -> None:
        from prometheus_client import Gauge

        from hydra.dashboard.metrics import get_reconciliation_mismatch

        metric = get_reconciliation_mismatch()
        assert isinstance(metric, Gauge)


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestMetricHelpers:
    """Verify convenience update functions work correctly."""

    def test_record_trade_increments_counter(self) -> None:
        from hydra.dashboard.metrics import get_trades_total, record_trade

        record_trade(
            symbol="BTCUSDT",
            side="BUY",
            strategy_id="strat-1",
            exchange_id="binance",
        )
        val = (
            get_trades_total()
            .labels(
                symbol="BTCUSDT",
                side="BUY",
                strategy_id="strat-1",
                exchange_id="binance",
            )
            ._value.get()
        )
        assert val == 1.0

    def test_record_trade_with_pnl_observes_histogram(self) -> None:
        from hydra.dashboard.metrics import get_trade_pnl, record_trade

        record_trade(
            symbol="BTCUSDT",
            side="SELL",
            strategy_id="strat-1",
            exchange_id="binance",
            pnl=42.5,
        )
        sample_count = get_trade_pnl().labels(symbol="BTCUSDT", strategy_id="strat-1")._sum.get()
        assert sample_count == 42.5

    def test_update_position(self) -> None:
        from hydra.dashboard.metrics import get_position_size, update_position

        update_position(symbol="BTCUSDT", exchange_id="binance", size=0.5)
        val = get_position_size().labels(symbol="BTCUSDT", exchange_id="binance")._value.get()
        assert val == 0.5

    def test_update_portfolio(self) -> None:
        from hydra.dashboard.metrics import (
            get_daily_pnl,
            get_drawdown_pct,
            get_portfolio_value,
            update_portfolio,
        )

        update_portfolio(value=100_000.0, drawdown_pct=3.5, daily_pnl=250.0)
        assert get_portfolio_value()._value.get() == 100_000.0
        assert get_drawdown_pct()._value.get() == 3.5
        assert get_daily_pnl()._value.get() == 250.0

    def test_record_signal(self) -> None:
        from hydra.dashboard.metrics import get_signal_count, record_signal

        record_signal(strategy_id="strat-1", signal_type="entry")
        val = get_signal_count().labels(strategy_id="strat-1", signal_type="entry")._value.get()
        assert val == 1.0

    def test_observe_event_bus_latency(self) -> None:
        from hydra.dashboard.metrics import get_event_bus_latency, observe_event_bus_latency

        observe_event_bus_latency(0.015)
        assert get_event_bus_latency()._sum.get() == 0.015

    def test_observe_exchange_api_latency(self) -> None:
        from hydra.dashboard.metrics import (
            get_exchange_api_latency,
            observe_exchange_api_latency,
        )

        observe_exchange_api_latency(
            exchange_id="binance",
            endpoint="/api/v3/order",
            seconds=0.250,
        )
        val = (
            get_exchange_api_latency()
            .labels(exchange_id="binance", endpoint="/api/v3/order")
            ._sum.get()
        )
        assert val == 0.250

    def test_record_ws_reconnect(self) -> None:
        from hydra.dashboard.metrics import get_ws_reconnects, record_ws_reconnect

        record_ws_reconnect(exchange_id="bybit")
        record_ws_reconnect(exchange_id="bybit")
        val = get_ws_reconnects().labels(exchange_id="bybit")._value.get()
        assert val == 2.0

    def test_observe_order_fill_latency(self) -> None:
        from hydra.dashboard.metrics import get_order_fill_latency, observe_order_fill_latency

        observe_order_fill_latency(1.2)
        assert get_order_fill_latency()._sum.get() == 1.2

    def test_observe_ml_inference_latency(self) -> None:
        from hydra.dashboard.metrics import (
            get_ml_inference_latency,
            observe_ml_inference_latency,
        )

        observe_ml_inference_latency(model_name="lstm-v2", seconds=0.003)
        val = get_ml_inference_latency().labels(model_name="lstm-v2")._sum.get()
        assert val == 0.003

    def test_update_data_gap(self) -> None:
        from hydra.dashboard.metrics import get_data_gap, update_data_gap

        update_data_gap(exchange_id="okx", symbol="BTCUSDT", gap_seconds=45.0)
        val = get_data_gap().labels(exchange_id="okx", symbol="BTCUSDT")._value.get()
        assert val == 45.0

    def test_update_reconciliation_mismatch(self) -> None:
        from hydra.dashboard.metrics import (
            get_reconciliation_mismatch,
            update_reconciliation,
        )

        update_reconciliation(exchange_id="binance", symbol="BTCUSDT", mismatch=True)
        val = (
            get_reconciliation_mismatch()
            .labels(exchange_id="binance", symbol="BTCUSDT")
            ._value.get()
        )
        assert val == 1.0

    def test_update_reconciliation_no_mismatch(self) -> None:
        from hydra.dashboard.metrics import (
            get_reconciliation_mismatch,
            update_reconciliation,
        )

        update_reconciliation(exchange_id="binance", symbol="BTCUSDT", mismatch=False)
        val = (
            get_reconciliation_mismatch()
            .labels(exchange_id="binance", symbol="BTCUSDT")
            ._value.get()
        )
        assert val == 0.0


# ---------------------------------------------------------------------------
# /metrics endpoint tests
# ---------------------------------------------------------------------------


class TestMetricsEndpoint:
    """Verify the FastAPI /metrics endpoint returns Prometheus text format."""

    def test_metrics_endpoint_returns_200(self, client: TestClient) -> None:
        resp = client.get("/metrics")
        assert resp.status_code == 200

    def test_metrics_endpoint_content_type(self, client: TestClient) -> None:
        resp = client.get("/metrics")
        content_type = resp.headers.get("content-type", "")
        assert "text/plain" in content_type or "text/openmetrics" in content_type

    def test_metrics_endpoint_contains_hydra_metrics(self, client: TestClient) -> None:
        from hydra.dashboard.metrics import update_portfolio

        update_portfolio(value=50_000.0, drawdown_pct=1.0, daily_pnl=100.0)

        resp = client.get("/metrics")
        body = resp.text
        assert "hydra_portfolio_value" in body
        assert "hydra_drawdown_pct" in body
        assert "hydra_daily_pnl" in body

    def test_metrics_endpoint_contains_histogram_buckets(self, client: TestClient) -> None:
        from hydra.dashboard.metrics import observe_event_bus_latency

        observe_event_bus_latency(0.01)

        resp = client.get("/metrics")
        body = resp.text
        assert "hydra_event_bus_latency_seconds_bucket" in body
        assert "hydra_event_bus_latency_seconds_count" in body
        assert "hydra_event_bus_latency_seconds_sum" in body


# ---------------------------------------------------------------------------
# Lazy initialization tests
# ---------------------------------------------------------------------------


class TestLazyInitialization:
    """Verify metrics are not initialized until first access."""

    def test_module_import_does_not_initialize(self) -> None:
        import hydra.dashboard.metrics as m

        # After reset, _initialized should be False
        assert m._initialized is False

    def test_first_access_initializes(self) -> None:
        import hydra.dashboard.metrics as m

        assert m._initialized is False
        m.get_portfolio_value()
        assert m._initialized is True

    def test_reset_clears_state(self) -> None:
        import hydra.dashboard.metrics as m

        m.get_portfolio_value()
        assert m._initialized is True
        m._reset_metrics()
        assert m._initialized is False
        assert len(m._metrics) == 0
