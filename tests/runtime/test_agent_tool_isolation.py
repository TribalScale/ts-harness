"""Agent tool allow and deny matrix."""

from __future__ import annotations

import unittest

from ts_harness.evals.harness import tool_matrix_failures


class AgentToolIsolationTests(unittest.TestCase):
    def test_agent_tool_matrix(self):
        self.assertEqual(tool_matrix_failures(), [])
