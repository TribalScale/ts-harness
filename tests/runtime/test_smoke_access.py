"""The bootstrap gives a linked web repository's nightly smoke run read access to its API and a way to report."""

from __future__ import annotations

import builtins
import unittest
from unittest import mock

from ts_harness.integrations.github_client import GitHubError
from ts_harness.workflows import nightly_smoke, repo_bootstrap as rb, smoke_access as sa


class FakeGitHub:
    """Actions variables and secrets on the web repository, deploy keys on the API repository."""

    def __init__(self, *, deny: tuple[str, ...] = ()):
        self.deny = deny
        self.variables: dict[str, str] = {}
        self.secrets: dict[str, str] = {}  # name -> sealed value
        self.keys: list[dict] = []
        self.calls: list[tuple[str, str]] = []

    def request(self, method, path, *, body=None, query=None, allow_404=False):
        self.calls.append((method, path))
        if any(fragment in path for fragment in self.deny):
            raise GitHubError(403, "Resource not accessible by integration")
        if path == "/repos/org/web/actions/variables" and method == "POST":
            self.variables[body["name"]] = body["value"]
            return {}
        if path.startswith("/repos/org/web/actions/variables/"):
            name = path.rsplit("/", 1)[1]
            if method == "GET":
                return {"name": name, "value": self.variables[name]} if name in self.variables else None
            self.variables[name] = body["value"]
            return {}
        if path == "/repos/org/web/actions/secrets/public-key":
            return {"key_id": "kid-1", "key": "cmVwby1wdWJsaWMta2V5"}
        if path.startswith("/repos/org/web/actions/secrets/"):
            name = path.rsplit("/", 1)[1]
            if method == "GET":
                return {"name": name} if name in self.secrets else None
            assert body["key_id"] == "kid-1"
            self.secrets[name] = body["encrypted_value"]
            return {}
        if path == "/repos/org/api/keys" and method == "GET":
            return list(self.keys)
        if path == "/repos/org/api/keys" and method == "POST":
            self.keys.append({"id": len(self.keys) + 1, **body})
            return self.keys[-1]
        if path.startswith("/repos/org/api/keys/") and method == "DELETE":
            self.keys = [k for k in self.keys if str(k["id"]) != path.rsplit("/", 1)[1]]
            return {}
        raise AssertionError(f"unexpected {method} {path}")


def fake_seal(public_key: str, value: str) -> str:
    """Stands in for the sealed box: records what would be encrypted, for the assertions."""
    return f"sealed[{public_key}]:{value}"


def setup(github, **kwargs):
    kwargs.setdefault("seal", fake_seal)
    kwargs.setdefault("report_url", "https://harness.example/webhooks/smoke")
    kwargs.setdefault("report_secret", "smoke-secret")
    return sa.ensure_smoke_access(github, "org/web", "org/api", **kwargs)


class SmokeAccessTests(unittest.TestCase):
    def test_everything_the_workflow_needs_is_set_up(self):
        github = FakeGitHub()
        notes = setup(github)
        self.assertEqual(github.variables, {"API_REPO": "org/api"})
        (key,) = github.keys
        self.assertEqual((key["title"], key["read_only"]), ("ts-harness nightly smoke: org/web", True))
        self.assertTrue(key["key"].startswith("ssh-ed25519 "))
        self.assertEqual(set(github.secrets), {"API_REPO_DEPLOY_KEY", "HARNESS_SMOKE_URL", "HARNESS_SMOKE_TOKEN"})
        self.assertIn("BEGIN OPENSSH PRIVATE KEY", github.secrets["API_REPO_DEPLOY_KEY"])
        self.assertTrue(github.secrets["API_REPO_DEPLOY_KEY"].startswith("sealed[cmVwby1wdWJsaWMta2V5]:"))
        self.assertTrue(github.secrets["HARNESS_SMOKE_TOKEN"].endswith(nightly_smoke.report_token("org/web", "smoke-secret")))
        text = "\n".join(notes)
        self.assertIn("added a read-only deploy key to org/api", text)
        # Notes go to people and logs: never a secret value.
        self.assertNotIn("PRIVATE KEY", text)
        self.assertNotIn(nightly_smoke.report_token("org/web", "smoke-secret"), text)

    def test_running_again_keeps_the_key_in_place(self):
        github = FakeGitHub()
        setup(github)
        notes = setup(github)
        self.assertEqual(len(github.keys), 1)
        self.assertEqual(sum(1 for c in github.calls if c == ("POST", "/repos/org/api/keys")), 1)
        self.assertIn("deploy key already in place", "\n".join(notes))

    def test_a_key_without_its_secret_is_replaced(self):
        github = FakeGitHub()
        github.keys.append({"id": 9, "title": "ts-harness nightly smoke: org/web", "key": "ssh-ed25519 old", "read_only": True})
        setup(github)
        self.assertEqual(len(github.keys), 1)
        self.assertNotEqual(github.keys[0]["key"], "ssh-ed25519 old")
        self.assertIn(("DELETE", "/repos/org/api/keys/9"), github.calls)

    def test_without_pynacl_only_the_variable_is_set_and_the_runbook_is_named(self):
        github = FakeGitHub()
        with mock.patch.object(sa, "default_sealer", return_value=None):
            notes = sa.ensure_smoke_access(github, "org/web", "org/api")
        self.assertEqual(github.variables, {"API_REPO": "org/api"})
        self.assertEqual((github.keys, github.secrets), ([], {}))
        self.assertIn("PyNaCl", "\n".join(notes))
        self.assertIn("docs/runbooks/nightly-smoke.md", "\n".join(notes))

    def test_default_sealer_is_none_without_pynacl(self):
        real_import = builtins.__import__

        def no_nacl(name, *args, **kwargs):
            if name.startswith("nacl"):
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        with mock.patch.object(builtins, "__import__", no_nacl):
            self.assertIsNone(sa.default_sealer())

    def test_missing_permissions_become_notes_not_errors(self):
        github = FakeGitHub(deny=("/repos/org/api/keys",))
        notes = "\n".join(setup(github))
        self.assertIn("could not set up read access to org/api (the GitHub App lacks the permission)", notes)
        self.assertNotIn("API_REPO_DEPLOY_KEY", github.secrets)
        # The report secrets are independent of the deploy key.
        self.assertIn("HARNESS_SMOKE_TOKEN", github.secrets)

    def test_no_report_url_means_no_report_secrets(self):
        github = FakeGitHub()
        notes = "\n".join(setup(github, report_url=""))
        self.assertNotIn("HARNESS_SMOKE_URL", github.secrets)
        self.assertIn("set SMOKE_REPORT_URL and SMOKE_REPORT_SECRET", notes)

    def test_generated_keys(self):
        private, public = sa.generate_deploy_key("ts-harness-smoke")
        self.assertTrue(private.startswith("-----BEGIN OPENSSH PRIVATE KEY-----"))
        self.assertRegex(public, r"^ssh-ed25519 \S+ ts-harness-smoke$")


