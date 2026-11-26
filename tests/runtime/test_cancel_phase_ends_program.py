"""Cancelling a program's only running phase ends the program; a queue with tickets left keeps going."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from ts_harness.workflows.hooks import cancel_workflow


class FakeMemory:
    def __init__(self, workflow, program):
        self.workflow = workflow
        self.program = program
        self.cancelled_programs: list[tuple[str, str]] = []

    def cancel_workflow(self, workflow_id, *, actor, reason):
        self.workflow["status"] = "cancelled"
        return {"status": "cancelled", "workflow_id": workflow_id}

    def get_program(self, program_id):
        return self.program

    def cancel_program(self, program_id, *, actor, reason="", cascade=True):
        self.cancelled_programs.append((program_id, reason))
        return {"status": "cancelled", "program_id": program_id}


def run(memory):
    ctx = SimpleNamespace(
        memory_client=memory, workflow_id="wf-1", actor="sam", tenant_id="default",
        extras={"reason": "started on the wrong ticket"}, runner_event="", workflow=None, task=None,
    )
    with (
        mock.patch.object(cancel_workflow, "load_workflow_payload", return_value=(memory.workflow, [])),
        mock.patch.object(cancel_workflow, "get_session_store"),
    ):
        return cancel_workflow.handle(ctx)


def program(workflows, artifacts=None, status="running"):
    return {"program": {"id": "prog-1", "status": status, "metadata": {"artifacts": artifacts or {}}}, "workflows": workflows}


class CancelPhaseTests(unittest.TestCase):
    def test_the_only_running_phase_ends_the_program(self):
        memory = FakeMemory(
            {"id": "wf-1", "program_id": "prog-1", "source_channel": ""},
            program([{"id": "wf-1", "status": "cancelled", "program_phase": "implement"}]),
        )
        result = run(memory)
        self.assertEqual(memory.cancelled_programs[0][0], "prog-1")
        self.assertIn("started on the wrong ticket", memory.cancelled_programs[0][1])
        self.assertEqual(result["program"]["status"], "cancelled")

    def test_another_running_phase_keeps_the_program(self):
        memory = FakeMemory(
            {"id": "wf-1", "program_id": "prog-1", "source_channel": ""},
            program([{"id": "wf-1", "status": "cancelled"}, {"id": "wf-2", "status": "running"}]),
        )
        run(memory)
        self.assertEqual(memory.cancelled_programs, [])

    def test_a_queue_with_tickets_left_keeps_going(self):
        memory = FakeMemory(
            {"id": "wf-1", "program_id": "prog-1", "source_channel": ""},
            program(
                [{"id": "wf-1", "status": "cancelled"}],
                artifacts={"jira_issue_keys": ["PROJ-1", "PROJ-2", "PROJ-3"], "completed_jira_issue_keys": []},
            ),
        )
        run(memory)
        self.assertEqual(memory.cancelled_programs, [])

    def test_a_child_workflow_or_a_finished_program_is_left_alone(self):
        child = FakeMemory({"id": "wf-1", "program_id": "prog-1", "parent_workflow_id": "wf-0"}, program([]))
        run(child)
        self.assertEqual(child.cancelled_programs, [])
        done = FakeMemory({"id": "wf-1", "program_id": "prog-1"}, program([], status="cancelled"))
        run(done)
        self.assertEqual(done.cancelled_programs, [])


if __name__ == "__main__":
    unittest.main()
