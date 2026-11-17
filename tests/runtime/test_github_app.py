"""GitHub App tokens, per-host webhooks, installation tracking and worker token refresh."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.pg_support import PgTestCase, requires_pg

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from memory_routes import coding_jobs, directory  # noqa: E402
from memory_routes import github_app as github_routes  # noqa: E402

from ts_harness.coding.github_tokens import JobTokenRejected, NoAppForRepository, mint_job_github_token  # noqa: E402
from ts_harness.coding.worker import GitHubTokens  # noqa: E402
from ts_harness.gateway import github_bff, github_webhook  # noqa: E402
from ts_harness.gateway.auth import AuthContext  # noqa: E402
from ts_harness.gateway.projects_bff import ProjectAccessDenied  # noqa: E402
from ts_harness.identity_directory.auth0_management import _github_login  # noqa: E402
from ts_harness.integrations import github_app, github_client  # noqa: E402
from ts_harness.integrations.github_app import GitHubApp, NotInstalled, api_base_for_host, app_jwt  # noqa: E402
from ts_harness.integrations.github_client import GitHubClient, GitHubError, _retry_wait  # noqa: E402

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
PUBLIC_PEM = _KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
).decode()


class FakeGitHub:
    """Records calls and answers the App endpoints."""

    def __init__(self, installed: dict[str, int] | None = None, expires_at: str = "2030-01-01T00:00:00Z"):
        self.installed = installed if installed is not None else {"acme/web": 7}
        self.expires_at = expires_at
        self.calls: list[tuple[str, str, dict[str, str], dict | None]] = []
        self.minted = 0

    def __call__(self, method, url, headers, data):
        body = json.loads(data) if data else None
        self.calls.append((method, url, headers, body))
        path = url.split("api.github.com", 1)[-1].split("/api/v3", 1)[-1]
        if path.startswith("/repos/") and path.endswith("/installation"):
            slug = path[len("/repos/") : -len("/installation")]
            if slug in self.installed:
                return 200, json.dumps({"id": self.installed[slug]}).encode(), {}
            return 404, b'{"message": "Not Found"}', {}
        if path.endswith("/access_tokens"):
            self.minted += 1
            return 201, json.dumps({"token": f"ghs_{self.minted}", "expires_at": self.expires_at}).encode(), {}
        return 404, b"{}", {}


class AppTokenTests(unittest.TestCase):
    def test_jwt_is_rs256_backdated_and_short(self):
        token = app_jwt("42", PRIVATE_PEM, now=1_000_000)
        claims = jwt.decode(token, PUBLIC_PEM, algorithms=["RS256"], options={"verify_exp": False})
        self.assertEqual(claims, {"iat": 999_940, "exp": 1_000_480, "iss": "42"})

    def test_host_maps_to_api_base(self):
        self.assertEqual(api_base_for_host("github.com"), "https://api.github.com")
        self.assertEqual(api_base_for_host("GHE.Acme.dev"), "https://ghe.acme.dev/api/v3")

    def test_token_is_scoped_to_one_repository_and_cached(self):
        fake = FakeGitHub()
        clock = [1_000.0]
        app = GitHubApp("42", PRIVATE_PEM, transport=fake, clock=lambda: clock[0])
        self.assertEqual(app.repo_token("acme", "web"), "ghs_1")
        self.assertEqual(app.repo_token("Acme", "Web"), "ghs_1")
        mint = [c for c in fake.calls if c[1].endswith("/access_tokens")]
        self.assertEqual(len(mint), 1)
        self.assertIn("/app/installations/7/access_tokens", mint[0][1])
        self.assertEqual(mint[0][3], {"repositories": ["web"]})
        self.assertTrue(mint[0][2]["Authorization"].startswith("Bearer ey"))

    def test_token_is_reminted_before_expiry(self):
        fake = FakeGitHub(expires_at="1970-01-01T01:00:00Z")
        clock = [3600 - 400.0]
        app = GitHubApp("42", PRIVATE_PEM, transport=fake, clock=lambda: clock[0])
        app.repo_token("acme", "web")
        clock[0] = 3600 - 200.0
        self.assertEqual(app.repo_token("acme", "web"), "ghs_2")

    def test_missing_installation_is_cached_briefly(self):
        fake = FakeGitHub(installed={})
        clock = [0.0]
        app = GitHubApp("42", PRIVATE_PEM, transport=fake, clock=lambda: clock[0])
        for _ in range(2):
            with self.assertRaises(NotInstalled):
                app.repo_token("acme", "web")
        lookups = [c for c in fake.calls if c[1].endswith("/installation")]
        self.assertEqual(len(lookups), 1)
        clock[0] = github_app.MISSING_INSTALLATION_TTL + 1
        with self.assertRaises(NotInstalled):
            app.repo_token("acme", "web")
        self.assertEqual(len([c for c in fake.calls if c[1].endswith("/installation")]), 2)


class ClientTests(unittest.TestCase):
    def setUp(self):
        github_app.reset_cache()
        self.addCleanup(github_app.reset_cache)
        github_client.RATE_LIMITS.clear()

    def test_retry_wait_rules(self):
        self.assertEqual(_retry_wait(429, {"retry-after": "3"}, 0), 3.0)
        self.assertEqual(_retry_wait(429, {"retry-after": "600"}, 0), github_client.MAX_RETRY_WAIT)
        self.assertEqual(_retry_wait(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "110"}, 100), 10.0)
        self.assertEqual(_retry_wait(429, {}, 0), 5.0)
        self.assertIsNone(_retry_wait(403, {}, 0))
        self.assertIsNone(_retry_wait(500, {"retry-after": "1"}, 0))

    def test_throttled_call_is_retried_and_rate_limit_recorded(self):
        responses = [
            (429, b"slow down", {"retry-after": "2"}),
            (200, b'{"number": 5}', {"x-ratelimit-remaining": "4999", "x-ratelimit-limit": "5000", "x-ratelimit-reset": "9"}),
        ]
        sleeps: list[float] = []
        client = GitHubClient(token="pat", transport=lambda *a: responses.pop(0), sleep=sleeps.append)
        self.assertEqual(client.get_pull("acme", "web", 5), {"number": 5})
        self.assertEqual(sleeps, [2.0])
        self.assertEqual(github_client.RATE_LIMITS["github.com"]["remaining"], 4999)

    def test_gives_up_after_two_retries(self):
        sleeps: list[float] = []
        client = GitHubClient(token="pat", transport=lambda *a: (429, b"no", {"retry-after": "1"}), sleep=sleeps.append)
        with self.assertRaises(GitHubError):
            client.get_pull("acme", "web", 5)
        self.assertEqual(sleeps, [1.0, 1.0])

    def test_app_token_for_repo_paths_and_pat_elsewhere(self):
        seen: list[str] = []

        def transport(method, url, headers, data):
            seen.append(headers["Authorization"])
            return 200, b"{}", {}

        client = GitHubClient(token="pat", transport=transport, token_provider=lambda o, r: f"app-{o}-{r}")
        client.request("GET", "/repos/acme/web/pulls/1")
        client.request("GET", "/user")
        self.assertEqual(seen, ["Bearer app-acme-web", "Bearer pat"])
        with self.assertRaises(GitHubError):
            GitHubClient(token="", transport=transport, token_provider=lambda o, r: "x").request("GET", "/user")

    def test_github_for_uses_env_app_and_falls_back_to_pat(self):
        env = {"GITHUB_APP_ID": "42", "GITHUB_APP_PRIVATE_KEY": PRIVATE_PEM.replace("\n", "\\n"), "GITHUB_TOKEN": "pat"}
        fake = FakeGitHub()
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
            github_app, "_stored_registration", return_value=None
        ):
            app = github_app.app_for("github.com")
            self.assertIsNotNone(app)
            self.assertEqual(app.private_key, PRIVATE_PEM)
            app.transport = fake
            client = github_client.github_for("https://github.com/acme/web.git")
            self.assertEqual(client.token_provider("acme", "web"), "ghs_1")
            self.assertEqual(client.token_provider("other", "repo"), "pat")
            self.assertEqual(github_client.repo_token("https://github.com/acme/web"), "ghs_1")
            self.assertEqual(github_client.repo_token("https://github.com/other/repo"), "pat")

    def test_github_for_without_app_or_pat_fails(self):
        with mock.patch.dict(os.environ, {"GITHUB_APP_ID": "", "GITHUB_TOKEN": ""}), mock.patch.object(
            github_app, "_stored_registration", return_value=None
        ):
            with self.assertRaises(GitHubError):
                github_client.github_for("https://github.com/acme/web")
            self.assertEqual(github_client.repo_token("https://github.com/acme/web"), "")

    def test_enterprise_host_uses_stored_registration(self):
        stored = github_app.Registration(host="ghe.acme.dev", app_id="9", private_key=PRIVATE_PEM, webhook_secret="w")
        with mock.patch.dict(os.environ, {"GITHUB_APP_ID": "", "GITHUB_TOKEN": ""}), mock.patch.object(
            github_app, "_stored_registration", side_effect=lambda h: stored if h == "ghe.acme.dev" else None
        ):
            client = github_client.github_for("https://ghe.acme.dev/acme/web")
            self.assertEqual((client.host, client.api_base), ("ghe.acme.dev", "https://ghe.acme.dev/api/v3"))
            self.assertEqual(github_webhook.webhook_secrets("ghe.acme.dev"), ["w"])


class WebhookTests(unittest.TestCase):
    def setUp(self):
        github_webhook._handled_deliveries.clear()
        github_app.reset_cache()
        self.addCleanup(github_app.reset_cache)

    def test_delivery_host_and_secrets(self):
        self.assertEqual(github_webhook.delivery_host({}), "github.com")
        self.assertEqual(github_webhook.delivery_host({"X-GitHub-Enterprise-Host": "GHE.acme.dev"}), "ghe.acme.dev")
        registration = github_app.Registration(host="github.com", app_id="1", private_key="k", webhook_secret="app")
        with mock.patch.object(github_app, "registration_for", return_value=registration), mock.patch.dict(
            os.environ, {"GITHUB_WEBHOOK_SECRET": "legacy"}
        ):
            secrets = github_webhook.webhook_secrets("github.com")
        self.assertEqual(secrets, ["app", "legacy"])
        import hashlib
        import hmac

        body = b'{"a": 1}'
        signature = "sha256=" + hmac.new(b"legacy", body, hashlib.sha256).hexdigest()
        self.assertTrue(github_webhook.verify_delivery(body, signature, secrets))
        self.assertFalse(github_webhook.verify_delivery(body, signature, ["app"]))

    def test_dedupe_consults_the_shared_table(self):
        memory = mock.MagicMock()
        memory.github_delivery_seen.return_value = True
        self.assertTrue(github_webhook.delivery_already_handled("d1", memory=memory))
        memory.github_delivery_seen.return_value = False
        self.assertFalse(github_webhook.delivery_already_handled("d2", memory=memory))
        github_webhook.remember_delivery("d2", host="ghe.acme.dev", event="push", memory=memory)
        memory.record_github_delivery.assert_called_once_with("d2", host="ghe.acme.dev", event="push")
        memory.github_delivery_seen.reset_mock()
        self.assertTrue(github_webhook.delivery_already_handled("d2", memory=memory))
        memory.github_delivery_seen.assert_not_called()

    def _installation_body(self, action: str, **extra):
        return {
            "action": action,
            "installation": {"id": 7, "account": {"login": "acme", "type": "Organization"}, "repository_selection": "selected"},
            **extra,
        }

    def test_installation_created_and_deleted(self):
        memory = mock.MagicMock()
        body = self._installation_body("created", repositories=[{"full_name": "acme/web"}])
        with mock.patch.object(github_webhook, "get_memory_client", return_value=memory):
            out = github_webhook.handle_github_webhook(body, event_name="installation", host="github.com")
            self.assertEqual(out["status"], "installation_synced")
            kwargs = memory.upsert_github_installation.call_args.kwargs
            self.assertEqual((kwargs["installation_id"], kwargs["repositories"], kwargs["suspended"]), (7, ["acme/web"], False))
            out = github_webhook.handle_github_webhook(self._installation_body("deleted"), event_name="installation")
        self.assertEqual(out["status"], "installation_deleted")
        memory.delete_github_installation.assert_called_once_with("github.com", 7)

    def test_installation_repositories_change(self):
        memory = mock.MagicMock()
        body = self._installation_body(
            "added", repositories_added=[{"full_name": "acme/api"}], repositories_removed=[{"full_name": "acme/old"}]
        )
        with mock.patch.object(github_webhook, "get_memory_client", return_value=memory):
            out = github_webhook.handle_github_webhook(body, host="ghe.acme.dev")
        self.assertEqual(out["status"], "installation_updated")
        memory.change_github_installation_repositories.assert_called_once_with(
            "ghe.acme.dev", 7, added=["acme/api"], removed=["acme/old"]
        )

    def test_revise_comment_and_unmerged_close_route_to_feedback(self):
        comment = {
            "action": "created",
            "issue": {"number": 3, "html_url": "https://github.com/acme/web/pull/3", "pull_request": {}},
            "comment": {"body": "/revise please fix the tests", "user": {"login": "dana"}},
            "repository": {"full_name": "acme/web"},
        }
        self.assertEqual(github_webhook.parse_github_event(comment)["kind"], "github.pr_comment")
        self.assertIsNone(github_webhook.parse_github_event({**comment, "comment": {"body": "looks good"}}))
        with mock.patch.object(github_webhook, "_handle_pr_feedback", return_value={"status": "rejected"}) as feedback, \
                mock.patch.object(github_webhook, "notify_subscribers", return_value=[]):
            self.assertEqual(github_webhook.handle_github_webhook(comment)["status"], "rejected")
            closed = {
                "action": "closed",
                "pull_request": {"number": 3, "merged": False, "html_url": "https://github.com/acme/web/pull/3"},
                "repository": {"full_name": "acme/web"},
            }
            github_webhook.handle_github_webhook(closed, event_name="pull_request")
        self.assertEqual([c.args[0]["kind"] for c in feedback.call_args_list], ["github.pr_comment", "github.pr_closed"])


class JobTokenTests(unittest.TestCase):
    def test_mints_for_the_jobs_repository_only(self):
        app = GitHubApp("42", PRIVATE_PEM, transport=FakeGitHub())
        out = mint_job_github_token(
            "j1", "tok", identity=lambda j, t: {"repo_url": "https://github.com/acme/web.git"}, app_for=lambda h: app
        )
        self.assertEqual((out["token"], out["repository"]), ("ghs_1", "acme/web"))
        self.assertTrue(out["expires_at"].startswith("2030-01-01"))

    def test_rejections(self):
        with self.assertRaises(JobTokenRejected):
            mint_job_github_token("j1", "", identity=lambda j, t: {}, app_for=lambda h: None)
        with self.assertRaises(NoAppForRepository):
            mint_job_github_token("j1", "t", identity=lambda j, t: {"repo_url": "https://github.com/a/b"}, app_for=lambda h: None)
        app = GitHubApp("42", PRIVATE_PEM, transport=FakeGitHub(installed={}))
        with self.assertRaises(NoAppForRepository):
            mint_job_github_token("j1", "t", identity=lambda j, t: {"repo_url": "https://github.com/a/b"}, app_for=lambda h: app)


class WorkerTokenTests(unittest.TestCase):
    def test_refreshes_env_only_when_managed_and_near_expiry(self):
        api = mock.MagicMock()
        api.github_token.return_value = {"token": "ghs_a", "expires_at": "1970-01-01T01:00:00Z"}
        env: dict[str, str] = {"GITHUB_TOKEN": "pat"}
        clock = [0.0]
        tokens = GitHubTokens(api, environ=env, clock=lambda: clock[0])
        self.assertFalse(tokens.refresh())
        self.assertTrue(tokens.refresh(force=True))
        self.assertEqual((env["GITHUB_TOKEN"], env["GH_TOKEN"]), ("ghs_a", "ghs_a"))
        self.assertFalse(tokens.refresh())
        clock[0] = 3600 - 100
        api.github_token.return_value = {"token": "ghs_b", "expires_at": "1970-01-01T02:00:00Z"}
        self.assertTrue(tokens.refresh())
        self.assertEqual(env["GITHUB_TOKEN"], "ghs_b")

    def test_no_app_keeps_the_pat(self):
        api = mock.MagicMock()
        api.github_token.return_value = None
        env = {"GITHUB_TOKEN": "pat"}
        tokens = GitHubTokens(api, environ=env)
        self.assertFalse(tokens.refresh(force=True))
        self.assertEqual(env, {"GITHUB_TOKEN": "pat"})
        self.assertFalse(tokens.managed)


class AdminBffTests(unittest.TestCase):
    ADMIN = AuthContext(actor_id="admin@acme.dev", tenant_id="default", roles=["admin"])
    VIEWER = AuthContext(actor_id="dev@acme.dev", tenant_id="default", roles=["viewer"])

    def _memory(self):
        memory = mock.MagicMock()
        memory.list_github_registrations.return_value = []
        memory.list_github_installations.return_value = [
            {"host": "github.com", "installation_id": 7, "account_login": "acme", "repositories": ["acme/web"],
             "suspended_at": None, "repository_selection": "selected"},
            {"host": "github.com", "installation_id": 8, "account_login": "gone", "repositories": []},
        ]
        memory.github_webhook_status.return_value = [{"host": "github.com", "last_day": 2}]
        return memory

    def test_overview_reports_app_and_install_link(self):
        registration = github_app.Registration(host="github.com", app_id="42", private_key="k", app_slug="ts-acme")
        with mock.patch.object(github_app, "registration_for", return_value=registration), mock.patch.object(
            github_bff, "_is_admin", return_value=False
        ), mock.patch.dict(os.environ, {"GITHUB_APP_HOST": "", "GITHUB_TOKEN": "pat"}):
            out = github_bff.github_overview(self._memory(), self.VIEWER, None)
        self.assertEqual(out["apps"][0]["install_url"], "https://github.com/apps/ts-acme/installations/new")
        self.assertTrue(out["apps"][0]["configured"])
        self.assertEqual(out["installations"][0]["repositories"], ["acme/web"])
        self.assertTrue(out["pat_fallback"])
        self.assertFalse(out["can_sync"])
        self.assertNotIn("private_key", json.dumps(out))

    def test_sync_is_admin_only_and_removes_stale_installations(self):
        memory = self._memory()
        app = mock.MagicMock()
        app.list_installations.return_value = [
            {"id": 7, "account": {"login": "acme", "type": "Organization"}, "repository_selection": "all"}
        ]
        app.installation_repositories.return_value = ["acme/web", "acme/api"]
        with mock.patch.object(github_bff, "_is_admin", return_value=False):
            with self.assertRaises(ProjectAccessDenied):
                github_bff.sync_installations(memory, self.VIEWER, None, {}, app_for=lambda h: app)
        with mock.patch.object(github_bff, "_is_admin", return_value=True):
            out = github_bff.sync_installations(memory, self.ADMIN, None, {"host": "github.com"}, app_for=lambda h: app)
            with self.assertRaises(ValueError):
                github_bff.sync_installations(memory, self.ADMIN, None, {}, app_for=lambda h: None)
        self.assertEqual(out, {"host": "github.com", "installations": 1, "removed": 1})
        self.assertEqual(memory.upsert_github_installation.call_args.kwargs["repositories"], ["acme/web", "acme/api"])
        memory.delete_github_installation.assert_called_once_with("github.com", 8)


class DirectoryGitHubLoginTests(unittest.TestCase):
    def test_auth0_login_only_from_a_github_identity(self):
        self.assertEqual(_github_login({"nickname": "dana", "identities": [{"provider": "github"}]}), "dana")
        self.assertIsNone(_github_login({"nickname": "dana", "identities": [{"provider": "auth0"}]}))

    def test_coding_request_falls_back_to_directory_login(self):
        from ts_harness.implementation import coding_job

        memory = mock.MagicMock()
        memory.directory_user.return_value = {"found": True, "user": {"github_login": "dana-gh"}}
        with mock.patch("ts_harness.memory.client.get_memory_client", return_value=memory):
            self.assertEqual(coding_job._directory_github_login("dana@acme.dev", "default"), "dana-gh")
            self.assertIsNone(coding_job._directory_github_login("agent-runner", "default"))
        memory.directory_user.assert_called_once_with(tenant_id="default", email="dana@acme.dev")


@requires_pg
class GitHubRoutesPostgresTests(PgTestCase):
    def setUp(self):
        with self.db.connect() as conn:
            for table in ("github_webhook_deliveries", "github_installations", "github_app_registrations", "coding_jobs"):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM directory_users WHERE subject = 'auth0|dana'")
            conn.commit()

    def test_registrations_installations_and_deliveries(self):
        with self.db.connect() as conn:
            github_routes.put_registration(
                conn, "GHE.acme.dev", {"app_id": "9", "app_slug": "ts", "private_key_secret_arn": "arn:k", "actor": "a"}
            )
            self.assertEqual(github_routes.get_registration(conn, "ghe.acme.dev")["app_id"], "9")
            self.assertEqual([r["host"] for r in github_routes.list_registrations(conn)["registrations"]], ["ghe.acme.dev"])

            github_routes.upsert_installation(
                conn, {"host": "github.com", "installation_id": 7, "account_login": "acme", "repositories": ["acme/web"]}
            )
            github_routes.change_installation_repositories(
                conn, "github.com", "7", {"added": ["acme/api"], "removed": ["acme/web"]}
            )
            rows = github_routes.list_installations(conn, {"host": "github.com"})["installations"]
            self.assertEqual(rows[0]["repositories"], ["acme/api"])
            github_routes.delete_installation(conn, "github.com", "7", {})
            self.assertEqual(github_routes.list_installations(conn, {})["installations"], [])

            self.assertFalse(github_routes.delivery_seen(conn, "d1")["seen"])
            self.assertFalse(github_routes.record_delivery(conn, {"delivery_id": "d1", "host": "github.com", "event": "push"})["duplicate"])
            self.assertTrue(github_routes.record_delivery(conn, {"delivery_id": "d1", "host": "github.com"})["duplicate"])
            self.assertTrue(github_routes.delivery_seen(conn, "d1")["seen"])
            status = github_routes.webhook_status(conn)["hosts"]
            self.assertEqual((status[0]["host"], status[0]["last_day"]), ("github.com", 1))

    def test_directory_keeps_github_login(self):
        with self.db.connect() as conn:
            directory.upsert_directory_user(conn, {"subject": "auth0|dana", "email": "dana@acme.dev", "github_login": "dana-gh"})
            directory.upsert_directory_user(conn, {"subject": "auth0|dana", "email": "dana@acme.dev"})
            person = directory.get_directory_user(conn, tenant_id="default", email="dana@acme.dev")
        self.assertEqual(person["user"]["github_login"], "dana-gh")

    def test_job_identity_only_for_live_jobs(self):
        with mock.patch.dict(os.environ, {"CODING_JOB_SECRET": "s3cret"}), self.db.connect() as conn:
            job = coding_jobs.create_coding_job(
                conn,
                {"spec": {"spec_version": 1, "repo_url": "https://github.com/acme/web", "base_branch": "main",
                          "work_order_id": "WO-1", "requested_by": "dana@acme.dev"}},
            )
            with self.assertRaises(ValueError):
                coding_jobs.job_identity(conn, job["id"])
            conn.execute("UPDATE coding_jobs SET status = 'running' WHERE id = %s", (job["id"],))
            conn.commit()
            identity = coding_jobs.job_identity(conn, job["id"])
        self.assertEqual((identity["repo_url"], identity["requested_by"]), ("https://github.com/acme/web", "dana@acme.dev"))


if __name__ == "__main__":
    unittest.main()
