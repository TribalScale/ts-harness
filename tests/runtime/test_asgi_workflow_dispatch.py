"""POST /workflow/dispatch reports runtime failures as a blocked dispatch."""

from __future__ import annotations

import asyncio
import threading
import unittest
from unittest import mock

import httpx
from starlette.testclient import TestClient

from ts_harness.gateway import asgi_app


class WorkflowDispatchErrorTests(unittest.TestCase):
    def test_runtime_error_returns_blocked_result(self):
        runtime = mock.Mock()
        runtime.dispatch_workflow.side_effect = RuntimeError("boom")
        with (
            mock.patch.object(asgi_app, "get_runtime", return_value=runtime),
            mock.patch.object(asgi_app, "_check_origin"),
            mock.patch.object(asgi_app, "_auth", return_value=mock.Mock()),
        ):
            response = TestClient(asgi_app.app).post("/workflow/dispatch", json={"agent_id": "dev-agent"})

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["status"], "blocked")
        self.assertEqual(response.json()["error"], "boom")


class LongRunningRequestTests(unittest.TestCase):
    def _runtime(self, release: threading.Event) -> mock.Mock:
        def slow(*_args):
            release.wait(timeout=3)
            return mock.Mock(to_dict=lambda: {"status": "ok"})

        runtime = mock.Mock()
        runtime.dispatch_workflow.side_effect = slow
        runtime.handle_session_message.side_effect = lambda *_args: (release.wait(timeout=3), {"ok": True})[1]
        runtime._build_health_report.return_value = mock.Mock(to_dict=lambda: {"status": "ok"})
        runtime.metrics.snapshot.return_value = {}
        return runtime

    def _assert_health_answers_during(self, path: str, body: dict) -> None:
        release = threading.Event()

        async def scenario() -> None:
            transport = httpx.ASGITransport(app=asgi_app.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                slow = asyncio.create_task(client.post(path, json=body))
                await asyncio.sleep(0.1)
                health = await asyncio.wait_for(client.get("/healthz"), timeout=2)
                self.assertEqual(health.status_code, 200)
                self.assertFalse(slow.done(), "health answered only after the slow request finished")
                release.set()
                self.assertEqual((await slow).status_code, 200)

        with (
            mock.patch.object(asgi_app, "get_runtime", return_value=self._runtime(release)),
            mock.patch.object(asgi_app, "_check_origin"),
            mock.patch.object(asgi_app, "_auth", return_value=mock.Mock()),
        ):
            asyncio.run(scenario())

    def test_dispatch_does_not_block_other_requests(self):
        self._assert_health_answers_during("/workflow/dispatch", {"agent_id": "dev-agent"})

    def test_session_message_does_not_block_other_requests(self):
        self._assert_health_answers_during("/session/message", {"message": "hi"})
