"""Push notification dispatchers for Discord and Slack webhooks.

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


class NotificationDispatcher:
    """Route notifications to all configured channels.

    Configure once at startup, then call ``notify()`` from anywhere.  Both
    channels are attempted independently — a failure on one does not suppress
    the other.

    Example
    -------
    ::

        dispatcher = NotificationDispatcher()
        dispatcher.configure(
            discord_url="https://discord.com/api/webhooks/...",
            slack_url="https://hooks.slack.com/services/...",
        )
        await dispatcher.notify("Trade filled", "BTC/USDT long @ 65000", color=0x00ff00)
    """

    def __init__(self) -> None:
        self._discord: DiscordWebhookNotifier | None = None
        self._slack: SlackWebhookNotifier | None = None

    def configure(self, discord_url: str = "", slack_url: str = "") -> None:
        """Set or replace webhook targets.

        Passing an empty string for a channel leaves it unconfigured (disabled).
        """
        self._discord = DiscordWebhookNotifier(discord_url) if discord_url else None
        self._slack = SlackWebhookNotifier(slack_url) if slack_url else None

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
