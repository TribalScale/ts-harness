"""ObjectStore contract: the same behavior from the local, S3 and GCS backends."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.object_store_support import FakeGcs, FakeS3

from ts_harness.storage import objects
from ts_harness.storage.backend import StorageConfigError
from ts_harness.storage.objects import (
    ChecksumMismatch,
    GcsObjectStore,
    LocalObjectStore,
    ObjectNotFound,
    ObjectStore,
    S3ObjectStore,
    sha256_hex,
)


class _Contract:
    store: ObjectStore

    def corrupt(self, key: str, data: bytes) -> None:
        raise NotImplementedError

    def test_put_get_head_round_trip(self):
        info = self.store.put("skills/a/SKILL.md", b"hello")
        self.assertEqual((info.size, info.sha256), (5, sha256_hex(b"hello")))
        self.assertEqual(self.store.get("skills/a/SKILL.md"), b"hello")
        head = self.store.head("skills/a/SKILL.md")
        self.assertEqual((head.size, head.sha256), (5, sha256_hex(b"hello")))
        self.assertIsNone(self.store.head("skills/missing"))
        with self.assertRaises(ObjectNotFound):
            self.store.get("skills/missing")

    def test_binary_content_survives(self):
        data = bytes(range(256))
        self.store.put("workspaces/a/image.bin", data)
        self.assertEqual(self.store.get("workspaces/a/image.bin"), data)

    def test_copy_keeps_the_checksum(self):
        self.store.put("skills/drafts/d1/SKILL.md", b"draft")
        info = self.store.copy("skills/drafts/d1/SKILL.md", "skills/x/1.0.0-abc/SKILL.md")
        self.assertEqual(info.sha256, sha256_hex(b"draft"))
        self.assertEqual(self.store.get("skills/x/1.0.0-abc/SKILL.md", expected_sha256=sha256_hex(b"draft")), b"draft")
        with self.assertRaises(ObjectNotFound):
            self.store.copy("skills/drafts/none", "skills/x/y")

    def test_list_and_delete_prefix(self):
        for key in ("skills/drafts/d1/SKILL.md", "skills/drafts/d1/ref/a.md", "skills/drafts/d2/SKILL.md", "skills/x/1/SKILL.md"):
            self.store.put(key, key.encode())
        self.assertEqual([i.key for i in self.store.list("skills/drafts/d1/")], ["skills/drafts/d1/SKILL.md", "skills/drafts/d1/ref/a.md"])
        self.assertEqual(self.store.delete_prefix("skills/drafts/d1/"), 2)
        self.assertEqual([i.key for i in self.store.list("skills/")], ["skills/drafts/d2/SKILL.md", "skills/x/1/SKILL.md"])
        self.store.delete("skills/x/1/SKILL.md")
        self.store.delete("skills/x/1/SKILL.md")
        self.assertEqual([i.key for i in self.store.list("skills/x/")], [])

    def test_checksum_mismatch_is_refused(self):
        self.store.put("skills/a/SKILL.md", b"good")
        with self.assertRaises(ChecksumMismatch):
            self.store.get("skills/a/SKILL.md", expected_sha256=sha256_hex(b"other"))
        self.corrupt("skills/a/SKILL.md", b"tampered")
        with self.assertRaisesRegex(ChecksumMismatch, "stored"):
            self.store.get("skills/a/SKILL.md")

    def test_keys_and_prefixes_are_validated(self):
        for bad in ("", "/abs", "a/../b", "a//b", "a/./b", "space here"):
            with self.assertRaises(ValueError):
                self.store.put(bad, b"x")
        with self.assertRaises(ValueError):
            self.store.list("no-trailing-slash")
        with self.assertRaises(ValueError):
            self.store.delete_prefix("")


class LocalObjectStoreTests(_Contract, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = LocalObjectStore(Path(self.tmp.name))

    def corrupt(self, key, data):
        (Path(self.tmp.name) / key).write_bytes(data)

    def test_sidecars_are_not_listed(self):
        self.store.put("workspaces/a/x.md", b"1")
        self.assertEqual([i.key for i in self.store.list("")], ["workspaces/a/x.md"])


class S3ObjectStoreTests(_Contract, unittest.TestCase):
    def setUp(self):
        self.fake = FakeS3(page_size=2)
        self.store = S3ObjectStore("bucket", client=self.fake)

    def corrupt(self, key, data):
        _, meta = self.fake.objects[("bucket", key)]
        self.fake.objects[("bucket", key)] = (data, meta)

    def test_listing_follows_continuation_tokens_and_deletes_in_batches(self):
        for i in range(5):
            self.store.put(f"workspaces/a/{i}.md", b"x")
        self.assertEqual(len(self.store.list("workspaces/a/")), 5)
        self.assertEqual(self.store.delete_prefix("workspaces/a/"), 5)
        self.assertIn("delete_objects", self.fake.calls)
        self.assertNotIn("delete_object", self.fake.calls)

    def test_metadata_carries_the_sha(self):
        self.store.put("skills/a/SKILL.md", b"v")
        self.assertEqual(self.fake.objects[("bucket", "skills/a/SKILL.md")][1], {"sha256": sha256_hex(b"v")})


class GcsObjectStoreTests(_Contract, unittest.TestCase):
    def setUp(self):
        self.fake = FakeGcs()
        self.store = GcsObjectStore("bucket", client=self.fake)

    def corrupt(self, key, data):
        self.fake.bucket("bucket").blobs[key].data = data


class FactoryTests(unittest.TestCase):
    def setUp(self):
        objects._STORES.clear()
        self.addCleanup(objects._STORES.clear)

    def test_local_backend_uses_a_directory_per_kind(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"TS_HARNESS_OBJECT_BACKEND": "local", "TS_HARNESS_LOCAL_OBJECT_DIR": tmp}
        ):
            store = objects.object_store("skills")
            self.assertIsInstance(store, LocalObjectStore)
            self.assertEqual(store.root, Path(tmp) / "skills")
            self.assertIs(objects.object_store("skills"), store)

    def test_s3_backend_needs_the_bucket(self):
        with mock.patch.dict(os.environ, {"TS_HARNESS_OBJECT_BACKEND": "s3", "TS_HARNESS_SKILLS_BUCKET": "b", "TS_HARNESS_WORKSPACE_BUCKET": ""}):
            self.assertEqual(objects.object_store("skills").bucket, "b")
            with self.assertRaisesRegex(StorageConfigError, "TS_HARNESS_WORKSPACE_BUCKET"):
                objects.object_store("workspaces")
        with self.assertRaises(ValueError):
            objects.object_store("logs")

    def test_gcs_backend(self):
        with mock.patch.dict(os.environ, {"TS_HARNESS_OBJECT_BACKEND": "gcs", "TS_HARNESS_WORKSPACE_BUCKET": "w"}):
            self.assertIsInstance(objects.object_store("workspaces"), GcsObjectStore)


class PackagingTests(unittest.TestCase):
    def test_gcp_extra_is_installed_in_the_runtime_image(self):
        root = Path(__file__).resolve().parents[2]
        self.assertIn('"google-cloud-storage', (root / "pyproject.toml").read_text())
        self.assertIn(".[google,aws,gcp]", (root / "docker" / "Dockerfile.agent-runtime").read_text())


if __name__ == "__main__":
    unittest.main()
