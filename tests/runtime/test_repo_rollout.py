"""Rolling out to a team's repos: onboarding drafts, branch protection, human PRs under harness checks."""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from ts_harness.gateway.github_webhook import _clear_harness_checks_for_human_pr
from ts_harness.implementation import repo_onboarding as onboarding
from ts_harness.implementation.verification import verify_implementation
from ts_harness.workflows import repo_governance as gov


def _write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


CI = """
name: CI
on:
  pull_request:
    branches: [dev]
jobs:
  e2e:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v5
      - run: cp env-example .env
      - run: npm ci
      - run: docker compose -f docker-compose.ci.yaml up --exit-code-from api
"""
COMPOSE = "services:\n  postgres:\n    image: postgres:17\n  maildev:\n    build: {context: ., dockerfile: maildev.Dockerfile}\n  api:\n    build: {context: ., dockerfile: e2e.Dockerfile}\n"
STARTUP = """#!/usr/bin/env bash
set -e
/opt/wait-for-it.sh postgres:5432
npm run migration:run
npm run seed:run
npm run start:prod > prod.log 2>&1 &
/opt/wait-for-it.sh maildev:1080
/opt/wait-for-it.sh localhost:3001
npm run lint
npm run test:e2e -- --runInBand
"""


class OnboardingDraftTests(unittest.TestCase):
    def _backend(self, repo: Path) -> None:
        _write(repo / ".github" / "workflows" / "ci.yml", CI)
        _write(repo / "docker-compose.ci.yaml", COMPOSE)
        _write(repo / "e2e.Dockerfile", "COPY ./startup.ci.sh /opt/startup.ci.sh\nRUN chmod +x /opt/startup.ci.sh\nCMD [\"/opt/startup.ci.sh\"]\n")
        _write(repo / "startup.ci.sh", STARTUP)
        _write(repo / "env-example", "DATABASE_HOST=postgres\n")
        _write(repo / "package.json", json.dumps({"dependencies": {"pg": "8"}}))

    def test_compose_based_ci_becomes_local_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._backend(repo)
            config, notes = onboarding.draft_config(repo)
            self.assertEqual(config["services"], ["postgres"])
            self.assertEqual(config["env_file"], "env-example")
            block = config["integration"]
            self.assertEqual(block["setup"], ["npm run migration:run", "npm run seed:run"])
            self.assertEqual(block["background"], [{"run": "npm run start:prod", "wait_for": "127.0.0.1:3001"}])
            self.assertEqual(block["command"], "npm run test:e2e -- --runInBand")
            self.assertTrue(any("maildev:1080" in note for note in notes))

    def test_chained_package_script_is_expanded(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write(repo / ".github" / "workflows" / "ci.yml", CI.replace("docker compose -f docker-compose.ci.yaml up --exit-code-from api", "yarn test-e2e"))
            _write(repo / "package.json", json.dumps({"scripts": {"test-e2e": "pm2 start \"yarn run-mock-server\" --name server && yarn playwright test"}}))
            config, notes = onboarding.draft_config(repo)
            self.assertEqual(config["integration"]["background"], [{"run": "yarn run-mock-server"}])
            self.assertEqual(config["integration"]["command"], "yarn playwright test")
            self.assertTrue(any("wait_for" in note for note in notes))

    def test_work_order_is_a_config_chore_with_the_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._backend(repo)
            body = onboarding.onboarding_work_order("WO-ONBOARD", repo)
            self.assertIn(onboarding.ONBOARD_MARKER, body)
            self.assertIn("npm run test:e2e -- --runInBand", body)
            self.assertTrue(body.rstrip().endswith("- .ts-harness.yml"))

    def test_onboarding_fails_until_the_suite_is_declared(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._backend(repo)
            _write(repo / ".ts-harness.yml", "services: [postgres]\n")
            body = onboarding.onboarding_work_order("WO-ONBOARD", repo)
            ok = lambda command, **_k: __import__("subprocess").CompletedProcess(command, 0, "", "")  # noqa: E731
            result = verify_implementation(repo, work_order_body=body, work_order_id="WO-ONBOARD", changed_files=[".ts-harness.yml"], runner=ok)
            self.assertIn("integration_suite_not_declared", result.reasons)


class FakeGitHub:
    def __init__(self, protection=None, workflows=None):
        self.protection = protection
        self.workflows = workflows or {}
        self.statuses = []

    def branch_protection(self, owner, repo, branch):
        return self.protection

    def get_tree(self, owner, repo, ref):
        return {"tree": [{"path": path} for path in self.workflows]}

    def get_file(self, owner, repo, path, ref):
        return {"content": base64.b64encode(self.workflows[path].encode()).decode()}

    def required_status_contexts(self, owner, repo, branch):
        return ["check", "harness/code-review", "harness/qa"]

    def create_status(self, owner, repo, sha, *, state, context, description):
        self.statuses.append((sha, state, context))


class GovernanceTests(unittest.TestCase):
    def test_workflow_triggers(self):
        self.assertTrue(gov.workflow_runs_on_pr({True: {"pull_request": {"branches": ["dev"]}}}, "dev"))
        self.assertFalse(gov.workflow_runs_on_pr({"on": {"pull_request": {"branches": ["main"]}}}, "dev"))
        self.assertTrue(gov.workflow_runs_on_pr({"on": ["push", "pull_request"]}, "dev"))
        self.assertFalse(gov.workflow_runs_on_pr({"on": {"pull_request": {"branches-ignore": ["dev"]}}}, "dev"))
        self.assertTrue(gov.workflow_runs_on_pr({"on": {"pull_request": {"branches": ["release/*", "d*"]}}}, "dev"))

    def test_unprotected_branch_report_and_payload(self):
        github = FakeGitHub(workflows={".github/workflows/ci.yml": CI, ".github/workflows/deploy.yml": "on: {push: {branches: [main]}}\njobs: {deploy: {}}\n"})
        report = gov.check_repo(github, "acme", "api", "dev")
        self.assertEqual(report.ci_checks, ["e2e"])
        self.assertEqual(report.ci_on_pull_requests, [".github/workflows/ci.yml"])
        self.assertTrue(any("not protected" in p for p in report.problems))
        body = gov.protection_body(report, None)
        self.assertEqual(body["required_status_checks"]["contexts"], ["e2e", "harness/code-review", "harness/qa", "harness/security-scan"])
        self.assertEqual(body["required_pull_request_reviews"]["required_approving_review_count"], 1)

    def test_stricter_settings_are_kept(self):
        current = {
            "required_status_checks": {"strict": False, "contexts": ["legacy-check"]},
            "required_pull_request_reviews": {"required_approving_review_count": 2, "require_code_owner_reviews": True},
            "enforce_admins": {"enabled": True},
        }
        github = FakeGitHub(protection=current, workflows={".github/workflows/ci.yml": CI})
        body = gov.protection_body(gov.check_repo(github, "acme", "api", "dev"), current)
        self.assertIn("legacy-check", body["required_status_checks"]["contexts"])
        self.assertEqual(body["required_pull_request_reviews"]["required_approving_review_count"], 2)
        self.assertTrue(body["enforce_admins"])

    def test_no_ci_on_prs_is_a_problem(self):
        report = gov.check_repo(FakeGitHub(workflows={}), "acme", "api", "dev")
        self.assertTrue(any("no CI workflow" in p for p in report.problems))


class HumanPrTests(unittest.TestCase):
    class Memory:
        def __init__(self, refs):
            self.refs = refs

        def find_external_refs(self, **_kwargs):
            return {"refs": self.refs}

    EVENT = {"pr_url": "https://github.com/acme/api/pull/5", "head_sha": "abc", "base_branch": "dev", "head_ref": "feature/x"}

    def test_human_pr_gets_the_required_harness_checks(self):
        github = FakeGitHub()
        posted = _clear_harness_checks_for_human_pr(self.EVENT, tenant_id="default", github=github, memory=self.Memory([]))
        self.assertEqual(posted, ["harness/code-review", "harness/qa"])
        self.assertEqual(github.statuses[0], ("abc", "success", "harness/code-review"))

    def test_harness_prs_are_left_alone(self):
        github = FakeGitHub()
        self.assertEqual(_clear_harness_checks_for_human_pr({**self.EVENT, "head_ref": "wo/wo-1-abc"}, tenant_id="default", github=github, memory=self.Memory([])), [])
        self.assertEqual(_clear_harness_checks_for_human_pr(self.EVENT, tenant_id="default", github=github, memory=self.Memory([{"program_id": "p"}])), [])
        self.assertEqual(github.statuses, [])


if __name__ == "__main__":
    unittest.main()
