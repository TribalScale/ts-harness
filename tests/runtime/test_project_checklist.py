"""Project setup checklist: each check with fakes, the runner's time-box, and the endpoint."""

from __future__ import annotations

import base64
import threading
import unittest
from typing import Any
from unittest import mock

from ts_harness.gateway import projects_bff as bff
from ts_harness.gateway.auth import AuthContext
from ts_harness.integrations.github_client import GitHubError
from ts_harness.integrations.jira_client import JiraError
from ts_harness.workflows import project_checklist as pc

PROJECT = {
    "id": "p1",
    "tenant_id": "default",
    "key": "PROJ",
    "name": "Project",
    "team_slug": "web",
    "jira_project_key": "PROJ",
    "default_base_branch": "dev",
    "bdd_mode": "auto",
    "revision_autonomy": "autonomous",
    "max_parallel_tickets": 2,
}
REPO = {
    "id": "r1",
    "full_name": "acme/api",
    "github_host": "github.com",
    "repo_url": "https://github.com/acme/api",
    "default_branch": None,
}
CI_YAML = "on:\n  pull_request:\n    branches: [dev]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
ALL_CONTEXTS = ["ci", "harness/code-review", "harness/security-scan", "harness/independent-review"]


class FakeJira:
    def __init__(self, project: dict[str, Any] | None = None, error: Exception | None = None):
        self.project, self.error, self.paths = project, error, []

    def request(self, method: str, path: str, **_: Any) -> Any:
        self.paths.append((method, path))
        if self.error:
            raise self.error
        return self.project


class FakeGitHub:
    def __init__(
        self,
        *,
        repo_status: int = 200,
        branches: tuple[str, ...] = ("dev",),
        contexts: list[str] | None = ALL_CONTEXTS,
        workflows: dict[str, str] | None = None,
        protection_error: Exception | None = None,
    ):
        self.repo_status, self.branches, self.contexts = repo_status, branches, contexts
        self.workflows = {".github/workflows/ci.yml": CI_YAML} if workflows is None else workflows
        self.protection_error = protection_error
        self.protection_reads = 0

    def request(self, method: str, path: str, *, allow_404: bool = False, **_: Any) -> Any:
        if "/branches/" in path:
            return {"name": path.rsplit("/", 1)[1]} if path.rsplit("/", 1)[1] in self.branches else None
        if self.repo_status >= 400:
            raise GitHubError(self.repo_status, "Not Found")
        return {"full_name": path.removeprefix("/repos/")}

    def required_status_contexts(self, owner: str, repo: str, branch: str) -> list[str] | None:
        self.protection_reads += 1
        if self.protection_error:
            raise self.protection_error
        return self.contexts

    def get_tree(self, owner: str, repo: str, ref: str, **_: Any) -> dict[str, Any]:
        return {"tree": [{"path": p} for p in self.workflows] + [{"path": "README.md"}]}

    def get_file(self, owner: str, repo: str, path: str, *, ref: str) -> dict[str, Any]:
        return {"content": base64.b64encode(self.workflows[path].encode()).decode()}


def ctx(**overrides: Any) -> pc.ChecklistContext:
    github = overrides.pop("gh", FakeGitHub())
    values: dict[str, Any] = {
        "project": dict(PROJECT),
        "repositories": [dict(REPO)],
        "program_defaults": {"jira_filing": {"allowed_projects": ["PROJ"]}, "github": {"org": "acme"}},
        "github_logins": {"lead@example.com": "lead-gh"},
        "jira_account_ids": {"lead@example.com": "acc-1"},
        "requester": "lead@example.com",
        "jira": lambda: FakeJira({"key": "PROJ", "name": "Project"}),
        "github": lambda _url: github,
        "installation": lambda *_: 7,
        "memory": mock.MagicMock(),
    }
    values.update(overrides)
    return pc.ChecklistContext(**values)


