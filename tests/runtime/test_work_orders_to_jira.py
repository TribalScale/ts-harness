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