class BootstrapSmokeTests(unittest.TestCase):
    def setUp(self):
        rb._smoke_access_done.clear()

    def test_the_smoke_workflow_ships_only_to_a_web_repo_linked_to_its_api(self):
        templates = rb.load_templates()
        linked = rb.render(templates["react-web"], repo_name="acme-web", contract_repo="org/api")
        self.assertIn(rb.SMOKE_WORKFLOW, linked)
        self.assertNotIn(rb.SMOKE_WORKFLOW, rb.render(templates["react-web"], repo_name="acme-web"))
        self.assertEqual(rb.contract_repo_of(linked[rb.MARKER_FILE]), "org/api")
        self.assertIsNone(rb.contract_repo_of("template: react-web\n"))

    def test_an_empty_linked_web_repo_gets_its_smoke_access_with_the_bootstrap_pr(self):
        from tests.runtime.test_repo_bootstrap import CONFIG, TEMPLATES, FakeGitHub as BootstrapGitHub

        github = BootstrapGitHub({"README.md": "# web\n"})
        with mock.patch.object(sa, "ensure_smoke_access", return_value=["org/web nightly smoke: set API_REPO=org/api"]) as access:
            result = rb.ensure_ready(github, "https://github.com/acme/web", "dev", role="web", templates=TEMPLATES,
                                     config=CONFIG, contract_repo="acme/api")
        access.assert_called_once_with(github, "acme/web", "acme/api")
        self.assertIn("org/web nightly smoke: set API_REPO=org/api", result.notes)
        self.assertIn(rb.SMOKE_WORKFLOW, {e["path"] for e in github.tree_body["tree"]})

    def test_a_ready_web_repo_with_the_workflow_is_set_up_once_per_process(self):
        from tests.runtime.test_repo_bootstrap import CONFIG, TEMPLATES, FakeGitHub as BootstrapGitHub

        marker = "template: react-web\ncontract:\n  openapi:\n    repo: acme/api\n    path: openapi.json\n"
        github = BootstrapGitHub({"package.json": "{}", rb.MARKER_FILE: marker, rb.SMOKE_WORKFLOW: "name: Nightly smoke\n"})
        with mock.patch.object(rb, "require_template_checks", return_value=[]), \
                mock.patch.object(sa, "ensure_smoke_access", return_value=["noted"]) as access:
            first = rb.ensure_ready(github, "https://github.com/acme/web", "dev", role="web", templates=TEMPLATES, config=CONFIG)
            rb.ensure_ready(github, "https://github.com/acme/web", "dev", role="web", templates=TEMPLATES, config=CONFIG)
        access.assert_called_once_with(github, "acme/web", "acme/api")
        self.assertIn("noted", first.notes)

    def test_a_failing_setup_never_blocks_the_bootstrap(self):
        from tests.runtime.test_repo_bootstrap import CONFIG, TEMPLATES, FakeGitHub as BootstrapGitHub

        github = BootstrapGitHub({"README.md": "# web\n"})
        with mock.patch.object(sa, "ensure_smoke_access", side_effect=RuntimeError("boom")):
            result = rb.ensure_ready(github, "https://github.com/acme/web", "dev", role="web", templates=TEMPLATES,
                                     config=CONFIG, contract_repo="acme/api")
        self.assertTrue(result.opened)
        self.assertIn("setup failed (RuntimeError)", "\n".join(result.notes))


if __name__ == "__main__":
    unittest.main()