class JiraAllowedTests(unittest.TestCase):
    def test_ok_when_on_allowlist(self):
        self.assertEqual(pc.check_jira_allowed(ctx())["status"], pc.OK)

    def test_ok_without_allowlist(self):
        item = pc.check_jira_allowed(ctx(program_defaults={}))
        self.assertEqual(item["status"], pc.OK)
        self.assertIn("no Jira allowlist", item["summary"])

    def test_failing_when_not_allowed_links_to_settings(self):
        item = pc.check_jira_allowed(ctx(program_defaults={"jira_filing": {"allowed_projects": ["OPS"]}}))
        self.assertEqual(item["status"], pc.FAILING)
        self.assertEqual(item["fix_link"], pc.LINK_JIRA_DEFAULTS)
        self.assertIn("PROJ", item["fix"])

    def test_failing_without_key(self):
        item = pc.check_jira_allowed(ctx(project={**PROJECT, "jira_project_key": None}))
        self.assertEqual((item["status"], item["fix_link"]), (pc.FAILING, "#/system/projects/p1"))


class JiraProjectTests(unittest.TestCase):
    def test_ok_gets_the_project(self):
        jira = FakeJira({"key": "PROJ", "name": "Project"})
        item = pc.check_jira_project(ctx(jira=lambda: jira))
        self.assertEqual(item["status"], pc.OK)
        self.assertEqual(jira.paths, [("GET", "/rest/api/3/project/PROJ")])

    def test_warning_without_key(self):
        self.assertEqual(pc.check_jira_project(ctx(project={**PROJECT, "jira_project_key": ""}))["status"], pc.WARNING)

    def test_failing_when_project_missing(self):
        item = pc.check_jira_project(ctx(jira=lambda: FakeJira(error=JiraError(404, "nope"))))
        self.assertEqual(item["status"], pc.FAILING)
        self.assertIn("does not exist", item["summary"])

    def test_failing_when_credentials_rejected_or_missing(self):
        self.assertEqual(pc.check_jira_project(ctx(jira=lambda: FakeJira(error=JiraError(401, "x"))))["status"], pc.FAILING)

        def broken():
            raise JiraError(500, "ATLASSIAN_SITE_URL is required")

        self.assertEqual(pc.check_jira_project(ctx(jira=broken))["status"], pc.FAILING)

    def test_other_errors_raise_for_the_runner(self):
        with self.assertRaises(JiraError):
            pc.check_jira_project(ctx(jira=lambda: FakeJira(error=JiraError(502, "bad gateway"))))


class RepositoriesTests(unittest.TestCase):
    def test_ok(self):
        item = pc.check_repositories(ctx())
        self.assertEqual(item["status"], pc.OK)
        self.assertIn("dev exists", item["details"][0])

    def test_failing_without_repositories(self):
        self.assertEqual(pc.check_repositories(ctx(repositories=[]))["status"], pc.FAILING)

    def test_failing_when_branch_missing_or_unreachable(self):
        missing = pc.check_repositories(ctx(gh=FakeGitHub(branches=("main",))))
        self.assertEqual(missing["status"], pc.FAILING)
        self.assertIn("base branch dev does not exist", missing["details"][0])
        unreachable = pc.check_repositories(ctx(gh=FakeGitHub(repo_status=404), installation=lambda *_: None))
        self.assertEqual(unreachable["status"], pc.FAILING)
        self.assertIn("not installed", unreachable["details"][0])
        self.assertEqual(unreachable["fix_link"], pc.LINK_GITHUB)

    def test_warning_when_only_the_fallback_token_reaches_it(self):
        item = pc.check_repositories(ctx(installation=lambda *_: None))
        self.assertEqual(item["status"], pc.WARNING)
        self.assertIn("fallback token", item["details"][0])

    def test_token_only_install_without_an_app(self):
        item = pc.check_repositories(ctx(installation=lambda *_: "no_app"))
        self.assertEqual(item["status"], pc.OK)
        self.assertIn("no GitHub App registered", item["details"][0])
        denied = pc.check_repositories(ctx(installation=lambda *_: "no_app", gh=FakeGitHub(repo_status=403)))
        self.assertIn("the GitHub token cannot read it", denied["details"][0])

    def test_one_repository_error_is_a_warning_line(self):
        def boom(*_):
            raise RuntimeError("app key unreadable")

        item = pc.check_repositories(ctx(installation=boom))
        self.assertEqual(item["status"], pc.WARNING)
        self.assertIn("could not check: app key unreadable", item["details"][0])


