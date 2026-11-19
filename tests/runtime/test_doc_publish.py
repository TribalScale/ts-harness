"""Tests for Google Doc artifact publishing helpers."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from pathlib import Path

from ts_harness.config.schema import load_runtime_config
from ts_harness.connectors.google_workspace.doc_publish import (
    _build_formatted_insert_requests,
    publish_markdown_to_google_doc,
    should_publish_artifact_text,
)
from ts_harness.workflows.artifacts import format_artifact_for_channel

REPO_ROOT = Path(__file__).resolve().parents[2]


def _runtime_with_google_publish() -> object:
    return load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")


class DocPublishHelperTests(unittest.TestCase):
    def test_should_publish_when_above_min_chars(self):
        self.assertTrue(
            should_publish_artifact_text(3500, max_channel_length=3900, min_chars=3000)
        )

    def test_should_publish_when_would_truncate(self):
        self.assertTrue(
            should_publish_artifact_text(4000, max_channel_length=3900, min_chars=3000)
        )

    def test_should_not_publish_for_short_text(self):
        self.assertFalse(
            should_publish_artifact_text(500, max_channel_length=3900, min_chars=3000)
        )

    def test_build_formatted_insert_requests_applies_heading_style(self):
        content = "# Title\n\nBody line\n"
        requests, end_index = _build_formatted_insert_requests(content, start_index=1)
        self.assertGreater(end_index, 1)
        self.assertTrue(any("insertText" in item for item in requests))
        self.assertTrue(
            any(
                item.get("updateParagraphStyle", {}).get("paragraphStyle", {}).get(
                    "namedStyleType"
                ) == "HEADING_1"
                for item in requests
            )
        )

    @patch("ts_harness.connectors.google_workspace.doc_publish.build_gw_clients")
    def test_publish_markdown_creates_doc_and_shares(self, mock_clients):
        drive = MagicMock()
        docs = MagicMock()
        created = {"id": "doc-1", "name": "Artifact", "webViewLink": "https://docs.example/doc-1"}
        drive.files.return_value.create.return_value.execute.return_value = created
        drive.permissions.return_value.create.return_value.execute.return_value = {}
        docs.documents.return_value.get.return_value.execute.return_value = {
            "body": {"content": [{"endIndex": 2}]},
        }
        docs.documents.return_value.batchUpdate.return_value.execute.return_value = {}
        mock_clients.return_value = (drive, docs, MagicMock())

        result = publish_markdown_to_google_doc(
            title="Refinery output",
            content="# FRD-001\n\nWhen a player joins, the system shall match them.",
            runtime_config=_runtime_with_google_publish(),
            tenant_id="default",
            agent_id="pm-agent",
            share_with_email="reviewer@test.local",
        )

        self.assertEqual(result["status"], "published")
        self.assertEqual(result["webViewLink"], "https://docs.example/doc-1")
        drive.permissions.return_value.create.assert_called_once()

    def test_format_artifact_includes_doc_link_when_provided(self):
        message = format_artifact_for_channel(
            {
                "prd_title": "Checker Game",
                "prd_markdown": "# Overview\n\n" + ("detail line\n" * 400),
            },
            display_name="PRD review",
            present_prompt="approval",
            step_name="draft_prd",
            agent_id="pm-agent",
            doc_url="https://docs.example/full",
            publish_full_text=True,
        )
        self.assertIn("https://docs.example/full", message)
        self.assertIn("Google Docs", message)


if __name__ == "__main__":
    unittest.main()


class MarkdownImportTests(unittest.TestCase):
    """Docs are created by uploading markdown for Drive to convert (tables, lists, bold...)."""

    MARKDOWN = "# PRD\n\n**Status:** Draft\n\n- [ ] **FR-1** One app\n\n| Role | Can |\n|---|---|\n| Admin | All |\n"

    def _drive(self, *, fail_media: bool = False):
        drive = MagicMock()
        calls = []

        def create(**kwargs):
            calls.append(kwargs)
            if fail_media and "media_body" in kwargs:
                raise RuntimeError("conversion unavailable")
            request = MagicMock()
            request.execute.return_value = {"id": "doc-1", "name": kwargs["body"]["name"], "webViewLink": "https://docs/x"}
            return request

        drive.files.return_value.create.side_effect = create
        return drive, calls

    def test_markdown_is_uploaded_for_drive_to_convert(self):
        from ts_harness.connectors.google_workspace.doc_helpers import DOCUMENT_MIME, create_document_with_markdown

        drive, calls = self._drive()
        docs = MagicMock()
        out = create_document_with_markdown(drive, docs, title="PRD", content=self.MARKDOWN, parent_folder_id="f1")
        self.assertEqual(out["document_id"], "doc-1")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["body"], {"name": "PRD", "mimeType": DOCUMENT_MIME, "parents": ["f1"]})
        media = calls[0]["media_body"]
        self.assertEqual(media.mimetype(), "text/markdown")
        self.assertEqual(media.getbytes(0, media.size()).decode(), self.MARKDOWN.strip())
        docs.documents.return_value.batchUpdate.assert_not_called()

    def test_a_failed_import_falls_back_to_writing_the_text(self):
        from ts_harness.connectors.google_workspace.doc_helpers import create_document_with_markdown

        drive, calls = self._drive(fail_media=True)
        docs = MagicMock()
        with self.assertLogs("ts_harness.connectors.google_workspace.doc_helpers", level="WARNING"):
            out = create_document_with_markdown(drive, docs, title="PRD", content=self.MARKDOWN)
        self.assertEqual(out["document_id"], "doc-1")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("media_body", calls[1])
        docs.documents.return_value.batchUpdate.assert_called()
