"""Generic outbound webhook notification adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any


class WebhookNotifier:
    """Posts workflow event notifications to an HTTP endpoint.

    If ``WORKFLOW_WEBHOOK_SECRET`` is set the request body is signed with
    HMAC-SHA-256 and the digest is sent in ``X-TS-Harness-Signature``.
    """

    def __init__(
        self,
        url: str | None = None,
        secret: str | None = None,
        timeout: int = 15,
    ) -> None:
        self.url = url or os.environ.get("WORKFLOW_WEBHOOK_URL", "")
        self.secret = secret or os.environ.get("WORKFLOW_WEBHOOK_SECRET", "")
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _sign(self, data: bytes) -> str:
        return "sha256=" + hmac.new(self.secret.encode(), data, hashlib.sha256).hexdigest()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def notify(self, *, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST a JSON body ``{event, ts, **payload}`` to the configured URL."""
        if not self.url:
            return {"ok": False, "error": "no_webhook_url"}

        body: dict[str, Any] = {
            "event": event,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **payload,
        }
        data = json.dumps(body).encode()
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.secret:
            signature = self._sign(data)
            headers["X-TS-Harness-Signature"] = signature

        request = urllib.request.Request(
            self.url, data=data, method="POST", headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {"ok": True}
        except urllib.error.URLError as exc:
            return {"ok": False, "error": str(exc)}

    def notify_workflow_event(
        self,
        *,
        event: str,
        workflow_id: str,
        task_id: str | None = None,
        step_name: str | None = None,
        tenant_id: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"workflow_id": workflow_id}
        if task_id:
            payload["task_id"] = task_id
        if step_name:
            payload["step_name"] = step_name
        if tenant_id:
            payload["tenant_id"] = tenant_id
        if error:
            payload["error"] = error
        return self.notify(event=event, payload=payload)
