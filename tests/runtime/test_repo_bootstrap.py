"""Empty project repositories start from a harness template through a bootstrap PR."""

from __future__ import annotations

import base64
import json
import unittest
from pathlib import Path
from unittest import mock

from ts_harness.integrations.github_client import GitHubError
from ts_harness.workflows import repo_bootstrap as rb

ROOT = Path(__file__).resolve().parents[2]


class FakeGitHub:
    """Enough of the GitHub REST API for a bootstrap: trees, commits, refs, pulls, protection."""

    def __init__(self, files: dict[str, str] | None, *, workflows: dict[str, str] | None = None):
        self.files = files  # None: the branch does not exist
        self.workflows = workflows or {}
        self.calls: list[tuple[str, str]] = []
        self.pulls: list[dict] = []
        self.tree_body: dict | None = None
        self.protection: dict | None = None
        self.set_protection: dict | None = None

    def get_tree(self, owner, repo, ref, recursive=True):
        if self.files is None:
            raise GitHubError(404, "Not Found")
        return {"tree": [{"path": p, "type": "blob"} for p in {**self.files, **self.workflows}]}

    def get_file(self, owner, repo, path, *, ref):
        text = {**(self.files or {}), **self.workflows}.get(path)
        return {"content": base64.b64encode(text.encode()).decode()} if text is not None else None

    def branch_protection(self, owner, repo, branch):
        return self.protection

    def set_branch_protection(self, owner, repo, branch, body):
        self.set_protection = body

    def request(self, method, path, *, body=None, query=None, allow_404=False):
        self.calls.append((method, path))
        if method == "GET" and path.endswith("/pulls"):
            return [p for p in self.pulls if p["head"] == query["head"].split(":", 1)[1]]
        if method == "GET" and "/git/ref/heads/ts-harness" in path:
            return None
        if method == "GET" and "/git/ref/heads/" in path:
            return {"object": {"sha": "base-sha"}}
        if method == "GET" and "/git/commits/" in path:
            return {"tree": {"sha": "base-tree"}}
        if method == "POST" and path.endswith("/git/trees"):
            self.tree_body = body
            return {"sha": "new-tree"}
        if method == "POST" and path.endswith("/git/commits"):
            assert body["parents"] == ["base-sha"] and body["tree"] == "new-tree"
            return {"sha": "new-commit"}
        if method == "POST" and path.endswith("/git/refs"):
            return {}
        if method == "POST" and path.endswith("/pulls"):
            pr = {"head": body["head"], "html_url": f"https://github.com/acme/web/pull/{len(self.pulls) + 1}", "body": body["body"]}
            self.pulls.append(pr)
            return pr
        raise AssertionError(f"unexpected {method} {path}")


TEMPLATES = rb.load_templates(ROOT / "templates")
CONFIG = rb.settings(None)


class CatalogTests(unittest.TestCase):
    def test_the_shipped_templates_load(self):
        self.assertEqual(set(TEMPLATES), {"node-api", "react-web"})
        for template in TEMPLATES.values():
            self.assertEqual(template.required_checks, ("checks", "bdd", "docker"))
            self.assertIn("## Roles", template.conventions())

    def test_roles_pick_a_template_and_settings_override(self):
        self.assertEqual(rb.template_for_role("API", TEMPLATES, CONFIG).name, "node-api")
        self.assertEqual(rb.template_for_role("web", TEMPLATES, CONFIG).name, "react-web")
        self.assertIsNone(rb.template_for_role("worker", TEMPLATES, CONFIG))
        self.assertIsNone(rb.template_for_role(None, TEMPLATES, CONFIG))
        custom = rb.settings({"repo_templates": {"by_role": {"worker": "node-api", "web": "none"}}})
        self.assertEqual(rb.template_for_role("worker", TEMPLATES, custom).name, "node-api")
        self.assertIsNone(rb.template_for_role("web", TEMPLATES, custom))
        self.assertIsNone(rb.template_for_role("api", TEMPLATES, rb.settings({"repo_templates": {"enabled": False}})))

    def test_render_fills_in_the_project_and_marks_the_template(self):
        files = rb.render(TEMPLATES["node-api"], repo_name="Acme-API-Test", title="Acme Test")
        self.assertNotIn("template.json", files)
        self.assertEqual(json.loads(files["package.json"])["name"], "acme-api-test")
        lock = json.loads(files["package-lock.json"])
        self.assertEqual((lock["name"], lock["packages"][""]["name"]), ("acme-api-test", "acme-api-test"))
        self.assertIn("title: 'Acme Test'", files["src/config/openapi.ts"])
        self.assertTrue(files["README.md"].startswith("# Acme Test\n"))
        self.assertIn("template: node-api\n", files[rb.MARKER_FILE])
        self.assertIn("services: [postgres]", files[rb.MARKER_FILE])
        self.assertIn(".github/workflows/ci.yml", files)
        web = rb.render(TEMPLATES["react-web"], repo_name="acme-web")
        self.assertIn("<title>Acme Web</title>", web["index.html"])
        self.assertIn('font-semibold">Acme Web<', web["src/components/AppLayout.tsx"])

    def test_names(self):
        self.assertEqual(rb.display_name("Acme-API-Test"), "Acme API Test")
        self.assertEqual(rb.display_name("order_service"), "Order Service")
        self.assertEqual(rb.package_name("Acme API!"), "acme-api")


