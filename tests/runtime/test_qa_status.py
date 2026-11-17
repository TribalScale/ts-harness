import unittest

from ts_harness.integrations.github_client import LATER_GATE_CONTEXTS, summarize_checks
from ts_harness.workflows import qa_status
from ts_harness.workflows.human_decisions import accepted_output

PR = "https://github.com/o/r/pull/19"
TASK_INPUT = {"pr_url": PR, "prs": [{"pr_url": PR, "head_sha": "62bb0d4"}]}


class _GitHub:
    def __init__(self):
        self.statuses = []

    def create_status(self, owner, repo, sha, *, state, context, description=""):
        self.statuses.append((owner, repo, sha, state, context, description))


def _qa_output(status, defects=()):
    return {"status": status, "result": {"prs": [{"pr_url": PR, "status": status, "defects": list(defects)}]}}


class QaStatusTests(unittest.TestCase):
    def test_marks_each_pr_head_pending_while_qa_runs(self):
        github = _GitHub()
        self.assertEqual(qa_status.qa_started(TASK_INPUT, github=github), [])
        self.assertEqual(github.statuses, [("o", "r", "62bb0d4", "pending", "harness/qa", "QA running")])

    def test_success_when_qa_passes(self):
        github = _GitHub()
        qa_status.qa_finished(TASK_INPUT, {"status": "succeeded", "output": _qa_output("success")}, github=github)
        self.assertEqual(github.statuses[-1][3:], ("success", "harness/qa", "QA passed"))

    def test_failure_names_the_blocking_defect_count(self):
        github = _GitHub()
        defects = [{"severity": "blocking", "description": "panel not wired"}, {"severity": "minor"}]
        qa_status.qa_finished(
            TASK_INPUT,
            {"status": "blocked", "error": "stage contract outcome: blocked", "output": _qa_output("failed", defects)},
            github=github,
        )
        self.assertEqual(github.statuses[-1][3:], ("failure", "harness/qa", "QA failed: 1 blocking of 2 defects"))

    def test_error_when_qa_could_not_finish(self):
        github = _GitHub()
        qa_status.qa_finished(TASK_INPUT, {"status": "failed", "error": "transport_error: timeout"}, github=github)
        self.assertEqual(github.statuses[-1][3:5], ("error", "harness/qa"))
        self.assertIn("timeout", github.statuses[-1][5])

    def test_keeps_pending_while_waiting_for_a_tool_approval(self):
        github = _GitHub()
        qa_status.qa_finished(TASK_INPUT, {"status": "blocked", "error": "awaiting_tool_approval:ap-1"}, github=github)
        self.assertEqual(github.statuses, [])

    def test_skips_prs_without_a_head_commit(self):
        github = _GitHub()
        warnings = qa_status.qa_started({"pr_url": PR}, github=github)
        self.assertEqual(github.statuses, [])
        self.assertEqual(warnings, [])

    def test_accepting_a_blocked_qa_gate_marks_the_status_as_an_override(self):
        github = _GitHub()
        task = {
            "step_name": "qa_gate",
            "input": {**TASK_INPUT, "_human_accept": {"actor": "ana@example.com", "at": "now"}},
            "output": _qa_output("failed"),
        }
        output = accepted_output(task, github=github)
        self.assertEqual(output["human_override"]["actor"], "ana@example.com")
        self.assertEqual(
            github.statuses[-1][3:], ("success", "harness/qa", "accepted by ana@example.com (human override)")
        )

    def test_pr_checks_wait_ignores_the_qa_status(self):
        self.assertIn("harness/qa", LATER_GATE_CONTEXTS)
        combined = {"statuses": [{"context": "ci", "state": "success"}, {"context": "harness/qa", "state": "pending"}]}
        self.assertEqual(summarize_checks(combined, [])["state"], "success")


if __name__ == "__main__":
    unittest.main()
