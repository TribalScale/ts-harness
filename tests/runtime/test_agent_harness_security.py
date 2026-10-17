"""Harness refusals: injected tool calls stay denied, and summaries are redacted."""

from __future__ import annotations

import unittest

from ts_harness.evals.harness import injected_write_denied, qa_exec_denied, summary_redacts_secret


class AgentHarnessSecurityTests(unittest.TestCase):
    def test_injected_write_file_is_denied_for_each_source(self):
        self.assertTrue(injected_write_denied())

    def test_summary_redacts_registered_secret(self):
        self.assertTrue(summary_redacts_secret())

    def test_qa_exec_is_denied(self):
        self.assertTrue(qa_exec_denied())
