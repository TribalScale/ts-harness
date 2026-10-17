import unittest
import tempfile
from pathlib import Path

from ts_harness.agent.sessions import SessionStore
from ts_harness.gateway.channels.discord import DiscordAdapter
from ts_harness.gateway.channels.router import ChannelRouter
from ts_harness.gateway.channels.slack import SlackAdapter
from ts_harness.gateway.channels.telegram import TelegramAdapter


class FakeTransport:
    def __init__(self):
        self.calls = []

    def post_json(self, url, body, headers=None, timeout=30):
        self.calls.append((url, body, headers or {}, timeout))
        return {"ok": True, "ts": "123.456", "id": "msg-1"}


class ChannelAdapterTests(unittest.TestCase):
    def test_slack_send_posts_chat_message_with_bearer_token(self):
        transport = FakeTransport()
        adapter = SlackAdapter(token="xoxb-token", transport=transport)

        result = adapter.send(
            {"channel": "C123", "text": "hello", "thread_id": "111.222"},
            tenant_id="tenant-a",
        )

        url, body, headers, _ = transport.calls[0]
        self.assertEqual(url, "https://slack.com/api/chat.postMessage")
        self.assertEqual(body["channel"], "C123")
        self.assertEqual(body["thread_ts"], "111.222")
        self.assertEqual(headers["Authorization"], "Bearer xoxb-token")
        self.assertEqual(result["status"], "sent")

    def test_discord_send_posts_to_webhook(self):
        transport = FakeTransport()
        adapter = DiscordAdapter(webhook_url="https://discord.example/webhook", transport=transport)

        adapter.send({"text": "hello"}, tenant_id="tenant-a")

        url, body, headers, _ = transport.calls[0]
        self.assertEqual(url, "https://discord.example/webhook")
        self.assertEqual(body["content"], "hello")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_telegram_send_posts_bot_message(self):
        transport = FakeTransport()
        adapter = TelegramAdapter(bot_token="telegram-token", transport=transport)

        adapter.send({"channel": "12345", "text": "hello"}, tenant_id="tenant-a")

        url, body, _, _ = transport.calls[0]
        self.assertEqual(url, "https://api.telegram.org/bottelegram-token/sendMessage")
        self.assertEqual(body["chat_id"], "12345")
        self.assertEqual(body["text"], "hello")

    def test_slack_inbound_rejects_bot_loop_and_duplicates(self):
        adapter = SlackAdapter(token="x", transport=FakeTransport())

        with self.assertRaises(ValueError):
            adapter.normalize_inbound({"event_id": "bot-1", "bot_id": "B1", "text": "loop"}, tenant_id="t")

        event = adapter.normalize_inbound({"event_id": "e1", "user": "U1", "text": "hello"}, tenant_id="t")
        self.assertEqual(event.sender_id, "U1")

        with self.assertRaises(ValueError):
            adapter.normalize_inbound({"event_id": "e1", "user": "U1", "text": "hello"}, tenant_id="t")

    def test_router_normalizes_and_routes_inbound_event(self):
        router = ChannelRouter(adapters={"slack": SlackAdapter(token="x", transport=FakeTransport())})

        event = router.normalize("slack", {"event_id": "e2", "user": "U1", "text": "hello"}, tenant_id="t")

        self.assertEqual(event.channel, "slack")
        self.assertEqual(event.text, "hello")

    def test_slack_events_api_payload_is_unwrapped(self):
        adapter = SlackAdapter(token="x", transport=FakeTransport())
        event = adapter.normalize_inbound(
            {
                "type": "event_callback",
                "event_id": "Ev123",
                "event": {
                    "type": "message",
                    "user": "U1",
                    "text": "hello from events api",
                    "ts": "123.456",
                },
            },
            tenant_id="t",
        )

        self.assertEqual(event.sender_id, "U1")
        self.assertEqual(event.text, "hello from events api")

    def test_slack_mention_maps_to_stable_session_routing_key(self):
        store = SessionStore()
        adapter = SlackAdapter(token="x", transport=FakeTransport())
        event = adapter.normalize_inbound(
            {
                "event_id": "mention-1",
                "user": "U1",
                "text": "<@BOT> hello",
                "channel": "C_ORCH",
                "thread_ts": "111.222",
            },
            tenant_id="default",
        )
        key_a = store.routing_key(
            tenant_id=event.tenant_id,
            source="slack",
            channel="slack",
            thread_id=event.thread_id,
            agent_id="orchestrator",
        )
        key_b = store.routing_key(
            tenant_id=event.tenant_id,
            source="slack",
            channel="slack",
            thread_id=event.thread_id,
            agent_id="orchestrator",
        )
        self.assertEqual(key_a, key_b)

    def test_slack_workspace_allowlist_blocks_unauthorized_workspace(self):
        import os

        os.environ["SLACK_ALLOWED_WORKSPACES"] = "T_ALLOWED"
        try:
            adapter = SlackAdapter(token="x", transport=FakeTransport())
            with self.assertRaises(ValueError):
                adapter.normalize_inbound(
                    {"event_id": "ws-1", "team_id": "T_BLOCKED", "user": "U1", "text": "hi"},
                    tenant_id="t",
                )
        finally:
            os.environ.pop("SLACK_ALLOWED_WORKSPACES", None)

    def test_discord_and_telegram_normalize_inbound_events(self):
        discord = DiscordAdapter(webhook_url="https://discord.example/webhook", transport=FakeTransport())
        telegram = TelegramAdapter(bot_token="telegram-token", transport=FakeTransport())
        d_event = discord.normalize_inbound({"content": "hello discord", "author": {"id": "D1"}}, tenant_id="t")
        t_event = telegram.normalize_inbound(
            {"message": {"chat": {"id": 99}, "from": {"id": 1}, "text": "hello telegram"}},
            tenant_id="t",
        )
        self.assertEqual(d_event.text, "hello discord")
        self.assertEqual(t_event.text, "hello telegram")

    def test_slack_dedup_survives_adapter_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dedup.txt"
            first = SlackAdapter(token="x", transport=FakeTransport(), dedup_path=path)
            first.normalize_inbound({"event_id": "persisted", "user": "U1", "text": "hello"}, tenant_id="t")

            second = SlackAdapter(token="x", transport=FakeTransport(), dedup_path=path)
            with self.assertRaises(ValueError):
                second.normalize_inbound({"event_id": "persisted", "user": "U1", "text": "hello"}, tenant_id="t")


if __name__ == "__main__":
    unittest.main()