class EmptinessTests(unittest.TestCase):
    def test_starter_files_only(self):
        self.assertTrue(rb.is_starter_only([]))
        self.assertTrue(rb.is_starter_only(["README.md", ".gitignore", "LICENSE"]))
        self.assertFalse(rb.is_starter_only(["README.md", "package.json"]))
        self.assertFalse(rb.is_starter_only(["docs/README.md"]))


class EnsureReadyTests(unittest.TestCase):
    def ensure(self, github, role="web"):
        return rb.ensure_ready(github, "https://github.com/acme/web", "dev", role=role, title="Acme", templates=TEMPLATES, config=CONFIG)

    def test_an_empty_repository_gets_one_bootstrap_pr(self):
        github = FakeGitHub({"README.md": "# web\n", ".gitignore": "node_modules\n"})
        first = self.ensure(github)
        self.assertFalse(first.ready)
        self.assertTrue(first.opened)
        self.assertEqual((first.template, first.pr_url), ("react-web", "https://github.com/acme/web/pull/1"))
        self.assertIn("starts from the `react-web` template. Opened the bootstrap PR", first.message())
        paths = {entry["path"] for entry in github.tree_body["tree"]}
        self.assertEqual(github.tree_body["base_tree"], "base-tree")
        self.assertIn("features/EXAMPLE-1-notes.feature", paths)
        self.assertIn("Once merged, TS Harness makes `checks`, `bdd`, `docker` required checks", github.pulls[0]["body"])
        # Asked again while the PR is open: the same PR, nothing new pushed.
        second = self.ensure(github)
        self.assertEqual((second.ready, second.opened, second.pr_url), (False, False, first.pr_url))
        self.assertIn("Waiting on the bootstrap PR", second.message())
        self.assertEqual(len(github.pulls), 1)

    def test_a_repository_with_code_is_left_alone(self):
        github = FakeGitHub({"README.md": "x", "package.json": "{}"})
        self.assertTrue(self.ensure(github).ready)
        self.assertFalse([c for c in github.calls if c[0] == "POST"])

    def test_no_template_for_the_role_means_ready(self):
        github = FakeGitHub({"README.md": "x"})
        self.assertTrue(self.ensure(github, role="worker").ready)
        self.assertFalse(github.calls)

    def test_a_missing_base_branch_says_what_to_create(self):
        with self.assertRaisesRegex(rb.BootstrapError, "has no `dev` branch"):
            self.ensure(FakeGitHub(None))

    def test_after_merge_the_templates_ci_jobs_become_required(self):
        files = {"README.md": "x", "package.json": "{}", rb.MARKER_FILE: "template: react-web\n"}
        workflow = "on:\n  pull_request:\njobs:\n  checks:\n    name: checks\n  bdd:\n    name: bdd\n  docker:\n    name: docker\n"
        github = FakeGitHub(files, workflows={".github/workflows/ci.yml": workflow})
        github.protection = {
            "required_status_checks": {"strict": True, "contexts": ["harness/code-review", "harness/qa", "harness/security-scan"]},
            "required_pull_request_reviews": {"required_approving_review_count": 1, "dismiss_stale_reviews": True},
        }
        result = self.ensure(github)
        self.assertTrue(result.ready)
        self.assertEqual(
            github.set_protection["required_status_checks"]["contexts"],
            ["bdd", "checks", "docker", "harness/code-review", "harness/qa", "harness/security-scan"],
        )
        self.assertIn("made bdd, checks, docker required checks", result.notes[0])
        # Already required: nothing set again.
        github.protection["required_status_checks"]["contexts"] = github.set_protection["required_status_checks"]["contexts"]
        github.set_protection = None
        self.ensure(github)
        self.assertIsNone(github.set_protection)


