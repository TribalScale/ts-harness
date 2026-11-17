import unittest

from ts_harness.workflows.phase_activity import phase_activity, summarize_workflow

STEPS = [
    ("deliver_work_order", "dev-agent"),
    ("await_pr_checks", "orchestrator"),
    ("review_precheck", "orchestrator"),
    ("code_review", "review-agent"),
    ("finalize_code_review", "orchestrator"),
    ("qa_gate", "qa-agent"),
    ("ensure_fresh", "orchestrator"),
    ("await_human_merge", "orchestrator"),
]


def _round(created_at, statuses, *, work_order="WO-003", pr_url="https://github.com/o/r/pull/19", errors=None, inputs=None):
    errors = errors or {}
    inputs = inputs or {}
    tasks = []
    for (step, agent), status in zip(STEPS, statuses):
        task = {
            "id": f"{step}-{created_at}",
            "step_name": step,
            "agent_id": agent,
            "status": status,
            "created_at": created_at,
            "updated_at": created_at,
            "error": errors.get(step),
            "input": {"work_order_id": work_order, **inputs.get(step, {})},
            "output": {},
        }
        if step == "deliver_work_order" and status == "succeeded":
            task["output"] = {"pr_url": pr_url, "work_order_id": work_order}
        tasks.append(task)
    return tasks


def _workflow(tasks, status="running"):
    return {"id": "wf-3", "status": status, "workflow_type": "deliver_work_order", "tasks": tasks}


class SummarizeDeliveryTests(unittest.TestCase):
    def test_reports_qa_running_on_the_latest_round(self):
        tasks = _round("2026-10-22T17:00:00Z", ["succeeded"] * 5 + ["cancelled"] * 3)
        tasks += _round("2026-10-22T18:24:45Z", ["succeeded"] * 5 + ["running", "pending", "pending"])

        summary = summarize_workflow(_workflow(tasks))

        self.assertEqual(summary["label"], "WO-003")
        self.assertEqual(summary["pr_url"], "https://github.com/o/r/pull/19")
        self.assertEqual(summary["current_step"], "qa_gate")
        self.assertEqual((summary["state"], summary["kind"]), ("QA running", "running"))
        self.assertEqual(summary["revisions"], 1)
        self.assertEqual([step["step_name"] for step in summary["steps"]], [s for s, _ in STEPS])
        self.assertEqual(summary["steps"][0]["label"], "OpenCode")

    def test_reports_a_revision_in_progress(self):
        tasks = _round("2026-10-22T17:00:00Z", ["succeeded"] * 5 + ["cancelled"] * 3)
        tasks += _round("2026-10-22T18:28:55Z", ["running"] + ["pending"] * 7)
        self.assertEqual(summarize_workflow(_workflow(tasks))["state"], "OpenCode revising (revision 1)")

    def test_reports_a_branch_refresh(self):
        tasks = _round("2026-10-22T17:00:00Z", ["succeeded"] * 5 + ["cancelled"] * 3)
        tasks += _round(
            "2026-10-22T18:24:46Z",
            ["running"] + ["pending"] * 7,
            inputs={"deliver_work_order": {"refresh": True}},
        )
        self.assertEqual(summarize_workflow(_workflow(tasks))["state"], "OpenCode refreshing the branch")

    def test_reports_ready_to_merge(self):
        tasks = _round(
            "2026-10-22T17:00:00Z",
            ["succeeded"] * 7 + ["blocked"],
            errors={"await_human_merge": "awaiting_external:github.merge"},
        )
        summary = summarize_workflow(_workflow(tasks))
        self.assertEqual((summary["state"], summary["kind"]), ("Ready to merge", "waiting"))

    def test_reports_a_step_waiting_for_a_human_decision(self):
        tasks = _round(
            "2026-10-22T17:31:32Z",
            ["succeeded"] * 4 + ["blocked"] + ["pending"] * 3,
            errors={"finalize_code_review": "barrier_blocked: code_review_blocked: 1 blocking findings"},
            inputs={"finalize_code_review": {"_awaiting_human_decision": {"reason": "revision_limit"}}},
        )
        summary = summarize_workflow(_workflow(tasks))
        self.assertEqual(summary["state"], "Stuck at code review: waiting for your decision")
        self.assertEqual(summary["kind"], "stuck")
        self.assertEqual(summary["stuck_task_id"], "finalize_code_review-2026-10-22T17:31:32Z")

    def test_reports_merged_when_the_workflow_succeeded(self):
        summary = summarize_workflow(_workflow(_round("t", ["succeeded"] * 8), status="succeeded"))
        self.assertEqual((summary["state"], summary["kind"]), ("Merged", "done"))

    def test_reports_a_stopped_work_order(self):
        tasks = _round(
            "t",
            ["succeeded"] * 4 + ["blocked"] + ["pending"] * 3,
            errors={"finalize_code_review": "stopped_by_human: ana@example.com: code_review_blocked"},
        )
        summary = summarize_workflow(_workflow(tasks))
        self.assertEqual((summary["state"], summary["kind"]), ("Stopped by ana@example.com", "failed"))


