"""BDD is decided per repository once per program, carried in work orders, and followed."""

from __future__ import annotations

import base64
import unittest
from unittest import mock

from ts_harness.workflows import bdd_policy as bp
from ts_harness.workflows import repo_bootstrap as rb

JEST_PACKAGE = '{"scripts": {"test": "jest"}, "devDependencies": {"jest": "1"}}'


class DecisionTests(unittest.TestCase):
    def test_facts_from_a_file_list(self):
        self.assertEqual(bp.inspect_files(["README.md"]), {"has_tests": False, "has_gherkin": False, "has_gherkin_runner": False})
        jest = bp.inspect_files(["src/a.ts", "src/a.test.ts", "package.json"], JEST_PACKAGE)
        self.assertEqual((jest["has_tests"], jest["has_gherkin"]), (True, False))
        cucumber = bp.inspect_files(["features/x.feature", "package.json"], '{"scripts": {"test:bdd": "cucumber-js"}}')
        self.assertEqual((cucumber["has_gherkin"], cucumber["has_gherkin_runner"]), (True, True))
        self.assertFalse(bp.inspect_files(["package.json"], '{"scripts": {"test": "echo \\"Error: no test specified\\""}}')["has_tests"])
        self.assertTrue(bp.inspect_files(["pkg/thing_test.go"])["has_tests"])

    def test_modes(self):
        none = {"has_tests": False, "has_gherkin": False}
        jest = {"has_tests": True, "has_gherkin": False}
        gherkin = {"has_tests": True, "has_gherkin": True}
        self.assertEqual(bp.decide("auto", none), (True, "the repository has no tests yet"))
        self.assertEqual(bp.decide("auto", jest)[0], False)
        self.assertEqual(bp.decide("auto", gherkin)[0], True)
        self.assertEqual(bp.decide("always", jest), (True, "project setting: always"))
        self.assertEqual(bp.decide("never", none), (False, "project setting: never"))
        self.assertEqual(bp.decide("bogus", none)[0], True)

    def test_marker(self):
        self.assertIs(bp.marker("## Tests\n\nBDD: required\n"), True)
        self.assertIs(bp.marker("- **BDD:** off"), False)
        self.assertIsNone(bp.marker("no line"))


class WorkOrderSectionTests(unittest.TestCase):
    MARKDOWN = (
        "# WO-001 Orders API\nRepository: acme/api\n\n## Summary\nx\n\n"
        "# WO-002 Week view\nRepository: acme/web\n\n## Summary\ny\n\n"
        "# WO-003 Docs\nRepository: acme/other\n\n## Summary\nz\n"
    )
    REPOS = [
        {"full_name": "acme/api", "bdd": True, "has_gherkin_runner": False},
        {"full_name": "acme/web", "bdd": False},
    ]

    def test_each_work_order_gets_its_repositorys_section_once(self):
        out = bp.with_tests_sections(self.MARKDOWN, self.REPOS)
        wo1, wo2, wo3 = out.split("# WO-")[1:]
        self.assertIn("BDD: required", wo1)
        self.assertIn("no Gherkin runner yet", wo1)
        self.assertIn("BDD: off", wo2)
        self.assertNotIn("## Tests", wo3)
        self.assertEqual(bp.with_tests_sections(out, self.REPOS), out)

    def test_one_repository_covers_work_orders_without_a_repository_line(self):
        out = bp.with_tests_sections("# WO-001 A\n\n## Summary\nx\n", [{"full_name": "acme/api", "bdd": True, "has_gherkin_runner": True}])
        self.assertIn("BDD: required", out)
        self.assertNotIn("no Gherkin runner", out)

    def test_adapter_adds_sections_when_work_orders_are_saved(self):
        from ts_harness.workflows.program_adapters import _extract_work_orders_outputs

        patch = _extract_work_orders_outputs(
            workflow_type="work_orders_from_prd",
            tasks=[{"status": "succeeded", "step_name": "plan_work_orders", "output": {"work_orders_markdown": self.MARKDOWN}}],
            artifacts={"project_repositories": self.REPOS},
        )
        self.assertIn("BDD: required", patch["work_orders_markdown"])


class FakeGitHub:
    def __init__(self, repos: dict[str, dict[str, str]]):
        self.repos = repos

    def get_tree(self, owner, repo, ref, recursive=True):
        return {"tree": [{"path": p, "type": "blob"} for p in self.repos[repo]]}

    def get_file(self, owner, repo, path, *, ref):
        text = self.repos[repo].get(path)
        return {"content": base64.b64encode(text.encode()).decode()} if text is not None else None


