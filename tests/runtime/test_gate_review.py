import unittest
from unittest import mock

from ts_harness.workflows.gate_review import build_gate_review, gate_review

PLANNING = [
    {"id": "refine", "step_name": "refine_requirements", "depends_on": [], "output": {}},
    {
        "id": "plan",
        "step_name": "plan_work_orders",
        "depends_on": ["refine"],
        "output": {
            "summary": "5 work orders.",
            "warnings": ["FB-004 and FB-005 combined."],
            "confidence": "medium",
            "artifacts": {"work_orders_markdown": "# WO-001", "context_graph_markdown": "graph"},
        },
    },
    {"id": "gate", "step_name": "approve_work_orders", "workflow_type": "work_orders_from_prd",
     "depends_on": ["plan"], "approval_required": True, "approval_status": "pending"},
]

TRACK = [
    {"id": "plan", "step_name": "plan_track", "output": {
        "repo": "o/r", "sha": "abc", "base_branch": "fe-dev", "pr_urls": ["https://github.com/o/r/pull/1"],
        "environments": ["staging", "production"],
        "slots": {"staging": {"rollback_sha": "old"}, "production": {"rollback_sha": "old", "required_roles": ["release"]}},
    }},
    {"id": "ci", "step_name": "await_ci_on_sha", "output": {"checks_state": "success", "checks": {"e2e": "success"}}},
    {"id": "rec", "step_name": "record_release_staging",
     "output": {"status": "succeeded", "evidence_url": "https://github.com/o/r/actions/runs/1"}},
    {"id": "gate-s", "step_name": "approve_promote_staging", "workflow_type": "release_track", "depends_on": ["ci"]},
    {"id": "gate-p", "step_name": "approve_promote_production", "workflow_type": "release_track", "depends_on": ["rec"]},
]


class GateReviewTests(unittest.TestCase):
    def test_planning_gate_shows_the_documents_before_it(self):
        review = build_gate_review(PLANNING[2], PLANNING)
        self.assertEqual(review["kind"], "documents")
        self.assertEqual(review["from_step"], "plan_work_orders")
        self.assertEqual(review["summary"], "5 work orders.")
        self.assertEqual(review["warnings"], ["FB-004 and FB-005 combined."])
        self.assertEqual(
            [(d["title"], d["markdown"]) for d in review["documents"]],
            [("Work orders", "# WO-001"), ("Context graph", "graph")],
        )

    def test_prd_gate_shows_the_prd(self):
        tasks = [
            {"id": "draft", "step_name": "draft_prd", "output": {"summary": "Drafted", "result": {"prd_markdown": "# PRD"}}},
            {"id": "gate", "step_name": "approve_prd", "depends_on": ["draft"]},
        ]
        review = build_gate_review(tasks[1], tasks)
        self.assertEqual([(d["title"], d["markdown"]) for d in review["documents"]], [("PRD", "# PRD")])

    def test_staging_gate_shows_the_release(self):
        release = build_gate_review(TRACK[3], TRACK)["release"]
        self.assertEqual(release["environment"], "staging")
        self.assertEqual((release["repo"], release["sha"], release["base_branch"]), ("o/r", "abc", "fe-dev"))
        self.assertEqual(release["pr_urls"], ["https://github.com/o/r/pull/1"])
        self.assertEqual(release["ci"]["state"], "success")
        self.assertEqual(release["compare_url"], "https://github.com/o/r/compare/old...abc")
        self.assertEqual(release["commit_url"], "https://github.com/o/r/commit/abc")
        self.assertNotIn("previous", release)

    def test_production_gate_shows_how_staging_went(self):
        release = build_gate_review(TRACK[4], TRACK)["release"]
        self.assertEqual(release["environment"], "production")
        self.assertEqual(release["required_roles"], ["release"])
        self.assertEqual(
            release["previous"],
            {"environment": "staging", "status": "succeeded", "evidence_url": "https://github.com/o/r/actions/runs/1"},
        )

    def test_a_jira_kickoff_shows_the_ticket_it_approves(self):
        gate = {
            "id": "hydrate",
            "step_name": "hydrate_ticket",
            "workflow_type": "implement_jira_ticket",
            "approval_required": True,
            "output": {"summary": "PR against fe-dev on acme/web", "work_order_body": "# THD-33 Comment count", "repo_url": "x"},
        }
        review = build_gate_review(gate, [gate])
        self.assertEqual((review["kind"], review["from_step"]), ("documents", "hydrate_ticket"))
        self.assertEqual(review["summary"], "PR against fe-dev on acme/web")
        self.assertEqual(review["documents"][0]["markdown"], "# THD-33 Comment count")

    def test_gate_review_loads_the_gates_workflow(self):
        memory = mock.Mock()
        memory.task_program.return_value = {"workflow_id": "wf-1", "program_id": "p"}
        memory.get_workflow_tasks.return_value = {"tasks": PLANNING}
        self.assertEqual(gate_review(memory, "gate")["kind"], "documents")
        memory.get_workflow_tasks.assert_called_once_with("wf-1")
        self.assertIsNone(gate_review(memory, "missing"))
