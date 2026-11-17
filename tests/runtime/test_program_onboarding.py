"""Programs onboard repositories that lack .ts-harness.yml before their first work orders."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ts_harness.implementation.work_order_context import (
    dependencies_from_artifacts,
    extract_work_order_section,
    parse_work_order_repositories,
    select_ready_work_order_ids,
)
from ts_harness.workflows.program_adapters import _route_work_orders_to_repositories, _work_order_wave
from ts_harness.workflows.repo_onboarding_gate import (
    Inspection,
    Target,
    inspect_repository,
    onboarding_targets,
    should_check,
    with_repo_onboarding,
)

MARKDOWN = """# Work orders

## WO-001 Add the endpoint
| Field | Value |
|---|---|
| Depends on | None |

## WO-002 Add the page
| Field | Value |
|---|---|
| Depends on | WO-001 |
"""

MULTI_MARKDOWN = """# Work orders

## WO-001 API endpoint
Repository: acme/api
| Depends on | None |

## WO-002 Web page
Repository: acme/web
| Depends on | None |

## WO-003 Web follow-up
Repository: acme/web
| Depends on | WO-002 |
"""

REPOS = [
    {"full_name": "acme/api", "repo_url": "https://github.com/acme/api", "default_branch": "dev"},
    {"full_name": "acme/web", "repo_url": "https://github.com/acme/web", "default_branch": "main"},
]


_no_configured_repo = patch("ts_harness.workflows.repo_onboarding_gate.configured_target", return_value=None)


def setUpModule():
    _no_configured_repo.start()


def tearDownModule():
    _no_configured_repo.stop()


def body(wo_id: str) -> str:
    return f"# {wo_id} Onboard this repository to TS Harness coding jobs\n\n## Summary\nchore: add config\n\n## Files\n- .ts-harness.yml\n"


def needs(repos: set[str]):
    def inspect(target: Target, wo_id: str) -> Inspection:
        if target.repo_url in repos:
            return Inspection(True, "missing", body(wo_id))
        return Inspection(False, "already has .ts-harness.yml")

    return inspect


def single(**extra):
    return {
        "work_orders_markdown": MARKDOWN,
        "work_order_ids": ["WO-001", "WO-002"],
        "next_work_order_id": "WO-001",
        "repo_url": "https://github.com/acme/api",
        "default_branch": "dev",
        **extra,
    }


class SingleRepositoryTests(unittest.TestCase):
    def test_repo_without_config_gets_an_onboarding_work_order_first(self):
        out = with_repo_onboarding(single(), inspect=needs({"https://github.com/acme/api"}))
        self.assertEqual(out["work_order_ids"], ["WO-0000", "WO-001", "WO-002"])
        self.assertEqual(out["onboarding_gates"], {"WO-0000": ""})
        self.assertEqual(out["next_work_order_ids"], ["WO-0000"])
        self.assertEqual(out["next_work_order_id"], "WO-0000")
        section = extract_work_order_section(out["work_orders_markdown"], "WO-0000")
        self.assertIn("Onboard this repository", section)
        self.assertNotIn("Repository:", section)
        # The planner's sections are untouched and still parse.
        self.assertIn("Add the page", extract_work_order_section(out["work_orders_markdown"], "WO-002"))
        self.assertEqual(out["repo_onboarding"]["repositories"][0]["work_order_id"], "WO-0000")

    def test_every_work_order_waits_for_the_onboarding_pr(self):
        out = with_repo_onboarding(single(), inspect=needs({"https://github.com/acme/api"}))
        deps = dependencies_from_artifacts(out)
        self.assertEqual(deps["WO-001"], ["WO-0000"])
        self.assertEqual(sorted(deps["WO-002"]), ["WO-0000", "WO-001"])
        self.assertEqual(select_ready_work_order_ids(out["work_order_ids"], ["WO-0000"], deps), ["WO-001"])

    def test_work_orders_added_later_are_gated_too(self):
        out = with_repo_onboarding(single(), inspect=needs({"https://github.com/acme/api"}))
        out["work_order_ids"].append("WO-003")
        out["work_orders_markdown"] += "\n## WO-003 Later\n| Depends on | None |\n"
        self.assertEqual(dependencies_from_artifacts(out)["WO-003"], ["WO-0000"])

    def test_structured_dependencies_are_gated(self):
        out = with_repo_onboarding(
            single(work_order_dependencies={"WO-001": [], "WO-002": ["WO-001"]}),
            inspect=needs({"https://github.com/acme/api"}),
        )
        self.assertEqual(dependencies_from_artifacts(out)["WO-001"], ["WO-0000"])

    def test_repo_already_set_up_is_left_alone(self):
        artifacts = single()
        out = with_repo_onboarding(artifacts, inspect=needs(set()))
        self.assertEqual(out["work_order_ids"], ["WO-001", "WO-002"])
        self.assertNotIn("onboarding_gates", out)
        self.assertEqual(out["work_orders_markdown"], MARKDOWN)
        self.assertEqual(out["repo_onboarding"]["repositories"][0]["reason"], "already has .ts-harness.yml")

    def test_check_failure_is_recorded_and_does_not_block(self):
        def broken(target, wo_id):
            raise RuntimeError("network down")

        out = with_repo_onboarding(single(), inspect=broken)
        self.assertEqual(out["work_order_ids"], ["WO-001", "WO-002"])
        self.assertIn("network down", out["repo_onboarding"]["repositories"][0]["reason"])

    def test_checked_once(self):
        out = with_repo_onboarding(single(), inspect=needs({"https://github.com/acme/api"}))
        self.assertTrue(should_check("implement", single()))
        self.assertFalse(should_check("implement", out))
        self.assertFalse(should_check("work_orders", single()))
        self.assertFalse(should_check("implement", {"jira_epic_key": "EPIC-1"}))

    def test_ids_do_not_collide(self):
        artifacts = single(work_order_ids=["WO-0000", "WO-001"])
        out = with_repo_onboarding(artifacts, inspect=needs({"https://github.com/acme/api"}))
        self.assertEqual(out["work_order_ids"][0], "WO-0001")

    def test_targets_follow_the_implement_intake(self):
        conf = Target("https://github.com/acme/conf", "develop")
        # The configured repository wins over repo_url, as in the implement intake.
        self.assertEqual(onboarding_targets(single(), conf), [Target("https://github.com/acme/conf", "dev")])
        artifacts = single()
        artifacts.pop("default_branch")
        self.assertEqual(onboarding_targets(artifacts, conf)[0].base_branch, "develop")
        self.assertEqual(onboarding_targets(single(), None)[0].repo_url, "https://github.com/acme/api")
        one = single(project_repositories=[REPOS[1]])
        self.assertEqual(onboarding_targets(one, conf), [Target("https://github.com/acme/web", "dev", "acme/web")])
        many = single(project_repositories=REPOS)
        self.assertEqual([t.base_branch for t in onboarding_targets(many, conf)], ["dev", "main"])


class MultiRepositoryTests(unittest.TestCase):
    def artifacts(self):
        return {
            "work_orders_markdown": MULTI_MARKDOWN,
            "work_order_ids": ["WO-001", "WO-002", "WO-003"],
            "next_work_order_id": "WO-001",
            "project_repositories": REPOS,
        }

    def test_only_the_repo_that_needs_it_is_gated(self):
        out = with_repo_onboarding(self.artifacts(), inspect=needs({"https://github.com/acme/web"}))
        self.assertEqual(out["onboarding_gates"], {"WO-0001": "acme/web"})
        deps = dependencies_from_artifacts(out)
        self.assertEqual(deps["WO-001"], [])
        self.assertEqual(deps["WO-002"], ["WO-0001"])
        # API work starts alongside the web repo's onboarding.
        self.assertEqual(out["next_work_order_ids"], ["WO-0001", "WO-001"])
        self.assertEqual(_work_order_wave(out), ["WO-0001", "WO-001"])

    def test_onboarding_work_order_is_routed_to_its_repository(self):
        out = with_repo_onboarding(self.artifacts(), inspect=needs({"https://github.com/acme/web"}))
        self.assertEqual(parse_work_order_repositories(out["work_orders_markdown"])["WO-0001"], "acme/web")
        intake = {"work_orders": [{"work_order_id": "WO-0001"}, {"work_order_id": "WO-001"}]}
        _route_work_orders_to_repositories(intake, out["work_orders_markdown"], REPOS)
        self.assertEqual(intake["work_orders"][0]["repo_url"], "https://github.com/acme/web")
        self.assertEqual(intake["work_orders"][0]["base_branch"], "main")
        self.assertEqual(intake["work_orders"][1]["repo_url"], "https://github.com/acme/api")

    def test_unnamed_work_orders_wait_for_every_gate(self):
        artifacts = self.artifacts()
        artifacts["work_orders_markdown"] += "\n## WO-004 Unrouted\n| Depends on | None |\n"
        artifacts["work_order_ids"].append("WO-004")
        out = with_repo_onboarding(artifacts, inspect=needs({"https://github.com/acme/api", "https://github.com/acme/web"}))
        self.assertEqual(sorted(dependencies_from_artifacts(out)["WO-004"]), ["WO-0000", "WO-0001"])


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class InspectRepositoryTests(unittest.TestCase):
    def repo(self, tmp: Path, files: dict[str, str]) -> str:
        work = tmp / "work"
        work.mkdir()
        _git(work, "init", "-q", "-b", "dev")
        for name, text in files.items():
            path = work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        _git(work, "add", "-A")
        _git(work, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "init")
        return str(work)

    def inspect(self, files: dict[str, str]) -> Inspection:
        with tempfile.TemporaryDirectory() as tmp:
            url = self.repo(Path(tmp), files)
            with patch("ts_harness.integrations.github_client.repo_token", return_value=""):
                return inspect_repository(Target(url, "dev"), "WO-0000")

    E2E = {
        "package.json": '{"name": "app", "scripts": {"test:e2e": "jest --config test/jest-e2e.json"}}',
        ".github/workflows/ci.yml": "jobs:\n  e2e:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: npm ci\n      - run: npm run test:e2e\n",
    }

    def test_repo_with_e2e_suite_and_no_config_needs_onboarding(self):
        found = self.inspect(self.E2E)
        self.assertTrue(found.needed, found.reason)
        self.assertTrue(found.body.startswith("# WO-0000 Onboard"))
        self.assertIn(".ts-harness.yml", found.body)

    def test_repo_with_config_does_not(self):
        found = self.inspect({**self.E2E, ".ts-harness.yml": "services: [postgres]\n"})
        self.assertFalse(found.needed)
        self.assertEqual(found.reason, "already has .ts-harness.yml")

    def test_repo_without_suite_does_not(self):
        found = self.inspect({"package.json": '{"name": "lib", "scripts": {"test": "jest"}}'})
        self.assertFalse(found.needed)

    def test_clone_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            found = inspect_repository(Target(str(Path(tmp) / "missing"), "dev"), "WO-0000")
        self.assertFalse(found.needed)
        self.assertIn("clone failed", found.reason)


class ProgramServiceHookTests(unittest.TestCase):
    def test_hook_runs_only_on_first_implement_entry_and_never_raises(self):
        from ts_harness.workflows import delivery_program

        with patch("ts_harness.workflows.repo_onboarding_gate.inspect_repository", side_effect=needs({"https://github.com/acme/api"})):
            out = delivery_program._with_repo_onboarding("implement", single())
        self.assertEqual(out["next_work_order_id"], "WO-0000")
        self.assertIs(delivery_program._with_repo_onboarding("implement", out), out)
        with patch("ts_harness.workflows.repo_onboarding_gate.with_repo_onboarding", side_effect=RuntimeError("boom")):
            artifacts = single()
            self.assertIs(delivery_program._with_repo_onboarding("implement", artifacts), artifacts)


if __name__ == "__main__":
    unittest.main()