class SummarizeStageWorkflowTests(unittest.TestCase):
    def test_a_stage_without_a_coding_step_uses_plain_step_names(self):
        tasks = [
            {"id": "f", "step_name": "fork_wave", "agent_id": "orchestrator", "status": "succeeded", "created_at": "1"},
            {
                "id": "j",
                "step_name": "join_wave",
                "agent_id": "orchestrator",
                "status": "blocked",
                "error": "awaiting_external:workflow.child_terminal",
                "created_at": "1",
            },
        ]
        summary = summarize_workflow({"id": "wf-impl", "status": "running", "workflow_type": "implement_delivery", "tasks": tasks})

        self.assertEqual(summary["label"], "implement delivery")
        self.assertEqual(summary["state"], "Waiting for the workflows it started")
        self.assertEqual([s["label"] for s in summary["steps"]], ["fork wave", "join wave"])
        self.assertEqual(summary["revisions"], 0)

    def test_a_pending_approval_is_the_current_step(self):
        tasks = [
            {"id": "d", "step_name": "draft_prd", "agent_id": "pm-agent", "status": "succeeded", "created_at": "1"},
            {
                "id": "a",
                "step_name": "approve_prd",
                "agent_id": "orchestrator",
                "status": "blocked",
                "approval_required": True,
                "approval_status": "pending",
                "created_at": "2",
            },
        ]
        summary = summarize_workflow({"id": "wf-prd", "status": "running", "tasks": tasks})

        self.assertEqual((summary["state"], summary["kind"]), ("Waiting for approval: approve prd", "waiting"))
        self.assertTrue(summary["steps"][1]["approval_pending"])
        self.assertFalse(summary["steps"][0]["approval_pending"])

    def test_a_later_gate_is_not_actionable_while_an_earlier_step_is_open(self):
        tasks = [
            {"id": "plan", "step_name": "plan_release", "agent_id": "orchestrator", "status": "succeeded", "created_at": "1"},
            {
                "id": "ci",
                "step_name": "await_ci_on_sha",
                "agent_id": "orchestrator",
                "status": "blocked",
                "error": "awaiting_external:github.pr_checks",
                "created_at": "2",
            },
            {
                "id": "staging",
                "step_name": "approve_promote_staging",
                "agent_id": "orchestrator",
                "status": "pending",
                "approval_required": True,
                "approval_status": "pending",
                "created_at": "3",
            },
            {
                "id": "production",
                "step_name": "approve_promote_production",
                "agent_id": "orchestrator",
                "status": "pending",
                "approval_required": True,
                "approval_status": "pending",
                "created_at": "4",
            },
        ]
        summary = summarize_workflow({"id": "wf-rel", "status": "running", "workflow_type": "deploy_release", "tasks": tasks})

        self.assertEqual(summary["current_step"], "await_ci_on_sha")
        self.assertEqual(summary["state"], "Waiting for PR checks")
        self.assertFalse(any(step["approval_pending"] for step in summary["steps"]))

    def test_only_the_ready_gate_offers_approval(self):
        tasks = [
            {"id": "ci", "step_name": "await_ci_on_sha", "agent_id": "orchestrator", "status": "succeeded", "created_at": "1"},
            {
                "id": "staging",
                "step_name": "approve_promote_staging",
                "agent_id": "orchestrator",
                "status": "pending",
                "approval_required": True,
                "approval_status": "pending",
                "created_at": "2",
            },
            {
                "id": "production",
                "step_name": "approve_promote_production",
                "agent_id": "orchestrator",
                "status": "pending",
                "approval_required": True,
                "approval_status": "pending",
                "created_at": "3",
            },
        ]
        summary = summarize_workflow({"id": "wf-rel", "status": "running", "workflow_type": "deploy_release", "tasks": tasks})

        pending = [step["step_name"] for step in summary["steps"] if step["approval_pending"]]
        self.assertEqual(pending, ["approve_promote_staging"])
        self.assertEqual(summary["state"], "Waiting for approval: approve promote staging")


