"""Listed files already up to date don't block, and readiness questions reach the chat unwrapped."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from ts_harness.implementation import verification as v

BODY = "# PROJ-50 Regenerate API types\n\n## Files\n- src/api/openapi.json\n- src/api/schema.d.ts\n- src/App.tsx\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class AlreadyCurrentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = repo = Path(self.tmp.name)
        _git(repo, "init", "-q", "-b", "dev")
        (repo / "src/api").mkdir(parents=True)
        for name in ("src/api/openapi.json", "src/api/schema.d.ts", "src/App.tsx"):
            (repo / name).write_text("x\n")
        _git(repo, "add", ".")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "base")
        (repo / "src/App.tsx").write_text("y\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_untouched_listed_files_block_without_evidence(self):
        self.assertEqual(v.untouched_required_files(self.repo, BODY, "HEAD"), ["src/api/openapi.json", "src/api/schema.d.ts"])

    def test_files_shown_to_be_current_are_accepted_and_reported(self):
        (self.repo / ".ts-harness").mkdir()
        (self.repo / v.ALREADY_CURRENT_FILE).write_text(json.dumps([
            {"path": "src/api/openapi.json", "evidence": "npm run api:pull changed nothing; cmp with the backend spec is identical"},
            {"path": "src/api/schema.d.ts", "evidence": ""},
        ]))
        current = v.already_current_files(self.repo)
        self.assertEqual(list(current), ["src/api/openapi.json"])  # no evidence, no waiver

        from ts_harness.implementation.backends.base import CodingRequest
        from ts_harness.implementation.pr_description import pr_body

        body = pr_body(CodingRequest(repo_url="https://github.com/org/web", base_branch="dev", work_order_id="PROJ-50",
                                     work_order_body=BODY, workspace_dir=Path(self.tmp.name), provider="anthropic"),
                       changed_files=["src/App.tsx"], repo_dir=self.repo)
        self.assertIn("## Listed files already up to date", body)
        self.assertIn("cmp with the backend spec is identical", body)


class ReadinessMessageTests(unittest.TestCase):
    def test_the_chat_gets_the_questions_without_the_failure_wrapper(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from ts_harness.workflows.hooks import notify_bound_channel as nb

        posted = {}

        def web(ctx, source, message):
            posted["message"] = message
            return True

        ctx = SimpleNamespace(
            workflow_id="wf-1", workflow={"source_channel": "web:web:t1"}, task={"id": "t", "step_name": "prepare_ticket_delivery"},
            extras={"error": "barrier_blocked: needs_answers: PROJ-60 needs 1 answer before it's built.\n\n1. Is message null?"},
            runner_event="blocked", tenant_id="default",
            skeleton=SimpleNamespace(notifications={"bound_channel": True}),
        )
        with patch.object(nb, "_append_web_transcript", side_effect=web), \
                patch("ts_harness.gateway.workflow_presentation.bound_channel", return_value=("web", "web", "t1")):
            nb.handle(ctx)
        self.assertTrue(posted["message"].startswith("PROJ-60 needs 1 answer"))
        self.assertNotIn("is blocked and will not retry", posted["message"])
        self.assertNotIn("barrier_blocked", posted["message"])


if __name__ == "__main__":
    unittest.main()
