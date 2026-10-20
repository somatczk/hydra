"""The Prometheus metric contract in hydra.core.metrics, read through the real exposition."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY, Counter, Gauge, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from hydra.core import metrics

REPO_ROOT = Path(__file__).resolve().parents[3]
TABLE_ROW = re.compile(r"^- (hydra_\w+): (\w+), (.+)\n  labels: (.+)\n  producer: (.+)$", re.M)


@pytest.fixture(autouse=True)
def _fresh_registry() -> Iterator[None]:
    metrics._reset_metrics()
    yield
    metrics._reset_metrics()


def _families() -> dict[str, str]:
    text = generate_latest().decode()
    return {f.name: f.type for f in text_string_to_metric_families(text)}


def _hydra_samples() -> list[tuple[str, dict[str, str], float]]:
    text = generate_latest().decode()
    return [
        (s.name, dict(s.labels), s.value)
        for f in text_string_to_metric_families(text)
        for s in f.samples
        if s.name.startswith("hydra_")
    ]


def _value(name: str, labels: dict[str, str] | None = None) -> float | None:
    return REGISTRY.get_sample_value(name, labels or {})


class TestContract:
    def test_docstring_table_matches_contract(self) -> None:
        assert metrics.__doc__ is not None
        rows = TABLE_ROW.findall(metrics.__doc__)
        table = {
            name: (
                kind,
                () if labels == "-" else tuple(x.strip() for x in labels.split(",")),
                unit,
                producer,
            )
            for name, kind, unit, labels, producer in rows
        }
        contract = {s.name: (s.kind, s.labels, s.unit, s.producer) for s in metrics.CONTRACT}
        assert table == contract

    def test_names_are_unique_and_prefixed(self) -> None:
        names = [s.name for s in metrics.CONTRACT]
        assert len(names) == len(set(names))
        assert all(n.startswith("hydra_") for n in names)
        assert all(s.help for s in metrics.CONTRACT)

    def test_every_contract_metric_is_exported_with_its_type(self) -> None:
        for spec in metrics.CONTRACT:
            metrics._collector(spec.name)
        families = _families()
        for spec in metrics.CONTRACT:
            family = spec.name.removesuffix("_total") if spec.kind == "counter" else spec.name
            assert families.get(family) == spec.kind, spec.name

    def test_samples_property_matches_exposition(self) -> None:
        for spec in metrics.CONTRACT:
            collector = metrics._collector(spec.name)
            child = collector.labels(*["x"] * len(spec.labels)) if spec.labels else collector
            if isinstance(child, Counter):
                child.inc()
            elif isinstance(child, Gauge):
                child.set(1)
            else:
                child.observe(1)
        exposed = {name for name, _, _ in _hydra_samples() if not name.endswith("_created")}
        expected = {sample for spec in metrics.CONTRACT for sample in spec.samples}
        assert exposed == expected

    def test_renamed_and_dropped_metrics_are_gone(self) -> None:
        for spec in metrics.CONTRACT:
            metrics._collector(spec.name)
        families = _families()
        assert "hydra_drawdown_pct" not in families
        assert "hydra_data_gap_seconds" not in families
        assert "hydra_reconciliation_mismatch" not in families
        assert "hydra_drawdown_ratio" in families

    def test_before_any_event_only_build_info_and_unlabelled_zero_counts_exist(self) -> None:
        metrics._ensure_initialized()
        samples = [s for s in _hydra_samples() if not s[0].endswith("_created")]
        build_info = ("hydra_build_info", {"version": _pyproject_version()}, 1.0)
        assert samples[0] == build_info
        rest = samples[1:]
        assert {name for name, _, _ in rest} == {
            "hydra_event_bus_latency_seconds_bucket",
            "hydra_event_bus_latency_seconds_count",
            "hydra_event_bus_latency_seconds_sum",
            "hydra_order_fill_latency_seconds_bucket",
            "hydra_order_fill_latency_seconds_count",
            "hydra_order_fill_latency_seconds_sum",
            "hydra_risk_lock_wait_seconds_bucket",
            "hydra_risk_lock_wait_seconds_count",
            "hydra_risk_lock_wait_seconds_sum",
            "hydra_risk_state_persist_failures_total",
            "hydra_risk_lock_timeouts_total",
        }
        assert all(value == 0.0 for _, _, value in rest)
        assert all(set(labels) <= {"le"} for _, labels, _ in rest)
        assert not any(v in ("init", "none") for _, labels, _ in samples for v in labels.values())

    def test_first_increment_of_an_unlabelled_counter_is_a_step_from_zero(self) -> None:
        metrics._ensure_initialized()
        assert _value("hydra_risk_state_persist_failures_total") == 0.0
        metrics.record_risk_state_persist_failure()
        assert _value("hydra_risk_state_persist_failures_total") == 1.0

    def test_no_gauge_reports_a_value_before_its_producer_sets_it(self) -> None:
        metrics._ensure_initialized()
        for name in (
            "hydra_kill_switch_active",
            "hydra_drawdown_ratio",
            "hydra_portfolio_value",
            "hydra_portfolio_heat_ratio",
        ):
            assert _value(name) is None, name

    def test_build_info_reports_pyproject_version(self) -> None:
        metrics._ensure_initialized()
        assert _value("hydra_build_info", {"version": _pyproject_version()}) == 1.0


def _pyproject_version() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return str(data["project"]["version"])


class TestTradingSetters:
    def test_record_trade_counts_and_observes_pnl(self) -> None:
        metrics.record_trade("BTCUSDT", "SELL", "s1", "binance", pnl=-15.0)
        metrics.record_trade("BTCUSDT", "SELL", "s1", "binance")
        labels = {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "strategy_id": "s1",
            "exchange_id": "binance",
        }
        assert _value("hydra_trades_total", labels) == 2.0
        pnl = {"symbol": "BTCUSDT", "strategy_id": "s1"}
        assert _value("hydra_trade_pnl_count", pnl) == 1.0
        assert _value("hydra_trade_pnl_sum", pnl) is None
        assert _value("hydra_trade_pnl_bucket", {**pnl, "le": "-10.0"}) == 1.0
        assert _value("hydra_trade_pnl_bucket", {**pnl, "le": "-20.0"}) == 0.0

    def test_update_portfolio_sets_ratio_drawdown(self) -> None:
        metrics.update_portfolio(8800.0, 0.12, -1200.0)
        assert _value("hydra_portfolio_value") == 8800.0
        assert _value("hydra_drawdown_ratio") == 0.12
        assert _value("hydra_daily_pnl") == -1200.0

    def test_update_portfolio_rejects_percent_drawdown(self) -> None:
        with pytest.raises(ValueError, match="fraction"):
            metrics.update_portfolio(8800.0, 12.0, 0.0)
        assert _value("hydra_drawdown_ratio") is None

    def test_out_of_range_drawdown_still_updates_equity_and_daily_pnl(self) -> None:
        metrics.update_portfolio(5000.0, 0.1, 10.0)
        with pytest.raises(ValueError, match="fraction"):
            metrics.update_portfolio(10_000.0, 1.2, -25.0)
        assert _value("hydra_portfolio_value") == 10_000.0
        assert _value("hydra_daily_pnl") == -25.0
        assert _value("hydra_drawdown_ratio") == 0.1
        with pytest.raises(ValueError, match="fraction"):
            metrics.update_portfolio(9000.0, -0.01, 3.0)
        assert _value("hydra_portfolio_value") == 9000.0
        assert _value("hydra_daily_pnl") == 3.0

    def test_heat_signal_and_spot_short(self) -> None:
        metrics.set_portfolio_heat(0.18)
        metrics.record_signal("s1", "entry_long")
        metrics.record_spot_short_ignored("s1", "entry_short")
        assert _value("hydra_portfolio_heat_ratio") == 0.18
        signal = {"strategy_id": "s1", "signal_type": "entry_long"}
        assert _value("hydra_signal_count_total", signal) == 1.0
        short = {"strategy_id": "s1", "signal_type": "entry_short"}
        assert _value("hydra_spot_short_ignored_total", short) == 1.0
        with pytest.raises(ValueError):
            metrics.record_spot_short_ignored("s1", "entry_long")
        with pytest.raises(ValueError):
            metrics.set_portfolio_heat(-0.1)


class TestPositions:
    def test_two_sessions_on_one_symbol_keep_their_own_series(self) -> None:
        metrics.set_session_position("sa", "trend", "binance", "BTCUSDT", 0.5)
        metrics.set_session_position("sb", "meanrev", "binance", "BTCUSDT", 0.0)
        a = {"session_id": "sa", "strategy_id": "trend", "exchange_id": "binance"}
        b = {"session_id": "sb", "strategy_id": "meanrev", "exchange_id": "binance"}
        assert _value("hydra_position_size", {**a, "symbol": "BTCUSDT"}) == 0.5
        assert _value("hydra_position_size", {**b, "symbol": "BTCUSDT"}) == 0.0

    def test_forget_session_removes_only_that_sessions_positions(self) -> None:
        metrics.set_session_position("sa", "trend", "binance", "BTCUSDT", 0.5)
        metrics.set_session_position("sa", "trend", "binance", "ETHUSDT", 2.0)
        metrics.set_session_position("sb", "meanrev", "binance", "BTCUSDT", 0.1)
        metrics.forget_session("sa", "trend", "paper")
        positions = [s for s in _hydra_samples() if s[0] == "hydra_position_size"]
        assert positions == [
            (
                "hydra_position_size",
                {
                    "session_id": "sb",
                    "strategy_id": "meanrev",
                    "exchange_id": "binance",
                    "symbol": "BTCUSDT",
                },
                0.1,
            )
        ]

    def test_legacy_update_position_exports_nothing(self) -> None:
        metrics.update_position("BTCUSDT", "binance", 0.25)
        metrics.update_position("BTCUSDT", "binance", 0.0)
        assert [s for s in _hydra_samples() if s[0] == "hydra_position_size"] == []


class TestFeedFreshness:
    def test_mark_feed_message_uses_wall_clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(metrics, "_wall_clock", lambda: 1_760_000_000.5)
        metrics.mark_feed_message("binance", "BTCUSDT", "1m")
        labels = {"exchange_id": "binance", "symbol": "BTCUSDT", "timeframe": "1m"}
        assert _value("hydra_feed_last_message_timestamp_seconds", labels) == 1_760_000_000.5

    def test_closed_bar_and_interval_then_forget(self) -> None:
        labels = {"exchange_id": "binance", "symbol": "BTCUSDT", "timeframe": "1m"}
        metrics.mark_feed_message("binance", "BTCUSDT", "1m", at=1_760_000_065.0)
        metrics.mark_closed_bar("binance", "BTCUSDT", "1m", 1_760_000_000.0)
        metrics.set_feed_bar_interval("binance", "BTCUSDT", "1m", 60)
        assert _value("hydra_feed_last_closed_bar_timestamp_seconds", labels) == 1_760_000_000.0
        assert _value("hydra_feed_bar_interval_seconds", labels) == 60.0

        metrics.forget_feed("binance", "BTCUSDT", "1m")
        body = generate_latest().decode()
        assert 'symbol="BTCUSDT"' not in body
        assert _value("hydra_feed_last_message_timestamp_seconds", labels) is None

    def test_forget_feed_keeps_other_streams(self) -> None:
        metrics.mark_feed_message("binance", "BTCUSDT", "1m", at=100.0)
        metrics.mark_feed_message("binance", "BTCUSDT", "1h", at=200.0)
        metrics.forget_feed("binance", "BTCUSDT", "1m")
        kept = {"exchange_id": "binance", "symbol": "BTCUSDT", "timeframe": "1h"}
        assert _value("hydra_feed_last_message_timestamp_seconds", kept) == 200.0

    def test_closed_bar_rejects_milliseconds(self) -> None:
        with pytest.raises(ValueError, match="Unix seconds"):
            metrics.mark_closed_bar("binance", "BTCUSDT", "1m", 1_760_000_000_000.0)

    def test_feed_counters(self) -> None:
        metrics.record_feed_bar("binance", "BTCUSDT", "1m", "rest_gap")
        metrics.record_feed_bar_rejected("binance", "BTCUSDT", "high_below_low")
        metrics.record_feed_anomaly("binance", "BTCUSDT", "spike")
        metrics.record_feed_persist_error("binance")
        metrics.record_event_handler_error("bar")
        bar = {
            "exchange_id": "binance",
            "symbol": "BTCUSDT",
            "timeframe": "1m",
            "source": "rest_gap",
        }
        assert _value("hydra_feed_bars_total", bar) == 1.0
        rejected = {"exchange_id": "binance", "symbol": "BTCUSDT", "reason": "high_below_low"}
        assert _value("hydra_feed_bars_rejected_total", rejected) == 1.0
        anomaly = {"exchange_id": "binance", "symbol": "BTCUSDT", "kind": "spike"}
        assert _value("hydra_feed_anomalies_total", anomaly) == 1.0
        assert _value("hydra_feed_persist_errors_total", {"exchange_id": "binance"}) == 1.0
        assert _value("hydra_event_handler_errors_total", {"event_type": "bar"}) == 1.0
        with pytest.raises(ValueError):
            metrics.record_feed_bar("binance", "BTCUSDT", "1m", "replay")


class TestRiskAndHealthSetters:
    def test_risk_metrics(self) -> None:
        metrics.set_circuit_breaker_tier("paper", 3)
        metrics.set_kill_switch_active(True)
        metrics.record_risk_rejection("max_position_pct", "open")
        metrics.record_risk_state_persist_failure()
        metrics.observe_risk_lock_wait(0.3)
        metrics.record_risk_lock_timeout()
        assert _value("hydra_circuit_breaker_active_tier", {"scope": "paper"}) == 3.0
        assert _value("hydra_kill_switch_active") == 1.0
        rejection = {"check": "max_position_pct", "intent": "open"}
        assert _value("hydra_risk_rejections_total", rejection) == 1.0
        assert _value("hydra_risk_state_persist_failures_total") == 1.0
        assert _value("hydra_risk_lock_wait_seconds_bucket", {"le": "0.25"}) == 0.0
        assert _value("hydra_risk_lock_wait_seconds_bucket", {"le": "0.5"}) == 1.0
        assert _value("hydra_risk_lock_timeouts_total") == 1.0
        with pytest.raises(ValueError):
            metrics.set_circuit_breaker_tier("paper", 5)
        with pytest.raises(ValueError):
            metrics.record_risk_rejection("max_position_pct", "OPENING")

    def test_session_state_and_forget(self) -> None:
        labels = {"session_id": "s1", "strategy_id": "sma", "mode": "paper"}
        metrics.set_session_state("s1", "sma", "paper", desired=True, supervised=False)
        metrics.set_paper_persist_dirty("s1", True)
        metrics.record_session_errored("sma")
        assert _value("hydra_session_desired", labels) == 1.0
        assert _value("hydra_session_supervised", labels) == 0.0
        assert _value("hydra_paper_persist_dirty", {"session_id": "s1"}) == 1.0
        assert _value("hydra_sessions_errored_total", {"strategy_id": "sma"}) == 1.0

        metrics.forget_session("s1", "sma", "paper")
        assert _value("hydra_session_desired", labels) is None
        assert _value("hydra_session_supervised", labels) is None
        assert _value("hydra_paper_persist_dirty", {"session_id": "s1"}) is None

    def test_ready_and_monitor_run(self) -> None:
        metrics.set_ready("db", False)
        metrics.mark_health_monitor_run(at=1_760_000_123.0)
        assert _value("hydra_ready", {"check": "db"}) == 0.0
        assert _value("hydra_health_monitor_last_run_timestamp_seconds") == 1_760_000_123.0
        with pytest.raises(ValueError):
            metrics.set_ready("redis", True)

    def test_latency_helpers(self) -> None:
        metrics.observe_event_bus_latency(0.004)
        metrics.observe_order_fill_latency(0.3)
        metrics.observe_ml_inference_latency("xgb", 0.02)
        metrics.record_ws_reconnect("binance")
        assert _value("hydra_event_bus_latency_seconds_bucket", {"le": "0.005"}) == 1.0
        assert _value("hydra_order_fill_latency_seconds_bucket", {"le": "0.25"}) == 0.0
        assert _value("hydra_order_fill_latency_seconds_bucket", {"le": "0.5"}) == 1.0
        assert _value("hydra_ml_inference_latency_seconds_count", {"model_name": "xgb"}) == 1.0
        assert _value("hydra_ws_reconnects_total", {"exchange_id": "binance"}) == 1.0


class TestReconciliation:
    def test_hard_coded_no_mismatch_exports_no_sample(self) -> None:
        metrics.update_reconciliation("binance", "BTC/USDT", mismatch=False)
        metrics.update_reconciliation("binance", "BTC/USDT", mismatch=True)
        assert not [s for s in _hydra_samples() if "reconciliation" in s[0]]
        assert "hydra_reconciliation_mismatch" not in {s.name for s in metrics.CONTRACT}


class TestReset:
    def test_reset_unregisters_and_reinitializes(self) -> None:
        metrics._ensure_initialized()
        assert metrics._initialized is True
        metrics._reset_metrics()
        assert metrics._initialized is False
        assert metrics._collectors == {}
        assert "hydra_build_info" not in _families()
        metrics.update_portfolio(1.0, 0.0, 0.0)
        assert _value("hydra_portfolio_value") == 1.0


class TestMetricsEndpoint:
    @pytest.fixture()
    def client(self) -> TestClient:
        from hydra.dashboard.api import app

        return TestClient(app)

    def test_endpoint_serves_contract_in_text_format(self, client: TestClient) -> None:
        metrics.update_portfolio(50_000.0, 0.02, 100.0)
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        assert "hydra_drawdown_ratio 0.02" in resp.text
        assert "hydra_portfolio_value 50000.0" in resp.text
        assert "hydra_drawdown_pct" not in resp.text
        assert f'hydra_build_info{{version="{_pyproject_version()}"}} 1.0' in resp.text
