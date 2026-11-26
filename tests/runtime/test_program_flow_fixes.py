"""Program flow fixes found running real requests: plan hand-off, notes, endpoint check, tickets."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from ts_harness.implementation.work_order_context import _changed_endpoints, api_contract_problems
from ts_harness.workflows import program_adapters

ROOT = Path(__file__).resolve().parents[2]
REPOS = [{"full_name": "org/api", "role": "api"}, {"full_name": "org/web", "role": "web"}]


class EndpointCheckTests(unittest.TestCase):
    TEMPLATE_WO = """# WO-001 Adopt the node-api template

Repository: org/api

## Requirements
- Add the CI-only test issuer and `npm run start:smoke`.

## E2E Acceptance Tests
- Given smoke mode, When `GET /me` carries a test token, Then the API responds 200.
- Given smoke mode, When `GET /schedule/week` is called, Then it returns rows.
"""

    def test_endpoints_called_in_acceptance_tests_are_not_changes(self):
        self.assertEqual(_changed_endpoints(self.TEMPLATE_WO), set())
        self.assertEqual(api_contract_problems(self.TEMPLATE_WO, REPOS), [])

    def test_gherkin_lines_outside_a_test_heading_are_not_changes(self):
        body = "# WO-002 Thing\n\nRepository: org/api\n\n## Requirements\n- Given a planner, When `GET /tray` loads, Then it lists.\n"
        self.assertEqual(_changed_endpoints(body), set())

    def test_an_explicit_no_endpoint_line_wins(self):
        body = "# WO-003 Thing\n\nRepository: org/api\n\n## Requirements\n- Endpoints: none added or changed.\n- Calls `GET /me`.\n"
        self.assertEqual(_changed_endpoints(body), set())

    def test_a_real_endpoint_change_still_needs_openapi(self):
        body = (
            "# WO-004 Lane step\n\nRepository: org/api\n\n## Requirements\n"
            "- `GET /schedule/week` returns the step to place on each lane order.\n"
        )
        problems = api_contract_problems(body, REPOS)
        self.assertEqual(len(problems), 1)
        self.assertIn("openapi.json", problems[0])


def _task(step, output, *, approval=False, at="1"):
    return {"step_name": step, "status": "succeeded", "output": output, "approval_required": approval, "created_at": at}


class PlanHandOffTests(unittest.TestCase):
    PRODUCES = ["approved_prd", "repo", "default_branch", "design_refs", "screen_specs_text", "prd_approval_notes"]
    REFS = [{"id": "D1", "title": "Week", "object_key": "design/a.png"}]

    def extract(self, tasks):
        return program_adapters._extract_declared_outputs(
            workflow_type="content_to_plan", tasks=tasks, produces_artifacts=self.PRODUCES
        )

    def test_a_plan_nested_under_artifacts_is_still_the_approved_prd(self):
        tasks = [_task("draft_plan", {"summary": "s", "artifacts": {"prd_markdown": "# Plan: X\n\nBody"}})]
        self.assertEqual(self.extract(tasks)["approved_prd"], "# Plan: X\n\nBody")

    def test_design_refs_and_screen_specs_follow_the_plan(self):
        tasks = [
            _task("gather_context", {"design_refs": self.REFS, "screen_specs_text": "SC1 Week"}),
            _task("draft_plan", {"prd_markdown": "# Plan: X"}),
        ]
        patch = self.extract(tasks)
        self.assertEqual(patch["design_refs"], self.REFS)
        self.assertEqual(patch["screen_specs_text"], "SC1 Week")

    def test_notes_left_when_approving_reach_the_next_phase(self):
        tasks = [
            _task("draft_plan", {"prd_markdown": "# Plan: X"}, at="1"),
            _task("approve_plan", {"summary": "Approved with notes", "reviewer_notes": "Apply: T8 returns the reason."}, approval=True, at="2"),
        ]
        patch = self.extract(tasks)
        self.assertEqual(patch["prd_approval_notes"], "Apply: T8 returns the reason.")
        intake = program_adapters._build_work_orders_inputs(summary="s", artifacts=patch)
        self.assertEqual(intake["prd_approval_notes"], "Apply: T8 returns the reason.")

    def test_the_latest_approval_notes_win(self):
        tasks = [
            _task("approve_plan", {"reviewer_notes": "old"}, approval=True, at="1"),
            _task("approve_plan", {"reviewer_notes": "new"}, approval=True, at="2"),
        ]
        self.assertEqual(program_adapters._approval_notes(tasks), "new")

    def test_prd_outputs_carry_approval_notes_too(self):
        tasks = [
            _task("draft_prd", {"prd": {"title": "P"}}),
            _task("approve_prd", {"approved_prd": {"title": "P"}, "reviewer_notes": "Use one row per mill."}, approval=True),
        ]
        patch = program_adapters._extract_prd_outputs(workflow_type="prd_request", tasks=tasks)
        self.assertEqual(patch["prd_approval_notes"], "Use one row per mill.")

    def test_the_content_program_binds_frames_specs_and_notes_into_work_orders(self):
        program = json.loads((ROOT / "config/programs/content_to_jira_work_orders.json").read_text())
        nodes = {n["id"]: n for n in program["nodes"]}
        bound = {b.get("name") for b in nodes["work_orders"]["input_bindings"]}
        self.assertTrue({"design_refs", "screen_specs_text", "prd_approval_notes"} <= bound)
        self.assertIn("design_refs", {b.get("name") for b in nodes["file_jira"]["input_bindings"]})

    def test_the_refinery_and_story_writer_receive_the_notes(self):
        for name, step in (("work_orders_from_prd.json", "refine_requirements"), ("jira_from_prd.json", "plan_stories")):
            stages = {s["name"]: s for s in json.loads((ROOT / "config/workflows" / name).read_text())["stages"]}
            self.assertEqual(stages[step]["input_map"]["prd_approval_notes"], "$workflow.prd_approval_notes")


# --- tickets ------------------------------------------------------------------------------------

from ts_harness.jira.prepare_ticket_queue import explicit_ticket_queue, is_epic  # noqa: E402
from ts_harness.jira.ticket_queue import named_ticket_key, ticket_request, wants_explicit_queue  # noqa: E402
from ts_harness.tools.workflow_tools import _jira_entry  # noqa: E402


def issue(key, kind="Story", blocked_by=(), done=False):
    links = [
        {
            "type": {"name": "Blocks", "inward": "is blocked by"},
            "inwardIssue": {"key": b, "fields": {"status": {"statusCategory": {"key": "new"}}}},
        }
        for b in blocked_by
    ]
    status = {"name": "Done" if done else "To Do", "statusCategory": {"key": "done" if done else "new"}}
    return {"key": key, "fields": {"issuetype": {"name": kind}, "status": status, "issuelinks": links, "summary": key}}


class FakeJira:
    def __init__(self, issues, children):
        self.issues = {i["key"]: i for i in issues}
        self.children = children

    def search(self, jql, max_results=100, fields=None):
        if jql.startswith("key in ("):
            keys = [k.strip() for k in jql[len("key in (") : jql.index(")")].split(",")]
            return [self.issues[k] for k in keys if k in self.issues]
        if jql.startswith("key = "):
            key = jql.split()[2]
            return [self.issues[key]] if key in self.issues else []
        if jql.startswith("parent = "):
            return [self.issues[k] for k in self.children.get(jql.split()[2], [])]
        return []


class TicketRequestTests(unittest.TestCase):
    def test_epics_exclusions_and_a_second_part(self):
        req = ticket_request("Implement the tickets in epic PROJ-33 except PROJ-45, and the tickets in epic PROJ-47.")
        self.assertEqual(req, {"included": ["PROJ-33", "PROJ-47"], "excluded": ["PROJ-45"], "epic": True})

    def test_short_number_lists(self):
        req = ticket_request("Implement PROJ-33 epic tickets (excluding PROJ-45): PROJ-34, 35, 36 and 46")
        self.assertEqual(req["included"], ["PROJ-33", "PROJ-34", "PROJ-35", "PROJ-36", "PROJ-46"])
        self.assertEqual(req["excluded"], ["PROJ-45"])

    def test_a_negated_key_is_never_the_one_ticket(self):
        text = "Implement PROJ-34 and PROJ-35. Do not implement the epics PROJ-33 or PROJ-47, and do not implement PROJ-45."
        self.assertEqual(ticket_request(text)["excluded"], ["PROJ-33", "PROJ-47", "PROJ-45"])
        self.assertEqual(named_ticket_key(text), "")
        self.assertTrue(wants_explicit_queue(text))

    def test_one_key_is_still_one_ticket(self):
        self.assertEqual(named_ticket_key("implement PROJ-12"), "PROJ-12")
        self.assertFalse(wants_explicit_queue("implement PROJ-12"))
        self.assertFalse(wants_explicit_queue("implement all PROJ"))


class ExplicitQueueTests(unittest.TestCase):
    def setUp(self):
        self.jira = FakeJira(
            [
                issue("PROJ-33", "Epic"),
                issue("PROJ-47", "Epic"),
                issue("PROJ-41"),
                issue("PROJ-44", blocked_by=["PROJ-41"]),
                issue("PROJ-45"),
                issue("PROJ-46", done=True),
                issue("PROJ-50", blocked_by=["PROJ-41", "PROJ-44"]),
                issue("PROJ-51"),
            ],
            {"PROJ-33": ["PROJ-41", "PROJ-44", "PROJ-45", "PROJ-46"], "PROJ-47": ["PROJ-50", "PROJ-51"]},
        )

    def test_epics_expand_minus_exclusions_done_and_epics(self):
        built = explicit_ticket_queue(
            "Implement the tickets in epic PROJ-33 except PROJ-45, and the tickets in epic PROJ-47.", self.jira.search
        )
        self.assertEqual(built["keys"], ["PROJ-41", "PROJ-44", "PROJ-50", "PROJ-51"])
        reasons = {item["key"]: item["reason"] for item in built["exclusions"]}
        self.assertEqual(reasons["PROJ-45"], "excluded in the request")
        self.assertEqual(reasons["PROJ-46"], "already done")
        self.assertEqual(built["projects"], ["PROJ"])

    def test_a_blocker_left_out_keeps_its_dependents_out(self):
        built = explicit_ticket_queue("Implement PROJ-44 and PROJ-50, not PROJ-41", self.jira.search)
        self.assertEqual(built["keys"], [])
        reasons = {item["key"]: item["reason"] for item in built["exclusions"]}
        self.assertIn("blocked by PROJ-41", reasons["PROJ-44"])

    def test_is_epic(self):
        self.assertTrue(is_epic(issue("PROJ-33", "Epic")))
        self.assertFalse(is_epic(issue("PROJ-34")))


class DeliveryRefusesEpicsTests(unittest.TestCase):
    def test_an_epic_is_never_coded(self):
        from ts_harness.jira import ticket_delivery

        self.assertIn("is an epic, not a ticket", Path(ticket_delivery.__file__).read_text())


class JiraEntryTests(unittest.TestCase):
    def test_a_list_or_an_epic_starts_the_queue(self):
        entry, artifacts = _jira_entry(
            "jira_to_pr",
            "implement",
            {"jira_issue_key": "PROJ-45"},
            "Implement these Jira tickets: PROJ-34, PROJ-35. Do not implement PROJ-45.",
        )
        self.assertEqual((entry, artifacts), ("pick", {}))

    def test_one_ticket_keeps_its_entry(self):
        self.assertEqual(
            _jira_entry("jira_to_pr", "implement", {"jira_issue_key": "PROJ-12"}, "implement PROJ-12"),
            ("implement", {"jira_issue_key": "PROJ-12"}),
        )
        self.assertEqual(_jira_entry("software_delivery", "intake", {}, "PROJ-1 and PROJ-2"), ("intake", {}))

    def test_the_routing_override_agrees(self):
        from ts_harness.routing.matcher import _with_named_ticket

        class Rule:
            program_type = "jira_to_pr"
            id = "jira_to_pr"

        class Intent:
            entry_phase = "pick"
            artifacts: dict = {}

        intent = _with_named_ticket(Rule(), Intent(), "implement the tickets in epic proj-33 except proj-45")
        self.assertEqual((intent.entry_phase, intent.artifacts), ("pick", {}))
        single = _with_named_ticket(Rule(), Intent(), "implement proj-12")
        self.assertEqual((single.entry_phase, single.artifacts), ("implement", {"jira_issue_key": "PROJ-12"}))


# --- stuck steps -------------------------------------------------------------------------------

from ts_harness.agent.loop import EnterpriseAgent  # noqa: E402
from ts_harness.workflows.human_decisions import output_shape_problem  # noqa: E402
from ts_harness.workflows.promotion import build_binding_key  # noqa: E402


class StuckStepTests(unittest.TestCase):
    CONTRACT = {
        "output_schema": {
            "type": "object",
            "anyOf": [
                {"required": ["prd_markdown"]},
                {"required": ["result"], "properties": {"result": {"type": "object", "required": ["prd_markdown"]}}},
            ],
        }
    }

    def test_accept_refuses_output_with_the_wrong_shape(self):
        nested = {"summary": "s", "artifacts": {"prd_markdown": "# Plan"}}
        self.assertTrue(output_shape_problem({"contract": self.CONTRACT}, nested))
        self.assertEqual(output_shape_problem({"contract": self.CONTRACT}, {"prd_markdown": "# Plan"}), "")
        self.assertEqual(output_shape_problem({"contract": {"semantic_validators": ["x"]}}, nested), "")
        self.assertEqual(output_shape_problem({}, nested), "")

    def test_retry_guidance_reaches_the_agent_prompt(self):
        prompt = EnterpriseAgent._append_knowledge_notes(
            None, "Do the step.", {"input": {"human_guidance": "Guidance from a planner:\nWO-001 changes no endpoint."}}
        )
        self.assertIn("a person retried it with guidance", prompt)
        self.assertIn("WO-001 changes no endpoint.", prompt)
        self.assertEqual(EnterpriseAgent._append_knowledge_notes(None, "Do the step.", {"input": {}}), "Do the step.")

    def test_decide_and_chat_pass_guidance_on_retry(self):
        from ts_harness.gateway import channel_confirmations
        from ts_harness.workflows import human_decisions

        source = Path(human_decisions.__file__).read_text()
        retry = source[source.index('if action == "retry":') : source.index('if action == "accept":')]
        self.assertIn("HUMAN_GUIDANCE_KEY", retry)
        self.assertIn('action in {"revise", "retry"}', Path(channel_confirmations.__file__).read_text())


class BindingKeyTests(unittest.TestCase):
    def test_the_web_prefix_is_added_once(self):
        self.assertEqual(build_binding_key("web", "web", "web:abc"), "web:web:abc")
        self.assertEqual(build_binding_key("web", "web", "web:web:web:web:abc"), "web:web:abc")
        self.assertEqual(build_binding_key("slack", "C1", "123.4"), "slack:C1:123.4")
        self.assertEqual(build_binding_key("slack", "C1", "slack:C1:123.4"), "slack:C1:123.4")


if __name__ == "__main__":
    unittest.main()
