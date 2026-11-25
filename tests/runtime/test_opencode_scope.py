"""OpenCode may edit only the files a work order declares; what slips through is named to the fix pass."""

from __future__ import annotations

import unittest

from ts_harness.implementation.verification import VerifyResult, build_verification_fix_prompt

BODY = """# WO-1 Weekly summary

## E2E Acceptance Tests
- Given a ledger, When it runs, Then it counts.

## Files
- src/reminder-worker/summary.js
- tests/reminder-worker/
- package.json
"""



class FixPromptTests(unittest.TestCase):
    def test_reverted_files_are_named_in_the_fix_prompt(self):
        verify = VerifyResult(ok=False, reasons=["unit_tests_failed"], summary="", log_tail="TypeError")
        prompt = build_verification_fix_prompt("WO-1", verify, reverted_paths=[".github/workflows/ci.yml"])
        self.assertIn("were reverted: .github/workflows/ci.yml", prompt)
        self.assertIn("protected (CI workflows, code owners)", prompt)
        self.assertNotIn("were reverted", build_verification_fix_prompt("WO-1", verify))


if __name__ == "__main__":
    unittest.main()
