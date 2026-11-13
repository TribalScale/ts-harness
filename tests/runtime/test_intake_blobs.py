"""Intake bytes in the object store: streaming stash, tenant-checked pointers, local retention."""

from __future__ import annotations

import io
import os
import tempfile
import time
import unittest
from pathlib import Path

from tests.object_store_support import FakeS3

from ts_harness.intake.blobs import (
    UploadIncomplete,
    UploadTooLarge,
    discord_key,
    load_bytes,
    stash_stream,
    sweep_local_intake,
    upload_key,
    validate_stored_pointer,
)
from ts_harness.storage.objects import LocalObjectStore, ObjectNotFound, S3ObjectStore, sha256_hex


class StashTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = LocalObjectStore(Path(self.tmp.name))

    def _stash(self, data: bytes, **kwargs):
        return stash_stream(
            tenant_id=kwargs.pop("tenant_id", "acme"),
            name=kwargs.pop("name", "brief.txt"),
            mime=kwargs.pop("mime", "text/plain"),
            fileobj=io.BytesIO(data),
            size=kwargs.pop("size", len(data)),
            store=self.store,
            **kwargs,
        )

    def test_round_trip_returns_a_small_pointer(self):
        data = b"hello intake" * 200_000  # ~2.4 MB, spills past the in-memory spool
        pointer = self._stash(data)
        self.assertEqual(set(pointer), {"kind", "object_key", "sha256", "size", "name", "mime"})
        self.assertEqual(pointer["object_key"], upload_key(tenant_id="acme", sha256=sha256_hex(data)))
        self.assertEqual(load_bytes(pointer, store=self.store), data)

    def test_same_bytes_are_stored_once(self):
        first = self._stash(b"same")
        mtime = (Path(self.tmp.name) / first["object_key"]).stat().st_mtime_ns
        second = self._stash(b"same", name="copy.txt")
        self.assertEqual(first["object_key"], second["object_key"])
        self.assertEqual((Path(self.tmp.name) / first["object_key"]).stat().st_mtime_ns, mtime)

    def test_too_large_is_refused_before_reading(self):
        source = io.BytesIO(b"x" * 10)
        with self.assertRaises(UploadTooLarge):
            stash_stream(tenant_id="t", name="a", mime=None, fileobj=source, size=11, store=self.store, max_bytes=10)
        self.assertEqual(source.tell(), 0)

    def test_short_body_is_refused(self):
        with self.assertRaises(UploadIncomplete):
            self._stash(b"abc", size=10)
        self.assertEqual(self.store.list("intake/"), [])

    def test_explicit_key_for_discord(self):
        key = discord_key(tenant_id="acme", attachment_id="123")
        pointer = self._stash(b"img", key=key, name="a.png", mime="image/png")
        self.assertEqual(pointer["object_key"], "intake/acme/discord/123")

    def test_tenant_and_name_are_sanitized(self):
        pointer = self._stash(b"x", tenant_id="../evil tenant", name="../../etc/passwd\n")
        self.assertTrue(pointer["object_key"].startswith("intake/evil_tenant/"))
        self.assertNotIn("/", pointer["name"].replace("_", ""))

    def test_s3_uses_the_streaming_upload(self):
        fake = FakeS3()
        pointer = stash_stream(
            tenant_id="t", name="a.txt", mime="text/plain", fileobj=io.BytesIO(b"abc"), size=3,
            store=S3ObjectStore("b", client=fake),
        )
        self.assertIn("upload_fileobj", fake.calls)
        self.assertEqual(fake.objects[("b", pointer["object_key"])][0], b"abc")


class ValidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = LocalObjectStore(Path(self.tmp.name))
        self.pointer = stash_stream(
            tenant_id="acme", name="a.txt", mime="text/plain", fileobj=io.BytesIO(b"data"), size=4, store=self.store
        )

    def test_valid_pointer_passes(self):
        out = validate_stored_pointer(self.pointer, tenant_id="acme", store=self.store)
        self.assertNotIn("error", out)
        self.assertEqual(out["object_key"], self.pointer["object_key"])

    def test_other_tenant_is_refused(self):
        out = validate_stored_pointer(self.pointer, tenant_id="other", store=self.store)
        self.assertEqual(out["error"], "attachment not found")
        self.assertNotIn("object_key", out)

    def test_understated_size_is_refused(self):
        out = validate_stored_pointer({**self.pointer, "size": 1}, tenant_id="acme", store=self.store)
        self.assertEqual(out["error"], "attachment not found")

    def test_wrong_sha_and_missing_object_are_refused(self):
        self.assertIn("error", validate_stored_pointer({**self.pointer, "sha256": "0" * 64}, tenant_id="acme", store=self.store))
        missing = {**self.pointer, "object_key": "intake/acme/zz/" + "f" * 64}
        self.assertIn("error", validate_stored_pointer(missing, tenant_id="acme", store=self.store))

    def test_bad_keys_are_refused(self):
        for key in ("intake/acme/../other/x", "/intake/acme/x", "", None):
            out = validate_stored_pointer({**self.pointer, "object_key": key}, tenant_id="acme", store=self.store)
            self.assertEqual(out["error"], "attachment not found", key)

    def test_extra_fields_are_dropped(self):
        out = validate_stored_pointer({**self.pointer, "content_base64": "x", "evil": 1}, tenant_id="acme", store=self.store)
        self.assertEqual(set(out), {"kind", "object_key", "sha256", "size", "name", "mime"})


class SweepTests(unittest.TestCase):
    def test_removes_only_old_local_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalObjectStore(Path(tmp))
            old = stash_stream(tenant_id="t", name="o", mime=None, fileobj=io.BytesIO(b"old"), size=3, store=store)
            new = stash_stream(tenant_id="t", name="n", mime=None, fileobj=io.BytesIO(b"new"), size=3, store=store)
            past = time.time() - 15 * 86400
            os.utime(Path(tmp) / old["object_key"], (past, past))
            self.assertEqual(sweep_local_intake(store), 1)
            with self.assertRaises(ObjectNotFound):
                load_bytes(old, store=store)
            self.assertEqual(load_bytes(new, store=store), b"new")

    def test_cloud_stores_are_left_to_lifecycle_rules(self):
        fake = FakeS3()
        store = S3ObjectStore("b", client=fake)
        store.put("intake/t/x", b"1")
        self.assertEqual(sweep_local_intake(store, older_than_days=0), 0)
        self.assertNotIn("delete_object", fake.calls)


if __name__ == "__main__":
    unittest.main()
