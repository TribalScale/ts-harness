"""POST /intake/uploads streams one file into the intake store; JSON bodies have a ceiling."""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

from ts_harness.gateway import asgi_app
from ts_harness.gateway import server as gateway_server
from ts_harness.gateway.auth import AuthError
from ts_harness.gateway.intake_uploads import UploadRejected, upload_request
from ts_harness.intake.blobs import load_bytes, validate_stored_pointer
from ts_harness.storage.objects import LocalObjectStore, sha256_hex


class _StoreCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = LocalObjectStore(Path(tmp.name))
        for patcher in (
            mock.patch("ts_harness.intake.blobs.intake_store", return_value=self.store),
            mock.patch("ts_harness.gateway.rate_limit.check_admin_rate_limit"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.runtime = mock.Mock()


class UploadRequestTests(unittest.TestCase):
    def test_header_checks(self):
        cases = [((None, "text/plain", None), 411), (("a", None, "abc"), 400), (("a", None, "0"), 400),
                 (("a", None, str(25 * 1024 * 1024)), 413)]
        for args, status in cases:
            with self.assertRaises(UploadRejected) as ctx:
                upload_request(*args)
            self.assertEqual(ctx.exception.status, status, args)

    def test_name_and_mime_are_cleaned(self):
        req = upload_request("../a\nb.txt", "Text/Plain; charset=utf-8", "4")
        self.assertEqual((req.mime, req.size), ("text/plain", 4))
        self.assertNotIn("/", req.name.replace("_", ""))


class AsgiUploadTests(_StoreCase):
    def _client(self, auth=None, auth_error=None):
        auth = auth or mock.Mock(tenant_id="acme")
        patches = [
            mock.patch.object(asgi_app, "get_runtime", return_value=self.runtime),
            mock.patch.object(asgi_app, "_check_origin"),
            mock.patch.object(asgi_app, "_auth", return_value=auth, side_effect=auth_error),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return TestClient(asgi_app.app)

    def test_upload_returns_a_pointer_the_session_message_accepts(self):
        data = b"requirements\n" * 100_000  # ~1.3 MB
        response = self._client().post(
            "/intake/uploads?name=brief.txt", content=data, headers={"Content-Type": "text/plain"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        pointer = response.json()["pointer"]
        self.assertEqual((pointer["kind"], pointer["size"], pointer["sha256"]), ("stored", len(data), sha256_hex(data)))
        self.assertTrue(pointer["object_key"].startswith("intake/acme/"))
        self.assertEqual(load_bytes(pointer, store=self.store), data)
        self.assertNotIn("error", validate_stored_pointer(pointer, tenant_id="acme", store=self.store))
        self.runtime.handle_session_message.assert_not_called()

    def test_oversized_declared_length_is_refused_before_reading(self):
        with mock.patch("ts_harness.gateway.intake_uploads.MAX_FILE_BYTES", 10):
            response = self._client().post("/intake/uploads?name=a.bin", content=b"x" * 20)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.store.list("intake/"), [])

    def test_unauthenticated_is_refused(self):
        response = self._client(auth_error=AuthError("missing token")).post("/intake/uploads?name=a", content=b"x")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.store.list("intake/"), [])

    def test_storage_failure_is_a_503(self):
        broken = mock.Mock()
        broken.head.side_effect = RuntimeError("bucket missing")
        with mock.patch("ts_harness.intake.blobs.intake_store", return_value=broken):
            response = self._client().post("/intake/uploads?name=a.txt", content=b"abc")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("bucket", response.json()["error"])

    def test_json_ceiling_applies_to_other_routes(self):
        client = self._client()
        with mock.patch.dict(os.environ, {"TS_HARNESS_MAX_JSON_BODY_BYTES": "100"}):
            response = client.post("/session/message", content=json.dumps({"text": "x" * 500}),
                                   headers={"Content-Type": "application/json"})
            self.assertEqual(response.status_code, 413)
            self.runtime.handle_session_message.assert_not_called()
            upload = client.post("/intake/uploads?name=a.txt", content=b"y" * 500)
            self.assertEqual(upload.status_code, 200)


class FallbackServerUploadTests(_StoreCase):
    def setUp(self):
        super().setUp()
        for patcher in (
            mock.patch.object(gateway_server, "get_runtime", return_value=self.runtime),
            mock.patch.object(gateway_server.GatewayHandler, "_check_origin"),
            mock.patch.object(gateway_server.GatewayHandler, "_authenticate", return_value=mock.Mock(tenant_id="acme")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway_server.GatewayHandler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def _post(self, path: str, body: bytes, headers: dict[str, str] | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        conn.request("POST", path, body=body, headers=headers or {})
        response = conn.getresponse()
        payload = json.loads(response.read() or b"{}")
        conn.close()
        return response.status, payload

    def test_upload_streams_rfile_into_the_store(self):
        status, payload = self._post("/intake/uploads?name=notes.md", b"# notes", {"Content-Type": "text/markdown"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(load_bytes(payload["pointer"], store=self.store), b"# notes")

    def test_json_ceiling(self):
        with mock.patch.dict(os.environ, {"TS_HARNESS_MAX_JSON_BODY_BYTES": "50"}):
            status, payload = self._post("/session/message", json.dumps({"text": "x" * 200}).encode(),
                                         {"Content-Type": "application/json"})
        self.assertEqual(status, 413, payload)
        self.runtime.handle_session_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
