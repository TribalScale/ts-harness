"""Tests for structured Google Drive artifact library publishing."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ts_harness.config.schema import (
    GoogleWorkspaceArtifactLibraryConfig,
    load_runtime_config,
)
from ts_harness.connectors.google_workspace.artifact_library import (
    DEFAULT_STEP_PHASE_MAP,
    extract_publishable_markdown,
    program_slug,
    publish_program_artifacts,
    resolve_phase_folder,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class ArtifactLibraryHelperTests(unittest.TestCase):
    def test_program_slug_sanitizes_summary(self):
        slug = program_slug("a1b2c3d4-e5f6-7890-abcd-ef1234567890", "Online Checkers PRD!")
        self.assertIn("online-checkers", slug)
        self.assertIn("a1b2c3d4", slug)

    def test_resolve_phase_folder_uses_defaults(self):
        cfg = GoogleWorkspaceArtifactLibraryConfig(enabled=True)
        self.assertEqual(
            resolve_phase_folder("work_orders_from_prd", "refine_requirements", cfg),
            "refinery",
        )
        self.assertEqual(
            resolve_phase_folder("prd_request", "draft_prd", cfg),
            "prd",
        )
        self.assertIsNone(
            resolve_phase_folder("work_orders_from_prd", "approve_refinery", cfg),
        )

    def test_extract_publishable_markdown(self):
        fields = extract_publishable_markdown(
            {
                "frds_markdown": "# FRD-001",
                "summary": "ignored",
                "markdown": "# PRD",
                "count": 3,
            }
        )
        self.assertEqual(fields["frds_markdown"], "# FRD-001")
        self.assertEqual(fields["markdown"], "# PRD")
        self.assertNotIn("summary", fields)

    def test_default_step_phase_map_covers_work_orders(self):
        self.assertIn("refine_requirements", DEFAULT_STEP_PHASE_MAP["work_orders_from_prd"])


class PublishProgramArtifactsTests(unittest.TestCase):
    @patch("ts_harness.connectors.google_workspace.artifact_library.build_gw_clients")
    def test_publish_creates_docs_and_manifest(self, mock_clients):
        drive = MagicMock()
        docs = MagicMock()
        mock_clients.return_value = (drive, docs, MagicMock())

        folder_counter = {"n": 0}

        def execute_side_effect(*_args, **_kwargs):
            call = drive.files.return_value.create.call_args
            body = (call.kwargs if call else {}).get("body") or {}
            folder_counter["n"] += 1
            n = folder_counter["n"]
            mime = body.get("mimeType", "")
            if mime == "application/vnd.google-apps.folder":
                fid = f"folder-{n}"
            else:
                fid = f"doc-{n}"
            return {"id": fid, "name": body.get("name"), "webViewLink": f"https://drive/{fid}"}

        drive.files.return_value.create.return_value.execute.side_effect = execute_side_effect
        drive.files.return_value.list.return_value.execute.return_value = {"files": []}
        drive.files.return_value.update.return_value.execute.return_value = {}
        drive.permissions.return_value.create.return_value.execute.return_value = {}
        docs.documents.return_value.get.return_value.execute.return_value = {
            "body": {"content": [{"endIndex": 2}]},
        }
        docs.documents.return_value.batchUpdate.return_value.execute.return_value = {}

        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        gw = config.connectors.google_workspace
        assert gw is not None
        gw.artifact_publish.folder_id = "root-folder"
        gw.artifact_publish.library.enabled = True

        result = publish_program_artifacts(
            artifact={"frds_markdown": "# FRD-001\n\n## FRD-001\n\nContent"},
            runtime_config=config,
            workflow_type="work_orders_from_prd",
            workflow_id="wf-123",
            step_name="refine_requirements",
            program_id="prog-abc",
            program_summary="Online checkers",
            share_with_email="reviewer@test.local",
        )

        self.assertIn(result["status"], {"published", "partial"})
        self.assertTrue(result.get("root_web_view_link"))
        self.assertTrue(result.get("manifest_web_view_link"))
        drive_library = result.get("drive_library") or {}
        self.assertTrue(drive_library.get("root_folder_id"))
        entries = drive_library.get("entries") or []
        self.assertEqual(len(entries), 1)
        self.assertIn("frds_markdown", entries[0].get("documents", {}))

    @patch("ts_harness.connectors.google_workspace.artifact_library.build_gw_clients")
    def test_publish_skips_unmapped_step(self, mock_clients):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        gw = config.connectors.google_workspace
        assert gw is not None
        gw.artifact_publish.folder_id = "root-folder"
        gw.artifact_publish.library.enabled = True

        result = publish_program_artifacts(
            artifact={"frds_markdown": "# FRD"},
            runtime_config=config,
            workflow_type="work_orders_from_prd",
            workflow_id="wf-123",
            step_name="approve_refinery",
        )
        self.assertEqual(result["status"], "skipped")
        mock_clients.assert_not_called()


if __name__ == "__main__":
    unittest.main()
