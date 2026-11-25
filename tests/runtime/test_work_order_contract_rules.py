"""Planner rules: deferred work gets its own work order, and the API contract comes first."""

from __future__ import annotations

import unittest
from unittest import mock

from ts_harness.implementation.work_order_context import api_contract_problems, deferred_work_problems
from ts_harness.workflows import stage_contracts

API = {"full_name": "org/api", "role": "api"}
WEB = {"full_name": "org/web", "role": "web"}

GOOD_PLAN = """# WO-001 Notes endpoints
Repository: org/api
- Depends on: None

## Requirements
- Add POST /notes and GET /notes/{id}.
- Update the committed openapi.json with both operations.

# WO-002 Notes page
Repository: org/web
- Depends on: WO-001

## Requirements
- Run `npm run api:pull && npm run api:generate` and call GET /notes/:id through the typed client.
"""


class DeferredWorkTests(unittest.TestCase):
    def test_a_plan_without_deferrals_passes(self):
        self.assertEqual(deferred_work_problems(GOOD_PLAN), [])

    def test_until_the_backend_spec_exists_needs_a_follow_up(self):
        plan = GOOD_PLAN.replace(
            "through the typed client.",
            "through the typed client.\n- Write the types from the planning docs until the backend's openapi.json is copied in.",
        )
        problems = deferred_work_problems(plan)
        self.assertEqual(len(problems), 1)
        self.assertIn("WO-002 defers work", problems[0])
        self.assertIn("Follow-up: WO-NNN", problems[0])

    def test_other_deferral_phrases(self):
        for phrase in ("Use a stub for now.", "Validation will be added later.", "TODO: paging.",
                       "Paging comes in a later work order."):
            with self.subTest(phrase=phrase):
                plan = GOOD_PLAN.replace("## Requirements\n- Add POST", f"## Requirements\n- {phrase}\n- Add POST")
                self.assertIn("WO-001 defers work", deferred_work_problems(plan)[0])

    def test_runtime_waits_are_not_deferrals(self):
        plan = GOOD_PLAN.replace(
            "through the typed client.",
            "through the typed client.\n- Show a spinner until the data is loaded and disable Save until the form is valid.",
        )
        self.assertEqual(deferred_work_problems(plan), [])

    def test_a_follow_up_that_depends_on_the_producer_satisfies_the_rule(self):
        plan = GOOD_PLAN.replace(
            "- Depends on: WO-001\n",
            "- Depends on: WO-001\n- Follow-up: WO-003\n",
        ).replace("through the typed client.", "through the typed client. Use a stub for exports for now.")
        plan += "\n# WO-003 Notes export\nRepository: org/web\n- Depends on: WO-002\n\n## Requirements\n- Replace the export stub once WO-002 is merged.\n"
        self.assertEqual(deferred_work_problems(plan), [])

    def test_a_follow_up_must_exist_and_depend_on_something(self):
        base = GOOD_PLAN.replace("- Depends on: None\n", "- Depends on: None\n- Follow-up: WO-009\n").replace(
            "- Add POST", "- Return fixed data for now.\n- Add POST"
        )
        self.assertIn("WO-009, which is not in the plan", deferred_work_problems(base)[0])
        orphan = base + "\n# WO-009 Real data\nRepository: org/api\n- Depends on: None\n"
        self.assertIn("follow-up WO-009 depends on nothing", deferred_work_problems(orphan)[0])


class ApiContractTests(unittest.TestCase):
    def test_contract_first_plan_passes(self):
        self.assertEqual(api_contract_problems(GOOD_PLAN, [API, WEB]), [])

    def test_api_work_order_must_update_openapi(self):
        plan = GOOD_PLAN.replace("- Update the committed openapi.json with both operations.\n", "")
        problems = api_contract_problems(plan, [API, WEB])
        self.assertTrue(any("WO-001 adds or changes endpoints but does not update the committed openapi.json" in p
                            for p in problems), problems)

    def test_web_work_order_must_depend_on_the_endpoint_provider(self):
        plan = GOOD_PLAN.replace("Repository: org/web\n- Depends on: WO-001", "Repository: org/web\n- Depends on: None")
        problems = api_contract_problems(plan, [API, WEB])
        self.assertIn("WO-002 calls GET /notes/{}, which WO-001 adds; it must depend on WO-001", problems)

    def test_web_work_order_must_generate_types(self):
        plan = GOOD_PLAN.replace("Run `npm run api:pull && npm run api:generate` and call", "Call")
        problems = api_contract_problems(plan, [API, WEB])
        self.assertEqual(len(problems), 1)
        self.assertIn("does not generate its API types", problems[0])

    def test_hand_written_types_are_refused_unless_forbidden(self):
        plan = GOOD_PLAN + "- Hand-write the request and response types in src/api/types.ts.\n"
        self.assertIn("WO-002 hand-writes API types", api_contract_problems(plan, [API, WEB])[0])
        self.assertIn("WO-002 hand-writes API types", api_contract_problems(plan, None)[0])
        forbidden = GOOD_PLAN + "- Never hand-write API types.\n"
        self.assertEqual(api_contract_problems(forbidden, [API, WEB]), [])

    def test_without_roles_only_the_hand_written_rule_applies(self):
        plan = GOOD_PLAN.replace("- Update the committed openapi.json with both operations.\n", "")
        self.assertEqual(api_contract_problems(plan, None), [])


class PlannerValidatorTests(unittest.TestCase):
    def test_planner_contract_reports_the_rules(self):
        markdown = GOOD_PLAN.replace("- Add POST", "- Return fixed data for now.\n- Add POST")
        output = {"artifacts": {"work_orders_markdown": markdown}}
        with mock.patch.object(stage_contracts, "work_orders_artifacts_failure_reason", return_value=None):
            violations = stage_contracts.validate_work_order_artifacts(
                {}, output, {"stage": "planner", "project_repositories": [API, WEB]}, []
            )
        self.assertEqual(len(violations), 1)
        self.assertIn("WO-001 defers work", violations[0].message)


if __name__ == "__main__":
    unittest.main()