class _Memory:
    def __init__(self):
        self.status = {
            "current_phase": "implement",
            "phases": {
                "prd": {"state": "succeeded", "workflow_id": "wf-prd"},
                "implement": {"state": "running", "workflow_id": "wf-impl"},
                "release": {"state": "not_started"},
            },
        }
        self.children = {
            "wf-impl": [{"id": "wf-3"}, {"id": "wf-1"}, {"id": "wf-old", "status": "superseded"}],
            "wf-prd": [{"id": "wf-impl"}],
        }
        self.workflows = {
            "wf-impl": {"workflow": {"id": "wf-impl", "status": "running", "workflow_type": "implement_delivery"}, "tasks": []},
            "wf-1": {"workflow": {"id": "wf-1", "status": "succeeded"}, "tasks": _round("t1", ["succeeded"] * 8, work_order="WO-001")},
            "wf-3": {"workflow": {"id": "wf-3", "status": "running"}, "tasks": _round("t1", ["running"] + ["pending"] * 7)},
        }

    def get_program_status(self, program_id):
        return self.status

    def get_workflow_children(self, workflow_id):
        return self.children.get(workflow_id, [])

    def get_workflow(self, workflow_id):
        return self.workflows[workflow_id]


class PhaseActivityTests(unittest.TestCase):
    def test_lists_the_stage_workflow_and_only_its_own_children(self):
        result = phase_activity(_Memory(), "program-1", "implement")

        self.assertEqual((result["phase"], result["state"], result["workflow_id"]), ("implement", "running", "wf-impl"))
        self.assertEqual(result["workflow"]["label"], "implement delivery")
        self.assertEqual([item["label"] for item in result["children"]], ["WO-001", "WO-003"])
        self.assertEqual(result["children"][0]["state"], "Merged")
        self.assertEqual(result["children"][1]["state"], "OpenCode implementing")

    def test_a_stage_that_handed_off_is_done_and_does_not_list_the_next_stage(self):
        memory = _Memory()
        memory.workflows["wf-prd"] = {
            "workflow": {"id": "wf-prd", "status": "superseded"},
            "tasks": [{"id": "p", "step_name": "plan_work_orders", "agent_id": "pm-agent", "status": "succeeded", "created_at": "1"}],
        }

        result = phase_activity(memory, "program-1", "prd")

        self.assertEqual((result["workflow"]["state"], result["workflow"]["kind"]), ("Done", "done"))
        self.assertEqual(result["children"], [])

    def test_a_parked_coding_job_reads_as_opencode_running_with_its_output(self):
        memory = _Memory()
        memory.workflows["wf-3"]["tasks"] = _round(
            "t1", ["blocked"] + ["pending"] * 7, errors={"deliver_work_order": "awaiting_external:coding.finished"}
        )
        memory.list_coding_jobs = lambda **kw: [
            {"id": "job-1", "status": "running", "last_output": "running tests", "started_at": "2026-10-22T18:00:00Z"}
        ] if kw.get("task_id") == "deliver_work_order-t1" else []

        child = phase_activity(memory, "program-1", "implement")["children"][1]

        self.assertEqual((child["state"], child["kind"]), ("OpenCode implementing", "running"))
        self.assertEqual(child["coding_job"]["last_output"], "running tests")
        self.assertEqual(child["coding_job"]["started_at"], "2026-10-22T18:00:00Z")
        self.assertIsNone(phase_activity(memory, "program-1", "implement")["children"][0]["coding_job"])

    def test_a_queued_coding_job_waits_for_a_worker(self):
        memory = _Memory()
        memory.workflows["wf-3"]["tasks"] = _round(
            "t1", ["blocked"] + ["pending"] * 7, errors={"deliver_work_order": "awaiting_external:coding.finished"}
        )
        memory.list_coding_jobs = lambda **kw: [{"id": "job-1", "status": "queued", "created_at": "2026-10-22T18:00:00Z"}]

        child = phase_activity(memory, "program-1", "implement")["children"][1]

        self.assertEqual((child["state"], child["kind"]), ("OpenCode queued", "waiting"))

    def test_a_stage_that_has_not_started_has_no_workflow(self):
        result = phase_activity(_Memory(), "program-1", "release")
        self.assertEqual(result, {"phase": "release", "state": "not_started", "workflow_id": "", "workflow": None, "children": []})


if __name__ == "__main__":
    unittest.main()
