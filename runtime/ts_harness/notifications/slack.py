"""Slack notification adapter for workflow events."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


class SlackNotifier:
    """Sends structured workflow event notifications to a Slack channel.

    Requires ``SLACK_BOT_TOKEN``.  Falls back gracefully (returns an error
    dict) when the token or target channel is absent so callers never raise.
    """

    def __init__(
        self,
        token: str | None = None,
        default_channel: str | None = None,
    ) -> None:
        self.token = token or os.environ.get("SLACK_BOT_TOKEN", "")
        self.default_channel = default_channel or os.environ.get(
            "SLACK_NOTIFICATION_CHANNEL", ""
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.token:
            return {"ok": False, "error": "no_token"}
        data = json.dumps(payload).encode()
        request = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.URLError as exc:
            return {"ok": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def post_thread_text(
        self,
        *,
        text: str,
        channel: str | None = None,
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        """Post plain text to a Slack channel or thread (no event card)."""
        target = channel or self.default_channel
        if not target:
            return {"ok": False, "error": "no_channel"}
        return self._post(
            {
                "channel": target,
                "text": text,
                **({"thread_ts": thread_ts} if thread_ts else {}),
            }
        )

    def notify_workflow_event(
        self,
        *,
        event: str,
        workflow_id: str,
        task_id: str | None = None,
        agent_id: str | None = None,
        step_name: str | None = None,
        tenant_id: str | None = None,
        error: str | None = None,
        channel: str | None = None,
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        """Post a workflow event notification block to Slack."""
        target = channel or self.default_channel
        if not target:
            return {"ok": False, "error": "no_channel"}

        fields: list[dict[str, str]] = [
            {"type": "mrkdwn", "text": f"*Event*\n`{event}`"},
            {"type": "mrkdwn", "text": f"*Workflow*\n`{workflow_id}`"},
        ]
        if task_id:
            fields.append({"type": "mrkdwn", "text": f"*Task*\n`{task_id}`"})
        if step_name:
            fields.append({"type": "mrkdwn", "text": f"*Step*\n`{step_name}`"})
        if agent_id:
            fields.append({"type": "mrkdwn", "text": f"*Agent*\n`{agent_id}`"})
        if tenant_id:
            fields.append({"type": "mrkdwn", "text": f"*Tenant*\n`{tenant_id}`"})

        color = "#36a64f"
        if event in {"failed", "blocked", "cancelled", "compensated"}:
            color = "#e01e5a"
        elif event in {"plan_proposed", "plan_approved", "awaiting_approval"}:
            color = "#ECB22E"

        text = f"*{event}* — workflow `{workflow_id}`"
        if error:
            text += f"\n> {error}"

        return self._post(
            {
                "channel": target,
                "text": text,
                "attachments": [{"color": color, "fields": fields}],
                **({"thread_ts": thread_ts} if thread_ts else {}),
            }
        )