class RepoAllowedTests(unittest.TestCase):
    def test_ok_inside_org(self):
        self.assertEqual(pc.check_repo_allowed(ctx())["status"], pc.OK)

    def test_failing_outside_org(self):
        item = pc.check_repo_allowed(ctx(program_defaults={"github": {"org": "other"}}))
        self.assertEqual(item["status"], pc.FAILING)
        self.assertIn("outside the tenant GitHub org other", item["details"][0])

    def test_linked_repo_passes_the_allowlist(self):
        memory = mock.MagicMock()
        memory.list_projects.return_value = {"projects": [{"id": "p1"}]}
        memory.list_project_repositories.return_value = {"repositories": [REPO]}
        defaults = {"repo_picker": {"repos": [{"id": "acme/web"}]}}
        self.assertEqual(pc.check_repo_allowed(ctx(program_defaults=defaults, memory=memory))["status"], pc.OK)

    def test_warning_without_repositories(self):
        self.assertEqual(pc.check_repo_allowed(ctx(repositories=[]))["status"], pc.WARNING)


class BranchProtectionTests(unittest.TestCase):
    def test_ok_reads_protection_once(self):
        github = FakeGitHub()
        item = pc.check_branch_protection(ctx(gh=github))
        self.assertEqual(item["status"], pc.OK)
        self.assertEqual(github.protection_reads, 1)

    def test_failing_when_independent_review_not_required(self):
        item = pc.check_branch_protection(ctx(gh=FakeGitHub(contexts=["harness/code-review", "harness/security-scan"])))
        self.assertEqual(item["status"], pc.FAILING)
        self.assertIn("branch protection does not require harness/independent-review", item["details"][0])

    def test_independent_review_not_wanted_when_disabled(self):
        defaults = {"governance": {"independent_review": {"enabled": False}}}
        github = FakeGitHub(contexts=["harness/code-review", "harness/security-scan"])
        self.assertEqual(pc.check_branch_protection(ctx(gh=github, program_defaults=defaults))["status"], pc.OK)

    def test_failing_when_unprotected_or_missing_checks(self):
        self.assertIn("not protected", pc.check_branch_protection(ctx(gh=FakeGitHub(contexts=None)))["details"][0])
        item = pc.check_branch_protection(ctx(gh=FakeGitHub(contexts=["harness/independent-review"])))
        self.assertIn("harness/code-review, harness/security-scan", item["details"][0])

    def test_error_reading_protection_is_a_warning(self):
        item = pc.check_branch_protection(ctx(gh=FakeGitHub(protection_error=GitHubError(403, "forbidden"))))
        self.assertEqual(item["status"], pc.WARNING)
        self.assertIn("could not read branch protection", item["details"][0])


