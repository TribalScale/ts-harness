"""A finished work-order filing can be filed again ("refile the work orders in PAY")."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from ts_harness.jira.assign_filed import assign_filed_tickets
from ts_harness.workflows.program_adapters import build_default_adapter_registry
from ts_harness.workflows.program_materialize import (
    find_edge,
    resolve_bridge_candidates_from_text,
    may_end_offer,
    node_may_end,
    resolve_target_node,
)

ROOT = Path(__file__).resolve().parents[2]
PROGRAMS = ("software_delivery", "content_to_jira_work_orders", "bug_to_jira_work_orders")


def _snapshot(name: str) -> dict:
    return json.loads((ROOT / "config" / "programs" / f"{name}.json").read_text())


class RefileEdgeTests(unittest.TestCase):
    def test_filing_ends_the_program_but_can_run_again(self):
        for name in PROGRAMS:
            snapshot = _snapshot(name)
            self.assertTrue(node_may_end(snapshot, "file_jira"), name)
            self.assertEqual(resolve_target_node(snapshot, "file_jira", "file_jira"), "file_jira")
            edge = find_edge(snapshot, source="file_jira", target="file_jira")
            self.assertEqual(edge["artifact_patch"], {"jira_refile": True})
            self.assertTrue(edge["confirm_required"])
            matches = resolve_bridge_candidates_from_text("refile the work orders in PAY", snapshot=snapshot, source_node="file_jira")
            self.assertEqual([m["target_node"] for m in matches], ["file_jira"], name)
            self.assertIn('"refile the work orders"', may_end_offer(snapshot, "file_jira")["text"])


class RefileInputTests(unittest.TestCase):
    ARTIFACTS = {
        "work_order_ids": ["WO-001"], "work_orders_markdown": "## WO-001 Hello",
        "jira_epic_key": "OLD-1", "jira_story_keys": ["OLD-2", "OLD-3"],
    }

    def _inputs(self, artifacts):
        return build_default_adapter_registry().build_inputs(
            "extract_file_jira_outputs", summary="refile the work orders in PAY", artifacts=artifacts)

    def test_a_refile_files_a_new_epic_and_remembers_the_old_tickets(self):
        refile = self._inputs({**self.ARTIFACTS, "jira_refile": True})
        self.assertNotIn("jira_epic_key", refile)
        self.assertEqual(refile["previous_jira_keys"], ["OLD-1", "OLD-2", "OLD-3"])
        first = self._inputs(self.ARTIFACTS)
        self.assertEqual(first["jira_epic_key"], "OLD-1")
        self.assertNotIn("previous_jira_keys", first)


class RefileAssignTests(unittest.TestCase):
    def _tasks(self, previous):
        return [
            {"step_name": "choose_epic", "status": "succeeded",
             "input": {"assignee_account_id": "acct-9", "jira_project_key": "PAY", "previous_jira_keys": previous,
                       "jira_allowed_projects": ["PAY", "OLD"]}},
            {"step_name": "file_tickets", "status": "succeeded", "updated_at": "2", "input": {"jira_project_key": "PAY"},
             "output": {"summary": "Filed.", "artifacts": {"jira_epic_key": "PAY-1", "jira_story_keys": ["PAY-2"]}}},
        ]

    def test_replaced_tickets_get_a_pointer_and_deleted_ones_are_skipped(self):
        memory = mock.Mock()
        memory.get_workflow.return_value = {"workflow": {"id": "wf"}, "tasks": self._tasks(["OLD-1", "OLD-2", "AD-1"])}
        jira = mock.Mock()
        jira.comment.side_effect = lambda key, text: (_ for _ in ()).throw(RuntimeError("404")) if key == "OLD-2" else None
        out = assign_filed_tickets({"workflow_id": "wf"}, {}, memory=memory, jira_factory=lambda: jira)
        self.assertEqual(out["assigned"], ["PAY-2"])
        self.assertEqual(out["replaced"], ["OLD-1"])
        commented = [call.args[0] for call in jira.comment.call_args_list]
        self.assertEqual(commented, ["OLD-1", "OLD-2"])  # AD-1 is outside the allowed projects
        self.assertIn("Refiled as PAY-1", jira.comment.call_args_list[0].args[1])


if __name__ == "__main__":
    unittest.main()


class RefileChatTests(unittest.TestCase):
    """A thread linked to the finished filing routes "refile the work orders" to the program."""

    def test_an_older_session_without_a_program_type_still_routes_the_refile(self):
        from ts_harness.routing.intents import classify_channel_intent

        snapshot = _snapshot("software_delivery")
        program = {"id": "prog-1", "program_type": "software_delivery", "current_phase": "file_jira",
                   "status": "succeeded", "program_definition_snapshot": snapshot,
                   "metadata": {"artifacts": {"ended_at_phase": "file_jira", "planning_track": "work_orders"}}}
        metadata = {"mode": "bridge_linked", "workflow_id": "wf-1", "workflow_type": "work_orders_from_prd",
                    "workflow_phase": "approved", "program_id": "prog-1", "program_type": None, "program_phase": "file_jira"}
        client = mock.Mock()
        client.get_program.return_value = {"program": program}
        with mock.patch("ts_harness.memory.client.get_memory_client", return_value=client):
            intent = classify_channel_intent("refile the work orders in PAY", metadata)
            other = classify_channel_intent("what is the status?", metadata)
        self.assertEqual((intent.kind, intent.entry_phase), ("program_advance", "file_jira"))
        self.assertNotEqual(other.kind, "program_advance")
