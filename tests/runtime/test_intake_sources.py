"""Intake pointers, caps, and document text extraction."""

from __future__ import annotations

import io
import unittest
import zipfile
from unittest import mock

from ts_harness.intake import sources
from ts_harness.intake.sources import (
    MAX_FILE_BYTES,
    MAX_POINTERS,
    MAX_TOTAL_BYTES,
    apply_arrival_caps,
    dedupe_pointers,
    extract_document,
    parse_drive_urls,
    pointers_from_message,
)

DOC_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


def _docx(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


class DriveUrlTests(unittest.TestCase):
    def test_recognizes_every_link_shape(self):
        text = (
            f"doc https://docs.google.com/document/d/{DOC_ID}/edit "
            "sheet https://docs.google.com/spreadsheets/d/2SheetIdAbcdefghij/edit#gid=0 "
            "slides https://docs.google.com/presentation/u/0/d/3SlidesIdAbcdefgh/view "
            "file https://drive.google.com/file/d/4FileIdAbcdefghijk/view?usp=sharing "
            "folder https://drive.google.com/drive/u/1/folders/5FolderIdAbcdefgh "
            "open https://drive.google.com/open?usp=x&id=6OpenIdAbcdefghij"
        )
        found = parse_drive_urls(text)
        self.assertEqual(
            [(p["file_id"], p["drive_type"]) for p in found],
            [
                (DOC_ID, "document"),
                ("2SheetIdAbcdefghij", "spreadsheet"),
                ("3SlidesIdAbcdefgh", "presentation"),
                ("4FileIdAbcdefghijk", "file"),
                ("5FolderIdAbcdefgh", "folder"),
                ("6OpenIdAbcdefghij", "unknown"),
            ],
        )
        self.assertTrue(all(p["kind"] == "drive" for p in found))

    def test_same_file_through_two_link_shapes_is_one_pointer(self):
        text = (
            f"https://docs.google.com/document/d/{DOC_ID}/edit and again "
            f"https://drive.google.com/open?id={DOC_ID}"
        )
        self.assertEqual(len(parse_drive_urls(text)), 1)

    def test_ignores_other_links(self):
        self.assertEqual(parse_drive_urls("see https://example.com/document/d/abcdefghijkl"), [])


class PointerTests(unittest.TestCase):
    def test_attachments_then_drive_links_and_dedupe(self):
        slack = {"kind": "slack_file", "file_id": "F1", "name": "a.pdf", "mime": "application/pdf", "size": 10}
        pointers = pointers_from_message(
            text=f"see https://docs.google.com/document/d/{DOC_ID}/edit",
            attachments=[slack, dict(slack), {"kind": "bogus"}, "nope"],
        )
        self.assertEqual([p["kind"] for p in pointers], ["slack_file", "drive"])

    def test_inline_bytes_are_refused_with_an_error(self):
        pointers = pointers_from_message(text="", attachments=[{"name": "a.txt", "content_base64": "aGk="}])
        self.assertEqual(len(pointers), 1)
        self.assertNotIn("content_base64", pointers[0])
        self.assertIn("/intake/uploads", pointers[0]["error"])

    def test_dedupe_by_identity_per_kind(self):
        pointers = [
            {"kind": "telegram_file", "file_id": "a", "file_unique_id": "U"},
            {"kind": "telegram_file", "file_id": "b", "file_unique_id": "U"},
            {"kind": "stored", "object_key": "k1", "sha256": "s"},
            {"kind": "stored", "object_key": "k2", "sha256": "s"},
            {"kind": "discord_file", "attachment_id": "9"},
            {"kind": "discord_file", "attachment_id": "9"},
        ]
        self.assertEqual(len(dedupe_pointers(pointers)), 3)


class ArrivalCapTests(unittest.TestCase):
    def _stored(self, index: int, size: int) -> dict:
        return {"kind": "stored", "object_key": f"k{index}", "sha256": f"s{index}", "size": size, "name": f"{index}.txt"}

    def test_pointer_count_cap(self):
        pointers = [self._stored(i, 1) for i in range(MAX_POINTERS + 2)]
        out = apply_arrival_caps(pointers)
        self.assertEqual(sum(1 for p in out if not p.get("error")), MAX_POINTERS)
        self.assertIn("too many", out[-1]["error"])

    def test_per_file_cap(self):
        out = apply_arrival_caps([self._stored(1, MAX_FILE_BYTES + 1)])
        self.assertIn("larger than 20 MB", out[0]["error"])

    def test_total_cap_counts_existing_pointers(self):
        existing = [self._stored(i, 19 * 1024 * 1024) for i in range(4)]  # 76 MB
        out = apply_arrival_caps([self._stored(10, 5 * 1024 * 1024), self._stored(11, 2 * 1024 * 1024)], existing=existing)
        self.assertIn("80 MB in total", out[0]["error"])
        self.assertNotIn("error", out[1])
        self.assertLessEqual(76 * 1024 * 1024 + 2 * 1024 * 1024, MAX_TOTAL_BYTES)

    def test_duplicates_of_existing_pointers_are_dropped(self):
        existing = [self._stored(1, 5)]
        self.assertEqual(apply_arrival_caps([self._stored(1, 5)], existing=existing), [])

    def test_inputs_are_not_mutated(self):
        pointer = self._stored(1, MAX_FILE_BYTES + 1)
        apply_arrival_caps([pointer])
        self.assertNotIn("error", pointer)


class ExtractTests(unittest.TestCase):
    def test_text_markdown_csv_json(self):
        for name, mime in (("a.txt", "text/plain"), ("a.md", None), ("a.csv", "text/csv"), ("a.json", "application/json")):
            out = extract_document(name, mime, "héllo".encode("utf-8-sig"))
            self.assertEqual((out["text"], out["error"]), ("héllo", None), name)

    def test_docx(self):
        out = extract_document("brief.docx", None, _docx(["Title", "Second para"]))
        self.assertEqual(out["text"], "Title\nSecond para")
        self.assertIsNone(out["error"])

    def test_broken_docx_is_an_error_not_a_raise(self):
        out = extract_document("brief.docx", sources.DOCX_MIME, b"not a zip")
        self.assertEqual(out["error"], "file could not be read")

    def test_pdf_via_pypdf(self):
        page = mock.Mock()
        page.extract_text.return_value = "page text"
        reader = mock.Mock(is_encrypted=False, pages=[page, page])
        with mock.patch("pypdf.PdfReader", return_value=reader):
            out = extract_document("spec.pdf", "application/pdf", b"%PDF-1.7")
        self.assertEqual(out["text"], "page text\n\npage text")

    def test_password_pdf(self):
        reader = mock.Mock(is_encrypted=True)
        reader.decrypt.return_value = 0
        with mock.patch("pypdf.PdfReader", return_value=reader):
            out = extract_document("spec.pdf", "application/pdf", b"%PDF")
        self.assertEqual(out["error"], "PDF is password protected")

    def test_scanned_pdf_says_so(self):
        page = mock.Mock()
        page.extract_text.return_value = ""
        with mock.patch("pypdf.PdfReader", return_value=mock.Mock(is_encrypted=False, pages=[page])):
            out = extract_document("scan.pdf", "application/pdf", b"%PDF")
        self.assertIn("no extractable text", out["error"])

    def test_malformed_pdf(self):
        out = extract_document("bad.pdf", "application/pdf", b"garbage")
        self.assertEqual(out["error"], "file could not be read")

    def test_character_cap_sets_truncated(self):
        out = extract_document("big.txt", "text/plain", b"x" * 100, limit=10)
        self.assertEqual((len(out["text"]), out["truncated"]), (10, True))

    def test_images_need_vision(self):
        out = extract_document("shot.png", "image/png", b"\x89PNG")
        self.assertEqual((out["needs_vision"], out["error"], out["text"]), (True, None, ""))
        self.assertEqual(extract_document("x.svg", "image/svg+xml", b"<svg/>")["error"], "unsupported image type")

    def test_unknown_type(self):
        out = extract_document("a.bin", "application/octet-stream", b"\x00\x01")
        self.assertEqual(out["error"], "unsupported type")

    def test_every_result_has_one_shape(self):
        for args in (("a.txt", "text/plain", b"x"), ("a.png", "image/png", b""), ("a.bin", None, b"")):
            self.assertEqual(set(extract_document(*args)), {"text", "truncated", "error", "needs_vision"})


if __name__ == "__main__":
    unittest.main()