class CiTests(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(pc.check_ci(ctx())["status"], pc.OK)

    def test_warning_without_pr_workflow(self):
        github = FakeGitHub(workflows={".github/workflows/deploy.yml": "on: push\n"})
        item = pc.check_ci(ctx(gh=github))
        self.assertEqual(item["status"], pc.WARNING)
        self.assertIn("no workflow runs on pull requests into dev", item["details"][0])

    def test_unreadable_workflows_is_a_warning(self):
        github = FakeGitHub()
        github.get_tree = mock.Mock(side_effect=GitHubError(500, "boom"))
        self.assertIn("could not read", pc.check_ci(ctx(gh=github))["details"][0])


class IdentityTests(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(pc.check_identity(ctx())["status"], pc.OK)

    def test_failing_without_any_mapping(self):
        item = pc.check_identity(ctx(github_logins={}))
        self.assertEqual((item["status"], item["fix_link"]), (pc.FAILING, pc.LINK_IDENTITY))

    def test_failing_when_requester_unmapped(self):
        item = pc.check_identity(ctx(requester="new@example.com"))
        self.assertEqual(item["status"], pc.FAILING)
        self.assertIn("new@example.com has no GitHub login mapping", item["details"])

    def test_warning_when_jira_unmapped(self):
        self.assertEqual(pc.check_identity(ctx(jira_account_ids={}))["status"], pc.WARNING)
        no_jira = {**PROJECT, "jira_project_key": None}
        self.assertEqual(pc.check_identity(ctx(jira_account_ids={}, project=no_jira))["status"], pc.OK)


class ProjectSettingsTests(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(pc.check_project_settings(ctx())["status"], pc.OK)

    def test_failing_on_invalid_values(self):
        bad = {**PROJECT, "bdd_mode": None, "revision_autonomy": "wild", "max_parallel_tickets": 0}
        item = pc.check_project_settings(ctx(project=bad))
        self.assertEqual(item["status"], pc.FAILING)
        self.assertEqual(len(item["details"]), 3)


class RunnerTests(unittest.TestCase):
    def test_all_ready(self):
        out = pc.run_checklist(ctx())
        self.assertEqual((out["ready"], out["total"], out["summary"]), (8, 8, "8 of 8 ready"))
        self.assertEqual([i["id"] for i in out["items"]], [c[0] for c in pc.CHECKS])
        self.assertTrue(all(i["fix"] == "" for i in out["items"]))

    def test_an_error_or_timeout_stays_in_its_item(self):
        release = threading.Event()

        def boom(_ctx):
            raise RuntimeError("kaput")

        def slow(_ctx):
            release.wait(5)
            return pc._item("slow", "Slow", pc.OK, "late")

        checks = [("boom", "Boom", boom), ("slow", "Slow", slow), ("settings", "Settings", pc.check_project_settings)]
        try:
            out = pc.run_checklist(ctx(), timeout=0.2, checks=checks)
        finally:
            release.set()
        by_id = {i["id"]: i for i in out["items"]}
        self.assertEqual(by_id["boom"]["status"], pc.WARNING)
        self.assertIn("Could not check: kaput", by_id["boom"]["summary"])
        self.assertIn("timed out", by_id["slow"]["summary"])
        self.assertEqual(by_id["project_settings"]["status"], pc.OK)
        self.assertEqual(out["summary"], "1 of 3 ready")


class EndpointTests(unittest.TestCase):
    def setUp(self):
        from starlette.testclient import TestClient

        from ts_harness.gateway import asgi_app

        self.memory = mock.MagicMock()
        self.memory.get_project.side_effect = lambda pid: dict(PROJECT) if pid == "p1" else {"id": pid, "tenant_id": "other"}
        self.memory.list_project_repositories.return_value = {"repositories": [REPO]}
        config = mock.Mock(
            program_defaults={"jira_filing": {"allowed_projects": ["OPS"]}},
            identity=mock.Mock(github_logins={}, jira_account_ids={}),
        )
        self.auth = AuthContext(actor_id="dev@example.com", tenant_id="default", roles=["requester"])
        github = FakeGitHub()
        patches = [
            mock.patch.object(asgi_app, "get_runtime", return_value=mock.Mock(config=config)),
            mock.patch.object(asgi_app, "_auth", return_value=self.auth),
            mock.patch.object(asgi_app, "_check_origin", return_value=None),
            mock.patch.object(asgi_app, "_require_admin", return_value=None),
            mock.patch.object(asgi_app, "get_memory_client", return_value=self.memory),
            mock.patch.object(bff, "_is_admin", return_value=False),
            mock.patch.object(bff, "_default_jira", return_value=FakeJira({"key": "PROJ"})),
            mock.patch.object(bff, "_default_github", return_value=github),
            mock.patch.object(bff, "_default_installation", return_value=7),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.client = TestClient(asgi_app.app)

    def test_viewer_gets_the_checklist(self):
        response = self.client.get("/admin/projects/p1/checklist")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        by_id = {i["id"]: i for i in body["items"]}
        self.assertEqual(body["total"], 8)
        self.assertEqual(by_id["jira_allowed"]["status"], "failing")
        self.assertEqual(by_id["identity"]["status"], "failing")
        self.assertEqual(by_id["repositories"]["status"], "ok")
        self.assertEqual(body["summary"], f"{body['ready']} of 8 ready")

    def test_other_tenant_project_is_not_found(self):
        self.assertEqual(self.client.get("/admin/projects/p2/checklist").status_code, 404)

    def test_repository_listing_failure_does_not_fail_the_checklist(self):
        self.memory.list_project_repositories.side_effect = RuntimeError("memory down")
        body = self.client.get("/admin/projects/p1/checklist").json()
        self.assertEqual({i["id"]: i["status"] for i in body["items"]}["repositories"], "failing")


if __name__ == "__main__":
    unittest.main()
