"""Tests for the Hydra Telegram notifier."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hydra.dashboard.telegram import TelegramNotifier


@pytest.fixture()
def notifier() -> TelegramNotifier:
    return TelegramNotifier(bot_token="test-token-123", chat_id="test-chat-456")  # noqa: S106


# ---------------------------------------------------------------------------
# send_trade_notification
# ---------------------------------------------------------------------------


class TestTradeNotification:
    async def test_formats_entry_trade(self, notifier: TelegramNotifier) -> None:
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        with patch.object(notifier, "_get_bot", return_value=mock_bot):
            await notifier.send_trade_notification(
                {
                    "action": "entry",
                    "side": "Long",
                    "pair": "BTC/USDT",
                    "price": 67420.0,
                    "size": 0.15,
                }
            )

        mock_bot.send_message.assert_awaited_once()
        call_kwargs = mock_bot.send_message.call_args
        text = call_kwargs.kwargs["text"]
        assert "Trade Entry" in text
        assert "BTC/USDT" in text
        assert "Long" in text

    async def test_includes_pnl_when_present(self, notifier: TelegramNotifier) -> None:
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        with patch.object(notifier, "_get_bot", return_value=mock_bot):
            await notifier.send_trade_notification(
                {
                    "action": "exit",
                    "side": "Long",
                    "pair": "BTC/USDT",
                    "price": 68100.0,
                    "size": 0.15,
                    "pnl": 102.0,
                }
            )

        text = mock_bot.send_message.call_args.kwargs["text"]
        assert "PnL" in text
        assert "+$102.00" in text

    async def test_negative_pnl(self, notifier: TelegramNotifier) -> None:
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        with patch.object(notifier, "_get_bot", return_value=mock_bot):
            await notifier.send_trade_notification(
                {
                    "action": "exit",
                    "side": "Short",
                    "pair": "BTC/USDT",
                    "price": 68500.0,
                    "size": 0.08,
                    "pnl": -48.90,
                }
            )

        text = mock_bot.send_message.call_args.kwargs["text"]
        assert "-$48.90" in text


# ---------------------------------------------------------------------------
# send_circuit_breaker_alert
# ---------------------------------------------------------------------------


class TestCircuitBreakerAlert:
    async def test_includes_tier(self, notifier: TelegramNotifier) -> None:
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        with patch.object(notifier, "_get_bot", return_value=mock_bot):
            await notifier.send_circuit_breaker_alert(tier=3, action="Tripped")

        text = mock_bot.send_message.call_args.kwargs["text"]
        assert "Circuit Breaker Alert" in text
        assert "3" in text
        assert "Tripped" in text

    async def test_tier_1_alert(self, notifier: TelegramNotifier) -> None:
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        with patch.object(notifier, "_get_bot", return_value=mock_bot):
            await notifier.send_circuit_breaker_alert(tier=1, action="Reset")

        text = mock_bot.send_message.call_args.kwargs["text"]
        assert "1" in text
        assert "Reset" in text


# ---------------------------------------------------------------------------
# send_daily_summary
# ---------------------------------------------------------------------------


class TestDailySummary:
    async def test_includes_pnl_and_trades(self, notifier: TelegramNotifier) -> None:
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        with patch.object(notifier, "_get_bot", return_value=mock_bot):
            await notifier.send_daily_summary(
                {
                    "date": "2026-03-14",
                    "pnl": 285.50,
                    "trades": 7,
                    "drawdown": 4.2,
                    "win_rate": 71.4,
                }
            )

        text = mock_bot.send_message.call_args.kwargs["text"]
        assert "Daily Summary" in text
        assert "+$285.50" in text
        assert "Trades: 7" in text
        assert "4.2%" in text

    async def test_negative_pnl_summary(self, notifier: TelegramNotifier) -> None:
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        with patch.object(notifier, "_get_bot", return_value=mock_bot):
            await notifier.send_daily_summary(
                {
                    "date": "2026-03-13",
                    "pnl": -120.0,
                    "trades": 5,
                    "drawdown": 6.1,
                    "win_rate": 40.0,
                }
            )

        text = mock_bot.send_message.call_args.kwargs["text"]
        assert "-$120.00" in text


# ---------------------------------------------------------------------------
# send_error_alert
# ---------------------------------------------------------------------------


class TestErrorAlert:
    async def test_sends_error(self, notifier: TelegramNotifier) -> None:
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        with patch.object(notifier, "_get_bot", return_value=mock_bot):
            await notifier.send_error_alert("Exchange connection timeout on Binance")

        text = mock_bot.send_message.call_args.kwargs["text"]
        assert "Error Alert" in text
        assert "Exchange connection timeout on Binance" in text


# ---------------------------------------------------------------------------
# send_model_notification
# ---------------------------------------------------------------------------


class TestModelNotification:
    async def test_model_promoted(self, notifier: TelegramNotifier) -> None:
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        with patch.object(notifier, "_get_bot", return_value=mock_bot):
            await notifier.send_model_notification(
                event="Promoted to Production",
                model_name="LSTM Price Predictor",
            )

        text = mock_bot.send_message.call_args.kwargs["text"]
        assert "Model Notification" in text
        assert "LSTM Price Predictor" in text
        assert "Promoted to Production" in text


# ---------------------------------------------------------------------------
# Lazy bot initialization
# ---------------------------------------------------------------------------


class TestLazyInit:
    def test_bot_not_imported_at_init(self, notifier: TelegramNotifier) -> None:
        """Verify that _bot is None until _get_bot is called."""
        assert notifier._bot is None

    def test_chat_id_stored(self, notifier: TelegramNotifier) -> None:
        assert notifier.chat_id == "test-chat-456"

    def test_bot_token_stored(self, notifier: TelegramNotifier) -> None:
        assert notifier.bot_token == "test-token-123"  # noqa: S105
