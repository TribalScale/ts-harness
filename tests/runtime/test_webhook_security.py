"""GitHub webhook deliveries are signed and handled at most once."""

from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from unittest import mock

from ts_harness.gateway import asgi_app, github_webhook
from starlette.testclient import TestClient

SECRET = "hook-secret"


def _signed(body: dict) -> tuple[bytes, str]:
    raw = json.dumps(body).encode()
    return raw, "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


class WebhookReplayTests(unittest.TestCase):
    def setUp(self):
        github_webhook._handled_deliveries.clear()
        self.runtime = mock.Mock()
        self.runtime.config.gateway.default_tenant_id = "default"
        self.handle = mock.Mock(return_value={"status": "ignored", "kind": "github.merge"})
        self.patches = [
            mock.patch.object(asgi_app, "get_runtime", return_value=self.runtime),
            mock.patch.dict("os.environ", {"GITHUB_WEBHOOK_SECRET": SECRET}),
            mock.patch.object(github_webhook, "handle_github_webhook", self.handle),
        ]
        for patch in self.patches:
            patch.start()
        self.client = TestClient(asgi_app.app)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        github_webhook._handled_deliveries.clear()

    def _post(self, delivery: str | None, body: dict | None = None, signature: str | None = None):
        raw, good = _signed(body or {"action": "closed"})
        headers = {"X-Hub-Signature-256": signature or good, "X-GitHub-Event": "pull_request"}
        if delivery is not None:
            headers["X-GitHub-Delivery"] = delivery
        return self.client.post("/webhooks/github", content=raw, headers=headers)

    def test_replayed_delivery_is_not_handled_twice(self):
        self.assertEqual(self._post("d-1").json()["status"], "ignored")
        replay = self._post("d-1")
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["status"], "duplicate")
        self.assertEqual(self.handle.call_count, 1)
        self._post("d-2")
        self.assertEqual(self.handle.call_count, 2)

    def test_failed_delivery_can_be_redelivered(self):
        self.handle.side_effect = [RuntimeError("memory-api down"), {"status": "resumed"}]
        self.assertEqual(self._post("d-1").status_code, 500)
        self.assertEqual(self._post("d-1").json()["status"], "resumed")

    def test_missing_delivery_id_and_bad_signature_are_rejected(self):
        self.assertEqual(self._post(None).status_code, 400)
        self.assertEqual(self._post("d-3", signature="sha256=" + "0" * 64).status_code, 401)
        self.handle.assert_not_called()

    def test_delivery_ids_expire_after_ttl(self):
        github_webhook.remember_delivery("old", now=0)
        self.assertFalse(github_webhook.delivery_already_handled("old", now=github_webhook.DELIVERY_TTL_SECONDS + 1))


if __name__ == "__main__":
    unittest.main()
