"""PRD -> work orders -> Jira: the program route, signal choice, filing inputs and refs."""

import json
import re
import unittest
from pathlib import Path
from unittest import mock

from ts_harness.workflows import program_adapters
from ts_harness.workflows.expressions import resolve_input_map
from ts_harness.workflows.program_materialize import automatic_edges_from, resolve_bridge_candidates_from_text

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = json.loads((ROOT / "config/programs/software_delivery.json").read_text())


class RouteTests(unittest.TestCase):
    def test_planning_for_jira_files_the_work_orders_automatically(self):
        nodes = {n["id"]: n for n in SNAPSHOT["nodes"]}
        self.assertEqual(nodes["work_orders_jira"]["default_workflow_type"], "work_orders_from_prd")
        self.assertEqual(nodes["file_jira"]["default_workflow_type"], "file_work_orders_as_jira")
        self.assertEqual([e["target"] for e in automatic_edges_from(SNAPSHOT, "work_orders_jira")], ["file_jira"])
        # The plain route still goes straight to implementation.
        self.assertEqual([e["target"] for e in automatic_edges_from(SNAPSHOT, "work_orders")], ["implement"])

    def test_asking_for_jira_picks_that_route_over_the_plainer_signals(self):
        for text in ("file work orders in jira", "Please plan and file in Jira", "track in jira"):
            found = resolve_bridge_candidates_from_text(text, snapshot=SNAPSHOT, source_node="prd")
            self.assertEqual([c["target_node"] for c in found], ["work_orders_jira"], text)
            self.assertTrue(found[0]["artifact_patch"]["assign_jira_to_requester"])
        plain = resolve_bridge_candidates_from_text("start work orders", snapshot=SNAPSHOT, source_node="prd")
        self.assertEqual([c["target_node"] for c in plain], ["work_orders"])
        stories = resolve_bridge_candidates_from_text("create jira", snapshot=SNAPSHOT, source_node="prd")
        self.assertEqual([c["target_node"] for c in stories], ["jira"])


class ContinuationTests(unittest.TestCase):
    def test_a_continuation_by_workflow_type_follows_the_wording(self):
        # The orchestrator names the workflow (work_orders_from_prd), which two nodes run.
        from ts_harness.tools.workflow_tools import _node_for_workflow_from

        node = _node_for_workflow_from(SNAPSHOT, "prd", "work_orders_from_prd", "file work orders in jira", "prd_request")
        self.assertEqual(node, "work_orders_jira")
        plain = _node_for_workflow_from(SNAPSHOT, "prd", "work_orders_from_prd", "start work orders", "prd_request")
        self.assertEqual(plain, "work_orders")
        unclear = _node_for_workflow_from(SNAPSHOT, "prd", "work_orders_from_prd", "go on", "prd_request")
        self.assertEqual(unclear, "work_orders")

    def test_the_offer_names_the_jira_route_plainly(self):
        from ts_harness.workflows.program_materialize import may_end_offer

        text = may_end_offer(SNAPSHOT, "prd")["text"]
        self.assertIn('"work orders in jira" for Work orders (Jira)', text)


class FilingInputTests(unittest.TestCase):
    def test_the_requester_is_the_assignee_on_this_route(self):
        artifacts = {
            "work_order_ids": ["WO-001"],
            "work_orders_markdown": "# WO-001 X",
            "project_repositories": [{"full_name": "acme/web", "default_branch": "dev", "role": "web"}],
            "assign_jira_to_requester": True,
            "requester_email": "pm@example.com",
        }
        with mock.patch.object(program_adapters, "_requester_jira_account", return_value="acct-9") as lookup:
            intake = program_adapters._build_file_jira_inputs(summary="s", artifacts=artifacts)
        lookup.assert_called_once_with("pm@example.com")
        self.assertEqual(intake["assignee_account_id"], "acct-9")
        self.assertEqual(intake["project_repositories"][0]["full_name"], "acme/web")
        with mock.patch.object(program_adapters, "_requester_jira_account") as lookup:
            plain = program_adapters._build_file_jira_inputs(summary="s", artifacts={**artifacts, "assign_jira_to_requester": False})
        lookup.assert_not_called()
        self.assertNotIn("assignee_account_id", plain)

    def test_file_tickets_receives_the_program_and_the_assignee(self):
        workflow = json.loads((ROOT / "config/workflows/file_work_orders_as_jira.json").read_text())
        stage = next(s for s in workflow["stages"] if s["name"] == "file_tickets")
        tasks = [{"step_name": "choose_epic", "status": "succeeded", "created_at": "1",
                  "input": {"root_program_id": "prog-1", "assignee_account_id": "acct-9",
                            "project_repositories": [{"full_name": "acme/web"}]},
                  "output": {"jira_project_key": "THD"}}]
        resolved = resolve_input_map(stage["input_map"], tasks=tasks, workflow={})
        self.assertEqual(resolved["root_program_id"], "prog-1")
        self.assertEqual(resolved["assignee_account_id"], "acct-9")
        self.assertEqual(resolved["project_repositories"], [{"full_name": "acme/web"}])