class AnnotationTests(unittest.TestCase):
    def annotate(self, github, repos, previous=None):
        templates = rb.load_templates()
        with mock.patch.object(rb, "load_templates", return_value=templates), mock.patch.object(rb, "live_settings", return_value=rb.settings(None)):
            return rb.annotate_repositories(github, repos, previous=previous)

    def test_decisions_ci_and_templates(self):
        github = FakeGitHub({
            "api": {"README.md": "x"},
            "web": {"package.json": JEST_PACKAGE, "src/a.test.ts": ""},
            "svc": {"package.json": JEST_PACKAGE, "src/a.test.ts": "", ".github/workflows/ci.yml": "on: [pull_request]\njobs:\n  test:\n    runs-on: x\n"},
        })
        out = self.annotate(github, [
            {"full_name": "acme/api", "role": "api", "bdd_mode": "auto"},
            {"full_name": "acme/web", "role": "worker", "bdd_mode": "auto"},
            {"full_name": "acme/svc", "role": "worker", "bdd_mode": "always"},
        ])
        api, web, svc = out
        self.assertEqual((api["template"], api["bdd"], api["has_gherkin_runner"]), ("node-api", True, True))
        self.assertNotIn("ci_on_pull_requests", api)
        self.assertEqual((web["bdd"], web["ci_on_pull_requests"]), (False, False))
        self.assertEqual((svc["bdd"], svc["has_gherkin_runner"], svc["ci_on_pull_requests"]), (True, False, True))

    def test_a_programs_decision_is_kept(self):
        github = FakeGitHub({"web": {"package.json": JEST_PACKAGE, "src/a.test.ts": ""}})
        repo = {"full_name": "acme/web", "role": "worker", "bdd_mode": "auto"}
        first = self.annotate(FakeGitHub({"web": {"README.md": "x"}}), [repo])
        self.assertTrue(first[0]["bdd"])  # empty at program start
        later = self.annotate(github, [repo], previous=first)  # a Jest setup arrived since
        self.assertEqual((later[0]["bdd"], later[0]["bdd_reason"]), (True, "the repository has no tests yet"))


class TicketFallbackTests(unittest.TestCase):
    def test_a_ticket_without_a_tests_section_gets_one(self):
        from ts_harness.jira.ticket_delivery import _with_test_policy

        memory = mock.Mock()
        memory.list_projects.return_value = {"projects": [{"id": "p", "name": "Acme", "jira_project_key": "ACME", "bdd_mode": "always"}]}
        memory.list_project_repositories.return_value = {"repositories": [{"full_name": "acme/web", "role": "web"}]}
        warnings: list[str] = []
        seen = []

        def inspect(repos):
            seen.extend(repos)
            return [{**repos[0], "bdd": True, "has_gherkin_runner": False}]

        body = _with_test_policy("# ACME-1 x\n", memory, tenant_id="t", jira_project="ACME", repo_url="https://github.com/acme/web", owner_repositories=[], warnings=warnings, inspect_repo=inspect)
        self.assertIn("BDD: required", body)
        self.assertEqual(seen[0]["bdd_mode"], "always")
        # The program's frozen decision wins, and a body that says BDD keeps it.
        frozen = [{"full_name": "acme/web", "bdd": False}]
        self.assertIn("BDD: off", _with_test_policy("# x\n", memory, tenant_id="t", jira_project="ACME", repo_url="https://github.com/acme/web", owner_repositories=frozen, warnings=warnings, inspect_repo=inspect))
        self.assertEqual(_with_test_policy("x\nBDD: off\n", memory, tenant_id="t", jira_project="ACME", repo_url="https://github.com/acme/web", owner_repositories=[], warnings=warnings, inspect_repo=inspect), "x\nBDD: off\n")


class CiSafetyNetTests(unittest.TestCase):
    def test_ci_added_by_a_work_order_becomes_required(self):
        from tests.runtime.test_repo_bootstrap import FakeGitHub as GH

        files = {"README.md": "x", "src/a.ts": "", rb.MARKER_FILE: "require_ci_checks: true\n"}
        workflow = "on:\n  pull_request:\njobs:\n  test:\n    name: test\n"
        github = GH(files, workflows={".github/workflows/ci.yml": workflow})
        github.protection = {"required_status_checks": {"contexts": ["harness/qa"]}, "required_pull_request_reviews": {"required_approving_review_count": 1, "dismiss_stale_reviews": True}}
        result = rb.ensure_ready(github, "https://github.com/acme/svc", "dev", role="worker", templates=rb.load_templates(), config=rb.settings(None))
        self.assertTrue(result.ready)
        self.assertIn("test", github.set_protection["required_status_checks"]["contexts"])


if __name__ == "__main__":
    unittest.main()