class AnnotationTests(unittest.TestCase):
    def test_empty_repositories_carry_their_template_for_planning(self):
        repos = [
            {"full_name": "acme/api", "role": "api", "default_branch": "dev"},
            {"full_name": "acme/web", "role": "web", "default_branch": "dev"},
        ]

        class GitHub(FakeGitHub):
            def get_tree(self, owner, repo, ref, recursive=True):
                return {"tree": [{"path": "README.md", "type": "blob"}] + ([{"path": "src/a.ts", "type": "blob"}] if repo == "web" else [])}

        with mock.patch.object(rb, "load_templates", return_value=TEMPLATES), mock.patch.object(rb, "live_settings", return_value=CONFIG):
            out = rb.annotate_repositories(GitHub({}), repos)
        self.assertEqual(out[0]["template"], "node-api")
        self.assertIn("modules/<name>/", out[0]["template_conventions"])
        self.assertNotIn("template", out[1])


class QueueTests(unittest.TestCase):
    def test_a_queue_waits_for_the_bootstrap_prs(self):
        from ts_harness.jira.prepare_ticket_queue import _ensure_project_ready
        from ts_harness.workflows.system_steps import StepBlocked

        memory = mock.Mock()
        memory.list_projects.return_value = {"projects": [{"id": "p1", "name": "Acme"}]}
        memory.list_project_repositories.return_value = {
            "repositories": [{"full_name": "acme/api", "role": "api"}, {"full_name": "acme/web", "role": "web"}]
        }
        pending = rb.Readiness(repo="acme/api", ready=False, template="node-api", pr_url="https://github.com/acme/api/pull/1", opened=True)
        done = rb.Readiness(repo="acme/web", ready=True)
        ready = mock.Mock(return_value=[pending, done])
        with self.assertRaises(StepBlocked) as caught:
            _ensure_project_ready(memory, {"tenant_id": "t1"}, "ACME", ready)
        text = str(caught.exception)
        self.assertIn("acme/api is empty, so it starts from the `node-api` template. Opened the bootstrap PR", text)
        self.assertIn("say `implement all ACME` again", text)
        self.assertNotIn("acme/web", text)
        self.assertEqual(ready.call_args.kwargs["title"], "Acme")
        ready.return_value = [done]
        _ensure_project_ready(memory, {"tenant_id": "t1"}, "ACME", ready)


if __name__ == "__main__":
    unittest.main()


class SettingsValidationTests(unittest.TestCase):
    def test_repo_templates_and_jira_tracking_are_accepted_settings(self):
        from ts_harness.config.runtime_settings import _canonicalize_program_defaults

        issues: list[dict[str, str]] = []
        out = _canonicalize_program_defaults(
            {
                "repo_templates": {"enabled": True, "by_role": {"Worker": "node-api", "web": "none"}},
                "jira_tracking": {"start_transition": "In Progress", "done_transition": "Closed"},
            },
            path="$",
            issues=issues,
        )
        self.assertEqual(issues, [])
        self.assertEqual(out["repo_templates"], {"enabled": True, "by_role": {"worker": "node-api", "web": "none"}})
        self.assertEqual(out["jira_tracking"], {"start_transition": "In Progress", "done_transition": "Closed"})
        self.assertEqual(rb.settings(out)["by_role"]["worker"], "node-api")

        _canonicalize_program_defaults({"repo_templates": {"by_role": ["api"], "colour": 1}}, path="$", issues=issues)
        self.assertEqual({i["code"] for i in issues} >= {"invalid_map"}, True)
        self.assertTrue(any("colour" in i["message"] for i in issues))