class WorkflowRefTests(unittest.TestCase):
    def test_every_workflow_ref_in_the_definitions_resolves(self):
        # An unknown `$workflow.<key>` silently resolves to nothing; the filing step lost the
        # program id that way.
        source = (ROOT / "runtime/ts_harness/workflows/expressions.py").read_text()
        known = set(re.findall(r'"workflow\.([a-z_]+)"', source))
        used = set()
        for path in list((ROOT / "config/workflows").glob("*.json")) + list((ROOT / "config/fragments").glob("*.json")):
            used |= set(re.findall(r'"\$workflow\.([a-z_]+)"', path.read_text()))
        self.assertEqual(sorted(used - known), [])


if __name__ == "__main__":
    unittest.main()


class OpeningGateTests(unittest.TestCase):
    def test_the_filing_gate_says_what_will_be_filed(self):
        from ts_harness.workflows.hooks.complete_program_phase import opening_gate_text

        text = opening_gate_text(
            "file_work_orders_as_jira",
            "choose_epic",
            {"work_order_ids": ["WO-001", "WO-002", "WO-003"], "jira_project_key": "THD", "assignee_account_id": "a"},
        )
        self.assertIn("Ready to file 3 Jira tickets in THD under a new epic, assigned to you", text)
        self.assertIn("Reply yes to file them", text)
        self.assertIn("under epic THD-9", opening_gate_text("file_work_orders_as_jira", "choose_epic", {"jira_epic_key": "THD-9"}))
        self.assertIn("(review plan) is waiting for you", opening_gate_text("x", "review_plan", {}))

    def test_an_automatic_advance_into_a_gate_announces_it(self):
        from ts_harness.workflows.hooks import complete_program_phase as cpp

        ctx = mock.Mock()
        ctx.memory_client.get_workflow_tasks.return_value = {"tasks": [
            {"step_name": "choose_epic", "workflow_type": "file_work_orders_as_jira", "approval_required": True,
             "approval_status": "pending", "depends_on": [], "input": {"work_order_ids": ["WO-001"], "jira_project_key": "THD"}},
            {"step_name": "file_tickets", "approval_required": False, "depends_on": ["x"]},
        ]}
        with mock.patch.object(cpp, "_notify_channel") as notify:
            cpp._announce_opening_gate(ctx, {"id": "wf-old", "source_channel": "web:web:t"}, {"workflow_id": "wf-new"})
        workflow, text = notify.call_args.args[1], notify.call_args.args[2]
        self.assertEqual(workflow["id"], "wf-new")
        self.assertIn("Ready to file 1 Jira ticket in THD", text)
        ctx.memory_client.get_workflow_tasks.return_value = {"tasks": [{"step_name": "plan", "approval_required": False}]}
        with mock.patch.object(cpp, "_notify_channel") as notify:
            cpp._announce_opening_gate(ctx, {}, {"workflow_id": "wf-2"})
        notify.assert_not_called()


class RequesterEmailTests(unittest.TestCase):
    def test_a_promoted_program_keeps_the_requesters_email(self):
        from types import SimpleNamespace

        from ts_harness.workflows.hooks.promote import _with_requester_email

        session = SimpleNamespace(metadata={"actor_email": "pm@example.com"})
        self.assertEqual(_with_requester_email({"x": 1}, session), {"x": 1, "requester_email": "pm@example.com"})
        self.assertIsNone(_with_requester_email(None, SimpleNamespace(metadata={})))

    def test_a_registered_program_keeps_the_requesters_email(self):
        from types import SimpleNamespace

        from ts_harness.workflows.hooks.register_program import register_on_promote

        memory = mock.Mock()
        session = SimpleNamespace(metadata={"actor_email": "pm@example.com"})
        skeleton = SimpleNamespace(program={"phase": "prd"})
        with mock.patch("ts_harness.workflows.hooks.register_program.program_pin_fields", return_value={}):
            register_on_promote(
                skeleton=skeleton, session=session, workflow_id="wf-1", summary="s", binding_key="web:web:t",
                tenant_id="default", actor="a", memory_client=memory,
            )
        self.assertEqual(memory.create_program.call_args.kwargs["artifacts"], {"requester_email": "pm@example.com"})


class ChatRelinkTests(unittest.TestCase):
    def test_a_chat_on_a_finished_workflow_follows_the_programs_active_one(self):
        from ts_harness.workflows.session_compat import refreshed_program_link

        memory = mock.Mock()
        memory.get_program_status.return_value = {"active_workflow_id": "wf-file", "current_phase": "file_jira"}
        memory.get_workflow.return_value = {"workflow": {"id": "wf-plan", "status": "superseded"}}
        meta = {"program_id": "prog-1", "workflow_id": "wf-plan", "program_phase": "work_orders_jira"}
        updated = refreshed_program_link(meta, memory)
        self.assertEqual((updated["workflow_id"], updated["active_workflow_id"]), ("wf-file", "wf-file"))

        memory.get_workflow.return_value = {"workflow": {"id": "wf-plan", "status": "running"}}
        self.assertIsNone(refreshed_program_link(meta, memory))
        memory.get_program_status.return_value = {"active_workflow_id": "wf-plan"}
        self.assertIsNone(refreshed_program_link(meta, memory))
        self.assertIsNone(refreshed_program_link({"workflow_id": "wf-x"}, memory))
