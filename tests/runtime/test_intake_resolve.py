"""resolve_intake_sources: caps, budgets, folders, images, and error records with fake clients."""

from __future__ import annotations

import unittest

from ts_harness.intake.resolve import (
    DRIVE_FOLDER_MIME,
    EXPIRED,
    DriveAccessError,
    SourceUnavailable,
    resolve_intake_sources,
)
from ts_harness.intake.sources import MAX_FOLDER_CHILDREN
from ts_harness.storage.objects import ObjectNotFound

MB = 1024 * 1024


class FakeDrive:
    def __init__(self) -> None:
        self.files: dict[str, dict] = {}
        self.bodies: dict[str, bytes] = {}
        self.children: dict[str, list[str]] = {}
        self.denied: set[str] = set()
        self.downloads: list[str] = []

    def add(self, file_id: str, name: str, mime: str, body: bytes = b"", *, parent: str | None = None, size=None):
        self.files[file_id] = {"id": file_id, "name": name, "mimeType": mime, "size": str(len(body) if size is None else size)}
        self.bodies[file_id] = body
        if parent:
            self.children.setdefault(parent, []).append(file_id)

    def metadata(self, file_id):
        if file_id in self.denied or file_id not in self.files:
            raise DriveAccessError()
        return dict(self.files[file_id])

    def export(self, file_id, mime):
        return self.bodies[file_id]

    def download(self, file_id):
        self.downloads.append(file_id)
        return self.bodies[file_id]

    def list_children(self, folder_id, limit):
        return [dict(self.files[c]) for c in self.children.get(folder_id, [])][:limit]


def _download_from(table: dict[str, tuple[str, str, bytes]], calls: list | None = None):
    def download(pointer):
        key = pointer.get("file_id") or pointer.get("object_key") or pointer.get("attachment_id")
        if calls is not None:
            calls.append(key)
        value = table[key]
        if isinstance(value, Exception):
            raise value
        return value

    return download


def _stored(key: str, size: int, name: str = "f.txt") -> dict:
    return {"kind": "stored", "object_key": key, "sha256": key, "size": size, "name": name, "mime": "text/plain"}


def _drive(file_id: str) -> dict:
    return {"kind": "drive", "file_id": file_id, "url": f"https://drive.google.com/file/d/{file_id}/view"}


