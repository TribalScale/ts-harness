"""OpenCode may edit only the files a work order declares; what slips through is named to the fix pass."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ts_harness.implementation.backends.opencode import opencode_scope_env, opencode_scope_paths
from ts_harness.implementation.verification import VerifyResult, build_verification_fix_prompt

BODY = """# WO-1 Weekly summary

## E2E Acceptance Tests
- Given a ledger, When it runs, Then it counts.

## Files
- src/reminder-worker/summary.js
- tests/reminder-worker/
- package.json
"""


class ScopePathsTests(unittest.TestCase):
    def test_declared_files_tests_features_and_pipeline_paths_are_allowed(self):
        allowed = opencode_scope_paths(BODY)
        self.assertEqual(allowed[:4], ["src/reminder-worker/summary.js", "tests/reminder-worker/**", "package.json", "features/**"])
        self.assertIn(".opencode/**", allowed)
        self.assertIn("docs/planner/**", allowed)
        self.assertNotIn("src/reminder-worker/index.js", allowed)

    def test_no_files_list_means_no_restriction(self):
        self.assertIsNone(opencode_scope_paths("# WO-2\n\nDo the thing."))

    def test_review_revision_files_are_added(self):
        self.assertIn("src/reminder-worker/index.js", opencode_scope_paths(BODY, ["src/reminder-worker/index.js"]))


class ScopeConfigTests(unittest.TestCase):
    def test_edits_are_denied_except_the_allowed_paths(self):
        directory = Path(tempfile.mkdtemp())
        env = opencode_scope_env({}, directory, ["src/a.js", "tests/**"])
        config = json.loads(Path(env["OPENCODE_CONFIG"]).read_text())
        self.assertEqual(config["permission"]["edit"], {"*": "deny", "src/a.js": "allow", "tests/**": "allow"})
        self.assertEqual(list(config["permission"]["edit"])[0], "*")  # last match wins

    def test_the_llm_proxy_config_is_kept(self):
        directory = Path(tempfile.mkdtemp())
        proxy = directory / "opencode-llm-proxy.json"
        proxy.write_text(json.dumps({"provider": {"anthropic": {"options": {"baseURL": "http://proxy"}}}}))
        env = opencode_scope_env({"OPENCODE_CONFIG": str(proxy)}, directory, ["src/a.js"])
        config = json.loads(Path(env["OPENCODE_CONFIG"]).read_text())
        self.assertEqual(config["provider"]["anthropic"]["options"]["baseURL"], "http://proxy")
        self.assertIn("edit", config["permission"])


class FixPromptTests(unittest.TestCase):
    def test_reverted_files_are_named_in_the_fix_prompt(self):
        verify = VerifyResult(ok=False, reasons=["unit_tests_failed"], summary="", log_tail="TypeError")
        prompt = build_verification_fix_prompt("WO-1", verify, reverted_paths=["src/reminder-worker/index.js"])
        self.assertIn("were reverted: src/reminder-worker/index.js", prompt)
        self.assertIn("importing from the declared module directly", prompt)
        self.assertNotIn("were reverted", build_verification_fix_prompt("WO-1", verify))


if __name__ == "__main__":
    unittest.main()
