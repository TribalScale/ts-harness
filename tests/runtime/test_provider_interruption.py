"""A coding run the model provider cut off keeps its work and is continued, not failed."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ts_harness.implementation.backends.opencode import OpenCodeBackend, provider_interruption


class DetectionTests(unittest.TestCase):
    def test_a_content_filter_block_is_an_interruption(self):
        log = "$ node -e '...\nunexpected EOF\n\nError: The response was blocked by the provider's content filter\n"
        self.assertIn("content filter", provider_interruption(log))

    def test_overload_and_rate_limit_count(self):
        self.assertTrue(provider_interruption("Error: 529 overloaded_error"))
        self.assertTrue(provider_interruption("APIError: rate limit exceeded"))

    def test_a_test_failure_mentioning_503_does_not(self):
        log = "expected status 503 but got 200\nFAIL src/api.test.ts\nError: tests failed"
        self.assertEqual(provider_interruption(log), "")

    def test_an_old_error_far_above_the_end_does_not(self):
        log = "Error: overloaded\n" + "\n".join(f"line {i}" for i in range(100)) + "\nError: something else"
        self.assertEqual(provider_interruption(log), "")


class SaveWorkTests(unittest.TestCase):
    def backend(self):
        backend = OpenCodeBackend.__new__(OpenCodeBackend)
        backend.runner = MagicMock()
        return backend

    def request(self, **extra):
        return SimpleNamespace(pr_url=extra.get("pr_url", ""), base_branch="dev", work_order_id="WO-1",
                               repo_url="https://github.com/acme/app")

    def test_a_first_runs_work_is_committed_and_pushed(self):
        with patch("ts_harness.implementation.security_scan.scan_secrets", return_value=SimpleNamespace(findings=[])), \
                patch("ts_harness.implementation.repo.commit_all_changes", return_value=True) as commit, \
                patch("ts_harness.implementation.backends.opencode.push_branch") as push, \
                patch("ts_harness.implementation.backends.opencode._git_env", return_value={}):
            note = self.backend()._save_interrupted_work(Path("/r"), self.request(), "wo/wo-1", ["a.ts"])
        self.assertEqual(note, "work in progress pushed to wo/wo-1")
        self.assertIn("interrupted by the model provider", commit.call_args.kwargs["message"])
        self.assertEqual(push.call_args.kwargs["branch"], "wo/wo-1")

    def test_never_onto_an_open_pr_or_without_changes(self):
        backend = self.backend()
        with patch("ts_harness.implementation.backends.opencode.push_branch") as push:
            self.assertEqual(backend._save_interrupted_work(Path("/r"), self.request(pr_url="https://x/pull/1"), "b", ["a"]), "")
            self.assertEqual(backend._save_interrupted_work(Path("/r"), self.request(), "b", []), "")
        push.assert_not_called()

    def test_a_secret_scan_finding_keeps_it_local(self):
        with patch("ts_harness.implementation.security_scan.scan_secrets", return_value=SimpleNamespace(findings=[{"rule": "x"}])), \
                patch("ts_harness.implementation.backends.opencode.push_branch") as push:
            note = self.backend()._save_interrupted_work(Path("/r"), self.request(), "b", ["a.ts"])
        self.assertIn("not pushed", note)
        push.assert_not_called()


if __name__ == "__main__":
    unittest.main()
