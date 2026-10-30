from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from ts_harness.workflows.hooks.complete_program_phase import _try_publish_brief
from ts_harness.workflows.program_brief import extract_brief_entries, publish_program_brief
from ts_harness.workflows.registry import HookContext


class ProgramBriefExtractTests(unittest.TestCase):
    def test_prd_approve_emits_pointer_and_facts(self):
        entries = extract_brief_entries(
            phase="prd",
            artifacts={
                "approved_prd": {"title": "Payments v2"},
                "jira_project_key": "PROJ",
                "repo_url": "https://github.com/acme/payments",
            },
        )
        kinds = {(item["kind"], item["topic"]) for item in entries}
        self.assertIn(("pointer", "approved_prd"), kinds)
        self.assertIn(("fact", "jira_project_key"), kinds)
        self.assertIn(("fact", "repo_url"), kinds)
        pointer = next(item for item in entries if item["topic"] == "approved_prd")
        self.assertEqual(pointer["content"], "Payments v2")
        self.assertNotIn("full prd body", pointer["content"].lower())

    def test_draft_prd_is_not_published(self):
        entries = extract_brief_entries(
            phase="prd",
            artifacts={"prd_markdown": "# Draft", "repo_url": "https://github.com/acme/payments"},
        )
        topics = {item["topic"] for item in entries}
        self.assertNotIn("approved_prd", topics)
        self.assertIn("repo_url", topics)


class ProgramBriefPublishTests(unittest.TestCase):
    def test_publish_exception_does_not_raise_from_hook(self):
        client = MagicMock()
        client.upsert_program_briefs.side_effect = RuntimeError("memory down")
        ctx = HookContext(
            workflow_id="wf-1",
            workflow_type="prd_request",
            tenant_id="default",
            task={"status": "succeeded"},
            stage=None,
            skeleton=MagicMock(program={}),
            actor="workflow-runner",
            memory_client=client,
        )
        from unittest.mock import patch

        with patch(
            "ts_harness.workflows.hooks.complete_program_phase.publish_program_brief",
            side_effect=RuntimeError("boom"),
        ):
            status = _try_publish_brief(
                ctx,
                "prog-1",
                {
                    "tenant_id": "default",
                    "project_id": "PROJ",
                    "metadata": {"artifacts": {"approved_prd": {"title": "X"}}},
                },
                "prd",
            )
        self.assertEqual(status, "failed")

    def test_publish_calls_upsert(self):
        client = MagicMock()
        client.upsert_program_briefs.return_value = {"entries": [{"topic": "approved_prd"}]}
        result = publish_program_brief(
            client,
            program_id="p1",
            tenant_id="default",
            project_id="PROJ",
            phase="prd",
            artifacts={"approved_prd": {"title": "X"}},
            actor="test",
        )
        client.upsert_program_briefs.assert_called_once()
        self.assertEqual(result["entries"][0]["topic"], "approved_prd")


if __name__ == "__main__":
    unittest.main()
