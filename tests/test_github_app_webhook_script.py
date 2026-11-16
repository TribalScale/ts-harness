"""scripts/github-app-webhook.py: set the App's webhook URL and secret, report what needs a hand change."""

from __future__ import annotations

import importlib.util
import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "github-app-webhook.py"
spec = importlib.util.spec_from_file_location("github_app_webhook", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

ALL_EVENTS = list(module.REQUIRED_EVENTS)


class FakeApp:
    def __init__(self, *, active=True, events=ALL_EVENTS):
        self.active = active
        self.events = events
        self.calls: list[tuple[str, str, dict | None]] = []
        self.url = "https://old.example/webhooks/github" if active else None

    def jwt(self):
        return "jwt"

    def _call(self, method, path, *, bearer, body=None):
        self.calls.append((method, path, body))
        if path == "/app/hook/config":
            if not self.active:
                return 404, {"message": "Not Found"}
            if method == "PATCH":
                self.url = body["url"]
            return 200, {"url": self.url, "content_type": "json"}
        if path == "/app":
            return 200, {"events": self.events}
        if path.startswith("/app/hook/deliveries"):
            return 200, [{"delivered_at": "t", "event": "ping", "status_code": 200}]
        return 404, {}


def run(app, argv, secret="s3cret"):
    out, err = io.StringIO(), io.StringIO()
    env = {"GITHUB_APP_WEBHOOK_SECRET": secret}
    with patch.dict(os.environ, env), patch("ts_harness.integrations.github_app.app_for", return_value=app), redirect_stdout(out), redirect_stderr(err):
        code = module.main(argv)
    return code, out.getvalue() + err.getvalue()


class WebhookScriptTests(unittest.TestCase):
    def test_sets_url_and_runtime_secret(self):
        app = FakeApp()
        code, output = run(app, ["--url", "https://h.ts.net:8443/webhooks/github"])
        self.assertEqual(code, 0, output)
        patch_call = next(c for c in app.calls if c[0] == "PATCH")
        self.assertEqual(patch_call[2]["url"], "https://h.ts.net:8443/webhooks/github")
        self.assertEqual(patch_call[2]["secret"], "s3cret")
        self.assertNotIn("s3cret", output)

    def test_inactive_webhook_needs_the_app_settings(self):
        code, output = run(FakeApp(active=False), ["--url", "https://h.ts.net:8443/webhooks/github"])
        self.assertEqual(code, 2)
        self.assertIn("Webhook > Active", output)

    def test_missing_events_are_reported(self):
        code, output = run(FakeApp(events=["status", "check_run"]), [])
        self.assertEqual(code, 2)
        self.assertIn("pull_request,", output)

    def test_empty_secret_refuses_to_set(self):
        app = FakeApp()
        code, _ = run(app, ["--url", "https://h/webhooks/github"], secret="")
        self.assertEqual(code, 1)
        self.assertFalse(any(c[0] == "PATCH" for c in app.calls))


if __name__ == "__main__":
    unittest.main()
