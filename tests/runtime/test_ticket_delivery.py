"""prepare_ticket_delivery: a Jira ticket gets design references and the onboarding check."""

import unittest
from unittest import mock

from ts_harness.jira.ticket_delivery import prepare_ticket_delivery
from ts_harness.workflows.repo_onboarding_gate import Inspection

PROGRAM_REFS = [
    {"id": "D1", "title": "List", "object_key": "design/t/a.png", "mime": "image/png"},
    {"id": "D2", "title": "Drawer", "object_key": "design/t/b.png", "mime": "image/png"},
]


class _Store:
    def __init__(self):
        self.objects = {}

    def head(self, key):
        return {} if key in self.objects else None

    def put(self, key, data):
        self.objects[key] = data


class _Jira:
    def __init__(self, attachments):
        self.attachments = attachments

    def image_attachments(self, key):
        return [{"id": str(i), "filename": f"shot-{i}.png", "mime": "image/png", "size": 10} for i, _ in enumerate(self.attachments)]

    def attachment_content(self, attachment_id):
        return self.attachments[int(attachment_id)]


def _memory(*, own_refs=None, parent_refs=None):
    memory = mock.Mock()
    memory.task_program.return_value = {"program_id": "jira-prog", "workflow_id": "wf-1"}
    statuses = {"jira-prog": {"artifacts": {"design_refs": own_refs} if own_refs else {}}, "prd-prog": {"artifacts": {"design_refs": parent_refs or []}}}
    memory.get_program_status.side_effect = lambda pid: statuses[pid]
    memory.get_program.side_effect = lambda pid: {"program": {"id": pid, "parent_program_id": "prd-prog" if pid == "jira-prog" else None}}
    return memory


TASK = {
    "id": "prep-1",
    "workflow_id": "wf-1",
    "tenant_id": "acme",
    "input": {
        "work_order_body": "# DISC-12 Category filter\n\nDesign: D1",
        "repo_url": "https://github.com/acme/web",
        "base_branch": "fe-dev",
        "jira_issue_key": "DISC-12",
    },
}


def _not_needed(target, wo_id):
    return Inspection(False, "already has .ts-harness.yml")


class PrepareTicketDeliveryTests(unittest.TestCase):
    def test_design_refs_come_from_the_program_the_ticket_points_at(self):
        out = prepare_ticket_delivery(
            TASK, {}, memory=_memory(parent_refs=PROGRAM_REFS), jira_factory=lambda: _Jira([]), store_factory=_Store,
            inspect=_not_needed,
        )
        self.assertEqual([ref["id"] for ref in out["design_refs"]], ["D1", "D2"])
        self.assertEqual(out["work_order_body"], TASK["input"]["work_order_body"])
        self.assertEqual(out["onboarding"], {"checked": True, "needed": False, "reason": "already has .ts-harness.yml"})
        self.assertEqual(out["warnings"], [])

    def test_image_attachments_become_named_references_after_the_programs(self):
        store = _Store()
        out = prepare_ticket_delivery(
            TASK, {}, memory=_memory(parent_refs=PROGRAM_REFS), jira_factory=lambda: _Jira([b"png-bytes"]),
            store_factory=lambda: store, inspect=_not_needed,
        )
        self.assertEqual([ref["id"] for ref in out["design_refs"]], ["D1", "D2", "D3"])
        self.assertEqual(out["design_refs"][2]["source"], "jira")
        self.assertIn(out["design_refs"][2]["object_key"], store.objects)
        # The ticket named D1; the attachment is added to its Design line.
        self.assertTrue(out["work_order_body"].endswith("\n\nDesign: D1, D3"))
        self.assertEqual(out["work_order_body"].count("Design:"), 1)

    def test_a_repository_without_harness_config_is_set_up_in_the_same_pr(self):
        def needed(target, wo_id):
            self.assertEqual((target.repo_url, target.base_branch, wo_id), ("https://github.com/acme/web", "fe-dev", "WO-0000"))
            return Inspection(True, "CI runs an e2e suite and .ts-harness.yml is missing", "# WO-0000 Onboard\n\nWrite .ts-harness.yml")

        out = prepare_ticket_delivery(
            TASK, {}, memory=_memory(), jira_factory=lambda: _Jira([]), store_factory=_Store, inspect=needed
        )
        self.assertTrue(out["onboarding"]["needed"])
        self.assertIn("## Repository setup (also in this PR)", out["work_order_body"])
        self.assertIn("Write .ts-harness.yml", out["work_order_body"])
        self.assertTrue(out["work_order_body"].startswith("# DISC-12 Category filter"))

    def test_lookups_that_fail_are_warnings_and_the_ticket_still_runs(self):
        memory = mock.Mock()
        memory.task_program.side_effect = RuntimeError("memory down")

        def boom(*_a):
            raise RuntimeError("clone failed")

        out = prepare_ticket_delivery(
            TASK, {}, memory=memory, jira_factory=mock.Mock(side_effect=RuntimeError("no Jira credentials")),
            store_factory=_Store, inspect=boom,
        )
        self.assertEqual(out["design_refs"], [])
        self.assertEqual(out["work_order_body"], TASK["input"]["work_order_body"])
        self.assertEqual(len(out["warnings"]), 3)

    def test_reads_the_ticket_from_hydrate_ticket_when_the_step_has_no_input(self):
        memory = _memory()
        memory.get_workflow.return_value = {
            "workflow": {"id": "wf-1", "input": {"jira_issue_key": "DISC-9"}},
            "tasks": [
                {"step_name": "hydrate_ticket", "status": "succeeded", "updated_at": "2026-11-17",
                 "output": {"work_order_body": "body", "repo_url": "https://github.com/acme/web", "base_branch": "dev"}},
            ],
        }
        seen = {}

        def inspect(target, wo_id):
            seen["target"] = target
            return Inspection(False, "no e2e or integration suite in CI")

        jira = mock.Mock()
        jira.image_attachments.return_value = []
        out = prepare_ticket_delivery(
            {"id": "prep-2", "workflow_id": "wf-1", "input": {}}, {}, memory=memory, jira_factory=lambda: jira,
            store_factory=_Store, inspect=inspect,
        )
        self.assertEqual(out["work_order_body"], "body")
        self.assertEqual(seen["target"].base_branch, "dev")
        jira.image_attachments.assert_called_once_with("DISC-9")


if __name__ == "__main__":
    unittest.main()
