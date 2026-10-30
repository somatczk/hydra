"""ops/notifications: the Discord, Slack and Telegram senders against a local HTTP server.

No external call and no httpx mock: every send runs the real async client
against an asyncio server on 127.0.0.1:0, so the payload shapes, the success
and failure statuses, the fire-and-forget contract, the token-hazard logging
deviation and the dispatcher's channel independence are asserted at the real
seam.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import Any

import pytest

from hydra.ops.notifications import (
    COLOR_GREEN,
    COLOR_RED,
    DiscordWebhookNotifier,
    NotificationDispatcher,
    SlackWebhookNotifier,
    TelegramNotifier,
)

BOT_ID = "123456789"
BOT_BODY = "AAEhBOweik6ad9r_QXMENQjcrGbqCr4K"
TOKEN = f"{BOT_ID}:{BOT_BODY}"
CHAT = "-1002146360582"

_REASONS = {
    200: "OK",
    204: "No Content",
    400: "Bad Request",
    500: "Internal Server Error",
}


class _Hit:
    """One request the local server received."""

    def __init__(self, method: str, path: str, body: dict[str, Any]) -> None:
        self.method = method
        self.path = path
        self.body = body


class _LocalWebhookServer:
    """A one-request-per-connection HTTP server on 127.0.0.1:0 with scripted statuses.

    The ``statuses`` list scripts the answer for the first requests in order
    (Discord answers 204, Slack 200); a request beyond the script gets a 200.
    ``hold`` (shared with the risk-relay tests' off-loop proof) parks every
    request until the event is set, so a real client can be made to hang.
    """

    def __init__(self, statuses: list[int], hold: asyncio.Event | None = None) -> None:
        self._statuses = statuses
        self._hold = hold
        self.hits: list[_Hit] = []
        self._server: asyncio.AbstractServer | None = None
        self.url = ""

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/hook"

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            method, path, _version = (await reader.readline()).decode().split()
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                name, _, value = line.decode().partition(":")
                headers[name.strip().lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            raw = await reader.readexactly(length) if length else b""
            self.hits.append(_Hit(method, path, json.loads(raw)))
            if self._hold is not None:
                await self._hold.wait()
            status = (
                self._statuses[len(self.hits) - 1] if len(self.hits) <= len(self._statuses) else 200
            )
            head = f"HTTP/1.1 {status} {_REASONS.get(status, 'OK')}\r\n"
            if status != 204:
                head += "Content-Length: 0\r\n"
            head += "Connection: close\r\n\r\n"
            writer.write(head.encode())
            await writer.drain()
        finally:
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def close(self) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()


async def _start(statuses: list[int], hold: asyncio.Event | None = None) -> _LocalWebhookServer:
    server = _LocalWebhookServer(statuses, hold)
    await server.start()
    return server


async def _refused_url() -> str:
    """A 127.0.0.1 URL with nothing listening: bind, read the port, release it."""
    server = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    return f"http://127.0.0.1:{port}/hook"


class TestDiscord:
    async def test_posts_an_embed_and_returns_true_on_204(self) -> None:
        server = await _start([204])
        try:
            notifier = DiscordWebhookNotifier(server.url)
            assert await notifier.send("Trade filled", "BTC/USDT long @ 65000") is True
        finally:
            await server.close()

        assert len(server.hits) == 1
        hit = server.hits[0]
        assert hit.method == "POST"
        assert hit.path == "/hook"
        assert COLOR_GREEN == 0x00FF00 == 65280
        assert hit.body == {
            "embeds": [
                {
                    "title": "Trade filled",
                    "description": "BTC/USDT long @ 65000",
                    "color": COLOR_GREEN,
                }
            ]
        }

    async def test_the_color_override_reaches_the_payload(self) -> None:
        server = await _start([204])
        try:
            notifier = DiscordWebhookNotifier(server.url)
            assert await notifier.send("Stopped", "kill switch", color=COLOR_RED) is True
        finally:
            await server.close()

        assert COLOR_RED == 0xFF0000
        assert server.hits[0].body["embeds"][0]["color"] == COLOR_RED

    async def test_returns_false_on_400(self) -> None:
        server = await _start([400])
        try:
            notifier = DiscordWebhookNotifier(server.url)
            assert await notifier.send("Trade filled", "BTC/USDT long @ 65000") is False
        finally:
            await server.close()

        assert len(server.hits) == 1

    async def test_connection_refused_returns_false(self) -> None:
        notifier = DiscordWebhookNotifier(await _refused_url())
        assert await notifier.send("Trade filled", "BTC/USDT long @ 65000") is False


class TestSlack:
    async def test_posts_blocks_and_returns_true_on_200(self) -> None:
        server = await _start([200])
        try:
            notifier = SlackWebhookNotifier(server.url)
            assert await notifier.send("Trade filled", "BTC/USDT long @ 65000") is True
        finally:
            await server.close()

        assert len(server.hits) == 1
        hit = server.hits[0]
        assert hit.method == "POST"
        assert hit.path == "/hook"
        assert hit.body == {
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": "Trade filled"},
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "BTC/USDT long @ 65000"},
                },
            ]
        }

    async def test_returns_false_on_500(self) -> None:
        server = await _start([500])
        try:
            notifier = SlackWebhookNotifier(server.url)
            assert await notifier.send("Trade filled", "BTC/USDT long @ 65000") is False
        finally:
            await server.close()

        assert len(server.hits) == 1

    async def test_connection_refused_returns_false(self) -> None:
        notifier = SlackWebhookNotifier(await _refused_url())
        assert await notifier.send("Trade filled", "BTC/USDT long @ 65000") is False


class TestTelegram:
    async def test_posts_the_sendmessage_shape_and_returns_true_on_200(self) -> None:
        server = await _start([200])
        try:
            notifier = TelegramNotifier(TOKEN, CHAT, base_url=server.url)
            assert await notifier.send("Trade filled", "BTC/USDT long @ 65000") is True
        finally:
            await server.close()

        assert len(server.hits) == 1
        hit = server.hits[0]
        assert hit.method == "POST"
        assert hit.path == f"/hook/bot{TOKEN}/sendMessage"
        assert hit.body == {
            "chat_id": CHAT,
            "text": "Trade filled\n\nBTC/USDT long @ 65000",
        }

    async def test_the_token_rides_the_bot_url_not_the_payload(self) -> None:
        server = await _start([200])
        try:
            notifier = TelegramNotifier(TOKEN, "@hydra_alerts", base_url=server.url)
            assert await notifier.send("Title", "Body") is True
        finally:
            await server.close()

        assert server.hits[0].path == f"/hook/bot{TOKEN}/sendMessage"
        assert server.hits[0].body == {"chat_id": "@hydra_alerts", "text": "Title\n\nBody"}

    async def test_returns_false_on_4xx(self) -> None:
        server = await _start([401])
        try:
            notifier = TelegramNotifier(TOKEN, CHAT, base_url=server.url)
            assert await notifier.send("Trade filled", "BTC/USDT long @ 65000") is False
        finally:
            await server.close()

        assert len(server.hits) == 1

    async def test_a_network_error_returns_false_and_no_log_record_holds_the_token(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        notifier = TelegramNotifier(TOKEN, CHAT, base_url=await _refused_url())
        with caplog.at_level(logging.WARNING, logger="hydra.ops.notifications"):
            assert await notifier.send("Trade filled", "BTC/USDT long @ 65000") is False

        assert caplog.records, "the failure must be logged"
        for record in caplog.records:
            assert TOKEN not in record.getMessage()
            assert record.exc_info is None  # a traceback would embed the token-bearing URL


class TestDispatcher:
    async def test_both_channels_are_hit_even_when_one_fails(self) -> None:
        server = await _start([500, 200])
        try:
            dispatcher = NotificationDispatcher()
            dispatcher.configure(
                discord_url=f"{server.url}/discord", slack_url=f"{server.url}/slack"
            )
            await dispatcher.notify("Trade filled", "BTC/USDT long @ 65000")
        finally:
            await server.close()

        assert [hit.path for hit in server.hits] == ["/hook/discord", "/hook/slack"]
        assert server.hits[0].body["embeds"][0]["title"] == "Trade filled"
        assert server.hits[1].body["blocks"][0]["text"]["text"] == "Trade filled"

    async def test_one_configured_channel_sends_one_request(self) -> None:
        server = await _start([204])
        try:
            dispatcher = NotificationDispatcher()
            dispatcher.configure(discord_url=server.url)
            await dispatcher.notify("Trade filled", "BTC/USDT long @ 65000")
        finally:
            await server.close()

        assert [hit.path for hit in server.hits] == ["/hook"]

    async def test_no_configured_channel_sends_nothing(self) -> None:
        server = await _start([])
        try:
            dispatcher = NotificationDispatcher()
            dispatcher.configure()
            await dispatcher.notify("Trade filled", "BTC/USDT long @ 65000")
        finally:
            await server.close()

        assert server.hits == []

    async def test_telegram_routes_when_both_rows_are_set(self) -> None:
        server = await _start([200])
        try:
            dispatcher = NotificationDispatcher()
            dispatcher.configure(
                discord_url=f"{server.url}/discord",
                telegram_token=TOKEN,
                telegram_chat_id=CHAT,
                telegram_base_url=server.url,
            )
            await dispatcher.notify("Trade filled", "BTC/USDT long @ 65000")
        finally:
            await server.close()

        assert [hit.path for hit in server.hits] == [
            "/hook/discord",
            f"/hook/bot{TOKEN}/sendMessage",
        ]
        assert server.hits[1].body["chat_id"] == CHAT

    async def test_a_half_set_telegram_pair_is_skipped(self) -> None:
        server = await _start([204])
        try:
            dispatcher = NotificationDispatcher()
            dispatcher.configure(discord_url=server.url, telegram_token=TOKEN)
            await dispatcher.notify("Trade filled", "BTC/USDT long @ 65000")
        finally:
            await server.close()

        assert [hit.path for hit in server.hits] == ["/hook"]  # Discord only
