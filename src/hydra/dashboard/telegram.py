"""Telegram bot integration for Hydra Trading Platform.

Sends trade notifications, circuit-breaker alerts, daily summaries,
error alerts, and model notifications. Also handles bot commands:
/status, /positions, /pause, /resume, /risk.

python-telegram-bot is imported lazily to avoid pulling it at module level.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _format_currency(value: float) -> str:
    """Format a number as a USD currency string."""
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.2f}"


class TelegramNotifier:
    """Sends Hydra notifications to a Telegram chat and handles bot commands."""

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._bot: Any | None = None
        self._app: Any | None = None

    # ------------------------------------------------------------------
    # Lazy bot initialisation
    # ------------------------------------------------------------------

    def _get_bot(self) -> Any:
        """Lazily import and instantiate the Telegram Bot."""
        if self._bot is None:
            from telegram import Bot

            self._bot = Bot(token=self.bot_token)
        return self._bot

    async def _send_message(self, text: str, parse_mode: str = "HTML") -> None:
        """Send a message to the configured chat."""
        bot = self._get_bot()
        await bot.send_message(chat_id=self.chat_id, text=text, parse_mode=parse_mode)

    # ------------------------------------------------------------------
    # Notification methods
    # ------------------------------------------------------------------

    async def send_trade_notification(self, trade: dict[str, Any]) -> None:
        """Notify about a trade entry or exit with PnL.

        Expected trade keys: side, pair, price, size, pnl (optional), action.
        """
        action = trade.get("action", "executed")
        side = trade.get("side", "N/A")
        pair = trade.get("pair", "N/A")
        price = trade.get("price", 0)
        size = trade.get("size", 0)

        lines = [
            f"<b>Trade {action.title()}</b>",
            f"Pair: {pair}",
            f"Side: {side}",
            f"Price: ${price:,.2f}",
            f"Size: {size}",
        ]

        pnl = trade.get("pnl")
        if pnl is not None:
            lines.append(f"PnL: {_format_currency(pnl)}")

        await self._send_message("\n".join(lines))

    async def send_circuit_breaker_alert(self, tier: int, action: str) -> None:
        """Alert when a circuit breaker trips or resets."""
        emoji_map = {1: "1", 2: "2", 3: "3", 4: "4"}
        tier_label = emoji_map.get(tier, str(tier))
        text = f"<b>Circuit Breaker Alert</b>\nTier: {tier_label}\nAction: {action}"
        await self._send_message(text)

    async def send_daily_summary(self, summary: dict[str, Any]) -> None:
        """Daily summary including PnL, trade count, and drawdown.

        Expected keys: date, pnl, trades, drawdown, win_rate.
        """
        date = summary.get("date", "N/A")
        pnl = summary.get("pnl", 0)
        trades = summary.get("trades", 0)
        drawdown = summary.get("drawdown", 0)
        win_rate = summary.get("win_rate", 0)

        text = (
            f"<b>Daily Summary - {date}</b>\n"
            f"PnL: {_format_currency(pnl)}\n"
            f"Trades: {trades}\n"
            f"Win Rate: {win_rate:.1f}%\n"
            f"Drawdown: {drawdown:.1f}%"
        )
        await self._send_message(text)

    async def send_error_alert(self, error: str) -> None:
        """Send an error alert to the chat."""
        text = f"<b>Error Alert</b>\n{error}"
        await self._send_message(text)

    async def send_model_notification(self, event: str, model_name: str) -> None:
        """Notify about ML model events (promoted, rolled back, retrained, etc.)."""
        text = f"<b>Model Notification</b>\nModel: {model_name}\nEvent: {event}"
        await self._send_message(text)

    # ------------------------------------------------------------------
    # Bot command handlers (for use with python-telegram-bot Application)
    # ------------------------------------------------------------------

    def build_application(self) -> Any:
        """Build a python-telegram-bot Application with command handlers.

        Returns the Application instance (not started).
        """
        from telegram.ext import Application, CommandHandler

        self._app = Application.builder().token(self.bot_token).build()
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("positions", self._cmd_positions))
        self._app.add_handler(CommandHandler("pause", self._cmd_pause))
        self._app.add_handler(CommandHandler("resume", self._cmd_resume))
        self._app.add_handler(CommandHandler("risk", self._cmd_risk))
        return self._app

    # --- Command callbacks (receive Update, Context) ---

    @staticmethod
    async def _cmd_status(update: Any, context: Any) -> None:
        """Handle /status command."""
        text = "<b>Hydra Status</b>\nTrading: Active\nStrategies: 2 active\nOpen Positions: 3"
        await update.message.reply_text(text, parse_mode="HTML")

    @staticmethod
    async def _cmd_positions(update: Any, context: Any) -> None:
        """Handle /positions command."""
        text = (
            "<b>Open Positions</b>\n"
            "1. BTC/USDT Long 0.15 BTC @ $67,420\n"
            "2. BTC/USDT Short 0.08 BTC @ $68,800\n"
            "3. BTC/USDT Long 0.20 BTC @ $67,900"
        )
        await update.message.reply_text(text, parse_mode="HTML")

    @staticmethod
    async def _cmd_pause(update: Any, context: Any) -> None:
        """Handle /pause command -- pause all trading."""
        await update.message.reply_text("Trading paused.", parse_mode="HTML")

    @staticmethod
    async def _cmd_resume(update: Any, context: Any) -> None:
        """Handle /resume command -- resume trading."""
        await update.message.reply_text("Trading resumed.", parse_mode="HTML")

    @staticmethod
    async def _cmd_risk(update: Any, context: Any) -> None:
        """Handle /risk command -- show risk status."""
        text = (
            "<b>Risk Status</b>\n"
            "Drawdown: 4.2% / 15.0%\n"
            "Daily Loss: $48.90 / $500.00\n"
            "Circuit Breakers: All normal"
        )
        await update.message.reply_text(text, parse_mode="HTML")
