"""Production intake clients against fake HTTP and Drive APIs."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from ts_harness.intake import connectors
from ts_harness.intake.blobs import discord_key, stash_stream
from ts_harness.intake.resolve import DriveAccessError, SourceUnavailable
from ts_harness.storage.objects import LocalObjectStore


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class DownloadTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = LocalObjectStore(Path(tmp.name))
        patcher = mock.patch("ts_harness.intake.blobs.intake_store", return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_slack_file(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("authorization", ""))
            if request.url.path == "/api/files.info":
                return httpx.Response(200, json={"ok": True, "file": {
                    "name": "spec.md", "mimetype": "text/markdown", "size": 4,
                    "url_private_download": "https://files.slack.com/files-pri/T/spec.md"}})
            return httpx.Response(200, content=b"# hi")

        with mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-1"}):
            out = connectors.build_download(_client(handler))({"kind": "slack_file", "file_id": "F1"})
        self.assertEqual(out, ("spec.md", "text/markdown", b"# hi"))
        self.assertEqual(seen, ["Bearer xoxb-1", "Bearer xoxb-1"])

    def test_slack_missing_scope_and_token(self):
        handler = lambda request: httpx.Response(200, json={"ok": False, "error": "missing_scope"})  # noqa: E731
        with mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-1"}):
            with self.assertRaisesRegex(SourceUnavailable, "files:read"):
                connectors.build_download(_client(handler))({"kind": "slack_file", "file_id": "F1"})
        with mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": ""}):
            with self.assertRaisesRegex(SourceUnavailable, "SLACK_BOT_TOKEN"):
                connectors.build_download(_client(handler))({"kind": "slack_file", "file_id": "F1"})

    def test_telegram_errors_never_carry_the_token(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/getFile"):
                return httpx.Response(200, json={"ok": True, "result": {"file_path": "documents/a.pdf", "file_size": 3}})
            return httpx.Response(500)

        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123:SECRET"}):
            with self.assertRaises(SourceUnavailable) as ctx:
                connectors.build_download(_client(handler))({"kind": "telegram_file", "file_id": "x", "name": "a.pdf"})
        self.assertNotIn("SECRET", str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        self.assertIn("HTTP 500", str(ctx.exception))

    def test_discord_prefers_the_prefetched_copy(self):
        key = discord_key(tenant_id="acme", attachment_id="77")
        stash_stream(tenant_id="acme", name="a.txt", mime="text/plain", fileobj=io.BytesIO(b"copy"), size=4,
                     key=key, store=self.store)
        calls: list[str] = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, content=b"cdn")

        pointer = {"kind": "discord_file", "attachment_id": "77", "object_key": key, "url": "https://cdn.discordapp.com/a.txt",
                   "name": "a.txt"}
        self.assertEqual(connectors.build_download(_client(handler))(pointer)[2], b"copy")
        self.assertEqual(calls, [])

    def test_discord_falls_back_to_the_url_then_reports_expiry(self):
        pointer = {"kind": "discord_file", "attachment_id": "88", "object_key": discord_key(tenant_id="acme", attachment_id="88"),
                   "url": "https://cdn.discordapp.com/a.txt?ex=1", "name": "a.txt"}
        ok = _client(lambda request: httpx.Response(200, content=b"cdn"))
        self.assertEqual(connectors.build_download(ok)(pointer)[2], b"cdn")
        gone = _client(lambda request: httpx.Response(404))
        with self.assertRaisesRegex(SourceUnavailable, "expired"):
            connectors.build_download(gone)(pointer)

    def test_fetch_stops_past_the_cap(self):
        big = _client(lambda request: httpx.Response(200, content=b"x" * 100))
        with self.assertRaisesRegex(SourceUnavailable, "larger than 20 MB"):
            connectors.fetch_capped(big, "https://example.test/f", max_bytes=10)

    def test_stored_pointer_reads_the_store(self):
        pointer = stash_stream(tenant_id="acme", name="n.txt", mime="text/plain", fileobj=io.BytesIO(b"stored"),
                               size=6, store=self.store)
        self.assertEqual(connectors.build_download(_client(lambda r: httpx.Response(500)))(pointer)[2], b"stored")


class _HttpError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.resp = mock.Mock(status=status)


class DriveSourceTests(unittest.TestCase):
    def _api(self, execute):
        api = mock.Mock()
        for method in ("get", "export", "get_media", "list"):
            getattr(api.files.return_value, method).return_value.execute.side_effect = execute
        return api

    def test_forbidden_and_missing_map_to_not_shared(self):
        for status in (403, 404):
            source = connectors.GoogleDriveSource(self._api(_HttpError(status)))
            with self.assertRaises(DriveAccessError):
                source.metadata("f1")

    def test_other_errors_propagate(self):
        source = connectors.GoogleDriveSource(self._api(_HttpError(500)))
        with self.assertRaises(_HttpError):
            source.download("f1")

    def test_list_children_query(self):
        api = self._api(lambda: {"files": [{"id": "a"}, {"id": "b"}, {"id": "c"}]})
        children = connectors.GoogleDriveSource(api).list_children("fold1", 2)
        self.assertEqual([c["id"] for c in children], ["a", "b"])
        kwargs = api.files.return_value.list.call_args.kwargs
        self.assertEqual(kwargs["q"], "'fold1' in parents and trashed = false")
        self.assertTrue(kwargs["supportsAllDrives"])

    def test_export_returns_bytes(self):
        api = self._api(lambda: "text body")
        self.assertEqual(connectors.GoogleDriveSource(api).export("d1", "text/plain"), b"text body")

    def test_build_drive_without_config_is_none(self):
        with mock.patch.dict(os.environ, {"TS_HARNESS_CONFIG": ""}):
            self.assertIsNone(connectors.build_drive(tenant_id="t", agent_id="pm-agent"))


if __name__ == "__main__":
    unittest.main()