class ResolveTests(unittest.TestCase):
    def test_empty_list(self):
        out = resolve_intake_sources([], download=_download_from({}), drive=None, describe_image=None)
        self.assertEqual(out, {"sources": [], "sources_text": "", "skipped": []})

    def test_not_a_list_raises(self):
        with self.assertRaises(ValueError):
            resolve_intake_sources({"kind": "stored"}, download=_download_from({}), drive=None, describe_image=None)

    def test_stored_text_file(self):
        out = resolve_intake_sources(
            [_stored("k1", 5, "notes.md")],
            download=_download_from({"k1": ("notes.md", "text/markdown", b"# Hi")}),
            drive=None,
            describe_image=None,
        )
        self.assertEqual(out["sources"][0]["text"], "# Hi")
        self.assertIsNone(out["sources"][0]["error"])
        self.assertIn("### 1. notes.md", out["sources_text"])

    def test_google_doc_sheet_and_slides_are_exported(self):
        drive = FakeDrive()
        drive.add("doc1", "Brief", "application/vnd.google-apps.document", b"doc text")
        drive.add("sheet1", "Data", "application/vnd.google-apps.spreadsheet", b"a,b\n1,2")
        drive.add("deck1", "Deck", "application/vnd.google-apps.presentation", b"slide text")
        drive.add("form1", "Form", "application/vnd.google-apps.form")
        out = resolve_intake_sources(
            [_drive("doc1"), _drive("sheet1"), _drive("deck1"), _drive("form1")],
            download=_download_from({}), drive=drive, describe_image=None,
        )
        self.assertEqual([s["text"] for s in out["sources"][:3]], ["doc text", "a,b\n1,2", "slide text"])
        self.assertEqual(out["sources"][3]["error"], "unsupported Google file type")

    def test_unshared_drive_file(self):
        drive = FakeDrive()
        drive.denied.add("secret1")
        out = resolve_intake_sources([_drive("secret1")], download=_download_from({}), drive=drive, describe_image=None)
        self.assertEqual(out["sources"][0]["error"], "not shared with the workspace account")

    def test_drive_without_a_connector(self):
        out = resolve_intake_sources([_drive("x1")], download=_download_from({}), drive=None, describe_image=None)
        self.assertEqual(out["sources"][0]["error"], "Google Drive is not connected")

    def test_folder_of_fifty_children_counts_as_one_pointer(self):
        drive = FakeDrive()
        drive.add("fold1", "Specs", DRIVE_FOLDER_MIME)
        for i in range(MAX_FOLDER_CHILDREN):
            drive.add(f"c{i}", f"{i}.txt", "text/plain", b"x", parent="fold1")
        stored = [_stored(f"k{i}", 1) for i in range(24)]
        table = {f"k{i}": (f"{i}.txt", "text/plain", b"y") for i in range(24)}
        out = resolve_intake_sources([_drive("fold1"), *stored], download=_download_from(table), drive=drive,
                                     describe_image=None)
        self.assertEqual(len(out["sources"]), MAX_FOLDER_CHILDREN + 24)
        self.assertEqual(out["skipped"], [])
        self.assertTrue(out["sources"][0]["name"].startswith("Specs/"))

    def test_folder_past_fifty_and_subfolders_are_reported(self):
        drive = FakeDrive()
        drive.add("fold1", "Big", DRIVE_FOLDER_MIME)
        drive.add("sub1", "nested", DRIVE_FOLDER_MIME, parent="fold1")
        for i in range(MAX_FOLDER_CHILDREN + 5):
            drive.add(f"c{i}", f"{i}.txt", "text/plain", b"x", parent="fold1")
        out = resolve_intake_sources([_drive("fold1")], download=_download_from({}), drive=drive, describe_image=None)
        reasons = [s["reason"] for s in out["skipped"]]
        self.assertTrue(any("more than 50 files" in r for r in reasons))
        self.assertIn("subfolders are not read", reasons)
        self.assertEqual(len(out["sources"]), MAX_FOLDER_CHILDREN - 1)

    def test_total_bytes_stop_folder_children(self):
        drive = FakeDrive()
        drive.add("fold1", "Media", DRIVE_FOLDER_MIME)
        for i in range(6):
            drive.add(f"c{i}", f"{i}.txt", "text/plain", b"z", parent="fold1", size=15 * MB)
        out = resolve_intake_sources([_drive("fold1")], download=_download_from({}), drive=drive, describe_image=None)
        # Sizes are checked from metadata before download, so the sixth child (past 80 MB) is never fetched.
        self.assertEqual(len(out["sources"]), 5)
        self.assertEqual(out["skipped"], [{"name": "Media/5.txt", "reason": "byte budget reached"}])
        self.assertNotIn("c5", drive.downloads)

    def test_total_text_budget(self):
        table = {f"k{i}": (f"{i}.txt", "text/plain", b"a" * 70_000) for i in range(4)}
        out = resolve_intake_sources([_stored(f"k{i}", 70_000) for i in range(4)], download=_download_from(table),
                                     drive=None, describe_image=None, max_total_text=150_000)
        texts = [s for s in out["sources"]]
        self.assertEqual(sum(len(s["text"]) for s in texts), 150_000)
        self.assertTrue(texts[2]["truncated"])
        self.assertEqual(out["skipped"], [{"name": "f.txt", "reason": "text budget reached"}])

    def test_per_file_cap_from_the_declared_size(self):
        calls: list = []
        out = resolve_intake_sources([_stored("k1", 21 * MB)], download=_download_from({}, calls), drive=None,
                                     describe_image=None)
        self.assertEqual(out["sources"][0]["error"], "file is larger than 20 MB")
        self.assertEqual(calls, [])

    def test_pointer_cap(self):
        table = {f"k{i}": ("a.txt", "text/plain", b"a") for i in range(27)}
        out = resolve_intake_sources([_stored(f"k{i}", 1) for i in range(27)], download=_download_from(table),
                                     drive=None, describe_image=None)
        self.assertEqual(len(out["sources"]), 25)
        self.assertEqual(len(out["skipped"]), 2)

    def test_time_budget(self):
        ticks = iter([0, 0, 0, 1000, 1000, 1000])
        table = {f"k{i}": ("a.txt", "text/plain", b"a") for i in range(3)}
        out = resolve_intake_sources([_stored(f"k{i}", 1) for i in range(3)], download=_download_from(table),
                                     drive=None, describe_image=None, deadline_s=10, clock=lambda: next(ticks))
        self.assertEqual(len(out["sources"]), 2)
        self.assertEqual(out["skipped"][0]["reason"], "time budget reached")

    def test_images(self):
        table = {"k1": ("shot.png", "image/png", b"\x89PNG")}
        described = resolve_intake_sources([_stored("k1", 4)], download=_download_from(table), drive=None,
                                           describe_image=lambda name, mime, data: "a login form")
        self.assertEqual(described["sources"][0]["text"], "a login form")
        disabled = resolve_intake_sources([_stored("k1", 4)], download=_download_from(table), drive=None, describe_image=None)
        self.assertEqual(disabled["sources"][0]["error"], "image reading is not enabled")

        def broken(*_args):
            raise RuntimeError("provider 500")

        failed = resolve_intake_sources([_stored("k1", 4)], download=_download_from(table), drive=None, describe_image=broken)
        self.assertEqual(failed["sources"][0]["error"], "image could not be read")

    def test_expired_and_unavailable_sources(self):
        table = {"k1": ObjectNotFound("k1"), "k2": SourceUnavailable(EXPIRED), "k3": RuntimeError("boom")}
        out = resolve_intake_sources([_stored("k1", 1), _stored("k2", 1), _stored("k3", 1)],
                                     download=_download_from(table), drive=None, describe_image=None)
        self.assertEqual([s["error"] for s in out["sources"]], [EXPIRED, EXPIRED, "file could not be read"])
        self.assertIn("Could not be read", out["sources_text"])

    def test_arrival_errors_are_reported_without_fetching(self):
        calls: list = []
        pointer = {**_stored("k1", 1), "error": "attachment not found"}
        out = resolve_intake_sources([pointer], download=_download_from({}, calls), drive=None, describe_image=None)
        self.assertEqual(out["sources"][0]["error"], "attachment not found")
        self.assertEqual(calls, [])

    def test_duplicates_and_junk_are_dropped(self):
        table = {"k1": ("a.txt", "text/plain", b"a")}
        out = resolve_intake_sources([_stored("k1", 1), _stored("k1", 1), "junk", None], download=_download_from(table),
                                     drive=None, describe_image=None)
        self.assertEqual(len(out["sources"]), 1)


if __name__ == "__main__":
    unittest.main()
