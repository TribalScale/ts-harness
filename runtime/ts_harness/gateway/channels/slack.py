from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

from ts_harness.gateway.channels.base import BaseChannelAdapter, JsonHttpTransport, MessageEvent

logger = logging.getLogger(__name__)


class SlackAdapter(BaseChannelAdapter):
    name = "slack"

    def __init__(
        self,
        token: str | None = None,
        transport: JsonHttpTransport | None = None,
        dedup_path: str | Path | None = None,
        *,
        dedup_client: Any = None,
    ):
        from ts_harness.storage.backend import shared_state_enabled

        self.token = token if token is not None else os.environ.get("SLACK_BOT_TOKEN", "")
        self.transport = transport or JsonHttpTransport()
        # In Postgres mode every replica checks the same table, so an event is handled once.
        self.shared_dedup = dedup_client is not None or shared_state_enabled()
        self._dedup_client = dedup_client
        self.dedup_path = None
        if not self.shared_dedup and (dedup_path or os.environ.get("TS_HARNESS_CHANNEL_DEDUP")):
            self.dedup_path = Path(dedup_path or os.environ.get("TS_HARNESS_CHANNEL_DEDUP", ""))
        self._seen: set[str] = self._load_seen()
        self._dedup_lock = threading.Lock()

    @staticmethod
    def unwrap_events_api_payload(payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("type") == "url_verification":
            return payload
        if payload.get("type") == "event_callback":
            event = dict(payload.get("event") or {})
            event.setdefault("event_id", payload.get("event_id"))
            event.setdefault("team_id", payload.get("team_id"))
            event.setdefault("dedup_key", payload.get("dedup_key"))
            return event
        return payload

    def _channel_allowed(self, channel_id: str) -> bool:
        allowed = os.environ.get("SLACK_ALLOWED_CHANNELS", "")
        if not allowed.strip():
            return True
        allowed_set = {item.strip() for item in allowed.split(",") if item.strip()}
        return channel_id in allowed_set

    def _workspace_allowed(self, workspace_id: str) -> bool:
        allowed = os.environ.get("SLACK_ALLOWED_WORKSPACES", "")
        if not allowed.strip():
            return True
        allowed_set = {item.strip() for item in allowed.split(",") if item.strip()}
        return workspace_id in allowed_set

    def normalize_inbound(self, payload: dict[str, Any], *, tenant_id: str) -> MessageEvent:
        payload = self.unwrap_events_api_payload(payload)
        if payload.get("type") == "url_verification":
            raise ValueError("slack url verification")
        channel_id = str(payload.get("channel") or "")
        workspace_id = str(payload.get("team_id") or payload.get("team") or "")
        if workspace_id and not self._workspace_allowed(workspace_id):
            raise ValueError("slack workspace not authorized")
        if channel_id and not self._channel_allowed(channel_id):
            raise ValueError("slack channel not authorized")
        if payload.get("bot_id") or payload.get("subtype") == "bot_message":
            raise ValueError("bot loop suppressed")
        dedup_key = str(
            payload.get("dedup_key")
            or payload.get("client_msg_id")
            or payload.get("ts")
            or payload.get("event_id")
            or ""
        )
        with self._dedup_lock:
            if dedup_key and dedup_key in self._seen:
                raise ValueError("duplicate slack event")
            if dedup_key:
                if self.shared_dedup and not self._first_sighting(dedup_key):
                    self._seen.add(dedup_key)
                    raise ValueError("duplicate slack event")
                self._seen.add(dedup_key)
                self._persist_seen(dedup_key)
        return MessageEvent(
            channel="slack",
            tenant_id=tenant_id,
            text=str(payload.get("text", "")),
            sender_id=str(payload.get("user", "unknown")),
            thread_id=payload.get("thread_ts") or payload.get("ts"),
            metadata={**payload, "channel_id": channel_id},
        )

    def _first_sighting(self, dedup_key: str) -> bool:
        """Check and mark in one call; if the memory API is down, fall back to this process's own memory."""
        client = self._dedup_client
        try:
            if client is None:
                from ts_harness.memory.client import get_memory_client

                client = self._dedup_client = get_memory_client()
            return bool(client.mark_channel_event(f"slack:{dedup_key}"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("slack dedup check failed, using local memory only: %s", exc)
            return True

    def _load_seen(self) -> set[str]:
        if not self.dedup_path or not self.dedup_path.exists():
            return set()
        return {line.strip() for line in self.dedup_path.read_text(encoding="utf-8").splitlines() if line.strip()}

    def _persist_seen(self, event_id: str) -> None:
        if not self.dedup_path:
            return
        self.dedup_path.parent.mkdir(parents=True, exist_ok=True)
        with self.dedup_path.open("a", encoding="utf-8") as handle:
            handle.write(event_id + "\n")

    def send(self, args: dict[str, Any], *, tenant_id: str) -> dict[str, Any]:
        channel = str(args.get("channel", ""))
        text = str(args.get("text", ""))
        if not channel:
            return {"error": "slack channel is required"}
        if not self.token:
            return {"error": "SLACK_BOT_TOKEN is required"}
        body: dict[str, Any] = {"channel": channel, "text": text}
        if args.get("thread_id"):
            body["thread_ts"] = args["thread_id"]
        response = self.transport.post_json(
            "https://slack.com/api/chat.postMessage",
            body,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        if response.get("ok") is False:
            return {"error": response.get("error", "slack send failed"), "channel": "slack"}
        return {
            "status": "sent",
            "channel": "slack",
            "tenant_id": tenant_id,
            "thread_id": args.get("thread_id"),
            "message_id": response.get("ts"),
            "text_preview": str(args.get("text", ""))[:200],
        }
