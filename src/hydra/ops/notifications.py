"""Push notification dispatchers for Discord, Slack and Telegram.

Notifiers are fire-and-forget: failures are logged but never re-raised so that
a misconfigured webhook never interrupts trading logic.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

# Discord embed color constants (callers may override)
COLOR_GREEN = 0x00FF00
COLOR_RED = 0xFF0000
COLOR_YELLOW = 0xFFFF00

# The only Telegram host production ever talks to; tests override the constructor's base_url.
TELEGRAM_API_URL = "https://api.telegram.org"


class DiscordWebhookNotifier:
    """Send notifications to Discord via webhook.

    Parameters
    ----------
    webhook_url:
        Full Discord webhook URL obtained from Server Settings → Integrations.
    """

    def __init__(self, webhook_url: str) -> None:
        self._webhook_url = webhook_url

    async def send(self, title: str, message: str, color: int = COLOR_GREEN) -> bool:
        """Post an embed to Discord.

        Returns
        -------
        bool
            ``True`` when Discord accepted the request (2xx), ``False`` otherwise.
        """
        payload = {
            "embeds": [
                {
                    "title": title,
                    "description": message,
                    "color": color,
                }
            ]
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(self._webhook_url, json=payload)
                response.raise_for_status()
            return True
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Discord webhook returned %s: %s",
                exc.response.status_code,
                exc.response.text,
            )
            return False
        except Exception:
            logger.warning("Discord webhook request failed", exc_info=True)
            return False


class SlackWebhookNotifier:
    """Send notifications to Slack via incoming webhook.

    Parameters
    ----------
    webhook_url:
        Full Slack webhook URL obtained from api.slack.com/apps.
    """

    def __init__(self, webhook_url: str) -> None:
        self._webhook_url = webhook_url

    async def send(self, title: str, message: str) -> bool:
        """Post a Block Kit message to Slack.

        Returns
        -------
        bool
            ``True`` when Slack accepted the request, ``False`` otherwise.
        """
        payload = {
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": title},
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": message},
                },
            ]
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(self._webhook_url, json=payload)
                response.raise_for_status()
            return True
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Slack webhook returned %s: %s",
                exc.response.status_code,
                exc.response.text,
            )
            return False
        except Exception:
            logger.warning("Slack webhook request failed", exc_info=True)
            return False


class TelegramNotifier:
    """Send notifications to Telegram through a bot's sendMessage endpoint.

    Outbound only (the removed inbound bot stays dead): no getUpdates, no
    commands, no long polling. ``base_url`` exists so tests can point the real
    client at a local server; production never overrides it.

    Parameters
    ----------
    bot_token:
        The bot's token from BotFather, ``<digits>:<token body>``.
    chat_id:
        The target chat: a numeric id or a ``@channelname``.
    """

    def __init__(self, bot_token: str, chat_id: str, *, base_url: str = TELEGRAM_API_URL) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._url = f"{base_url}/bot{bot_token}/sendMessage"

    async def send(self, title: str, message: str) -> bool:
        """Post ``title`` and ``message`` as one text to the chat.

        Returns
        -------
        bool
            ``True`` when Telegram accepted the request (2xx), ``False`` otherwise.
        """
        payload = {"chat_id": self._chat_id, "text": f"{title}\n\n{message}"}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(self._url, json=payload)
                response.raise_for_status()
            return True
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Telegram sendMessage returned %s: %s",
                exc.response.status_code,
                exc.response.text,
            )
            return False
        except Exception:
            # No exc_info: an httpx exception repr embeds the URL, and the URL has the token.
            logger.warning("Telegram sendMessage request failed")
            return False


class NotificationDispatcher:
    """Route notifications to all configured channels.

    Configure once at startup, then call ``notify()`` from anywhere.  Every
    channel is attempted independently — a failure on one does not suppress
    the others.

    Example
    -------
    ::

        dispatcher = NotificationDispatcher()
        dispatcher.configure(
            discord_url="https://discord.com/api/webhooks/...",
            slack_url="https://hooks.slack.com/services/...",
            telegram_token="123456:ABC-DEF...",
            telegram_chat_id="-100200300400",
        )
        await dispatcher.notify("Trade filled", "BTC/USDT long @ 65000", color=0x00ff00)
    """

    def __init__(self) -> None:
        self._discord: DiscordWebhookNotifier | None = None
        self._slack: SlackWebhookNotifier | None = None
        self._telegram: TelegramNotifier | None = None

    def configure(
        self,
        discord_url: str = "",
        slack_url: str = "",
        telegram_token: str = "",
        telegram_chat_id: str = "",
        telegram_base_url: str = TELEGRAM_API_URL,
    ) -> None:
        """Set or replace webhook targets.

        Passing an empty string for a channel leaves it unconfigured (disabled);
        the Telegram channel needs both the token and the chat id.
        ``telegram_base_url`` is the notifier's test seam and never overridden
        in production.
        """
        self._discord = DiscordWebhookNotifier(discord_url) if discord_url else None
        self._slack = SlackWebhookNotifier(slack_url) if slack_url else None
        self._telegram = (
            TelegramNotifier(telegram_token, telegram_chat_id, base_url=telegram_base_url)
            if telegram_token and telegram_chat_id
            else None
        )

    async def notify(self, title: str, message: str, color: int = COLOR_GREEN) -> None:
        """Send *title* / *message* to every configured channel.

        Errors are logged and swallowed — this is intentionally fire-and-forget.
        """
        if self._discord is not None:
            try:
                await self._discord.send(title, message, color)
            except Exception:
                logger.exception("Unexpected error dispatching Discord notification")

        if self._slack is not None:
            try:
                await self._slack.send(title, message)
            except Exception:
                logger.exception("Unexpected error dispatching Slack notification")

        if self._telegram is not None:
            try:
                await self._telegram.send(title, message)
            except Exception:
                logger.exception("Unexpected error dispatching Telegram notification")
