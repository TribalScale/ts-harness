"""Directory sources, the sync job, SCIM provisioning and disabled-person rejection."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from starlette.testclient import TestClient

from tests.pg_support import PgTestCase, requires_pg
from ts_harness.config.schema import IdentityConfig
from ts_harness.gateway import asgi_app, directory_links, identity
from ts_harness.gateway.auth import AuthError, authenticate_request
from ts_harness.gateway.system_jobs import dispatch_system_job
from ts_harness.identity_directory import DirectoryPerson, DirectorySnapshot, build_source, register_group_resolvers, sync_directory
from ts_harness.identity_directory.auth0_management import Auth0ManagementSource
from ts_harness.identity_directory.google_directory import GoogleDirectorySource
from ts_harness.identity_directory.microsoft_graph import MicrosoftGraphSource
from ts_harness.identity_directory.scim import ScimSource

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from memory_routes import directory, scim  # noqa: E402


def fake_http(routes):
    """``routes`` maps (method, path) to a JSON body or a callable(request) -> body."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = routes.get((request.method, request.url.path))
        if body is None:
            return httpx.Response(404, json={"error": f"no route {request.method} {request.url.path}"})
        if callable(body):
            body = body(request)
        return httpx.Response(200, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def config(**identity_kwargs):
    return SimpleNamespace(
        identity=IdentityConfig(provider="oidc", **identity_kwargs),
        gateway=SimpleNamespace(default_tenant_id="default"),
    )


class Auth0SourceTests(unittest.TestCase):
    def test_users_roles_groups_and_blocked(self):
        def users(request):
            page = int(request.url.params["page"])
            return {"users": [] if page else [
                {"user_id": "auth0|alice", "email": "Alice@Example.com", "name": "Alice", "app_metadata": {"groups": ["ts-harness-team-web"]}},
                {"user_id": "auth0|bob", "email": "bob@example.com", "name": "Bob", "blocked": True},
            ]}

        client, seen = fake_http(
            {
                ("POST", "/oauth/token"): {"access_token": "mgmt-token"},
                ("GET", "/api/v2/users"): users,
                ("GET", "/api/v2/roles"): lambda r: {"roles": [] if int(r.url.params["page"]) else [
                    {"id": "rol_1", "name": "approver"},
                    {"id": "rol_2", "name": "ts-harness-team-payments-approvers"},
                ]},
                ("GET", "/api/v2/roles/rol_1/users"): {"users": [{"user_id": "auth0|alice"}]},
                ("GET", "/api/v2/roles/rol_2/users"): {"users": [{"user_id": "auth0|alice"}]},
            }
        )
        source = Auth0ManagementSource(domain="acme.us.auth0.com", client_id="cid", client_secret="secret", client=client)

        snapshot = source.fetch()

        self.assertEqual((snapshot.match, snapshot.complete), ("subject", True))
        alice, bob = snapshot.people
        self.assertEqual(alice.subject, "auth0|alice")
        self.assertEqual(alice.email, "alice@example.com")
        self.assertEqual(alice.roles, ["approver"])
        self.assertEqual(alice.groups, ["ts-harness-team-payments-approvers", "ts-harness-team-web"])
        self.assertTrue(bob.disabled)
        token_request = json.loads(seen[0].content)
        self.assertEqual(token_request["audience"], "https://acme.us.auth0.com/api/v2/")
        self.assertEqual(seen[1].headers["authorization"], "Bearer mgmt-token")

    def test_from_env_uses_issuer_host(self):
        env = {"AUTH0_MGMT_CLIENT_ID": "cid", "AUTH0_MGMT_CLIENT_SECRET": "secret"}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("AUTH0_MGMT_DOMAIN", None)
            source = Auth0ManagementSource.from_env(config(issuer="https://acme.us.auth0.com/"))
        self.assertEqual(source.domain, "acme.us.auth0.com")
        with mock.patch.dict(os.environ, {"AUTH0_MGMT_CLIENT_ID": ""}):
            self.assertIsNone(Auth0ManagementSource.from_env(config(issuer="https://acme.us.auth0.com/")))


class GraphSourceTests(unittest.TestCase):
    def client(self):
        return fake_http(
            {
                ("POST", "/tenant-1/oauth2/v2.0/token"): {"access_token": "graph-token"},
                ("GET", "/v1.0/users"): lambda r: (
                    {"value": [{"id": "u2", "userPrincipalName": "Bob@corp.com", "accountEnabled": False}]}
                    if "skiptoken" in str(r.url)
                    else {
                        "value": [{"id": "u1", "mail": "alice@corp.com", "displayName": "Alice", "accountEnabled": True}],
                        "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?$skiptoken=abc",
                    }
                ),
                ("GET", "/v1.0/groups"): {"value": [{"id": "g1", "displayName": "ts-harness-team-payments"}]},
                ("GET", "/v1.0/groups/g1/transitiveMembers/microsoft.graph.user"): {"value": [{"id": "u1"}]},
                ("GET", "/v1.0/users/alice@corp.com/transitiveMemberOf/microsoft.graph.group"): {
                    "value": [{"displayName": "ts-harness-team-payments"}, {"displayName": "All staff"}]
                },
            }
        )

    def test_users_follow_next_links_and_match_by_email(self):
        client, _ = self.client()
        source = MicrosoftGraphSource(tenant_id="tenant-1", client_id="cid", client_secret="s", client=client)
        snapshot = source.fetch()
        self.assertEqual(snapshot.match, "email")
        self.assertEqual(
            [(p.email, p.groups, p.disabled) for p in snapshot.people],
            [("alice@corp.com", ["ts-harness-team-payments"], False), ("bob@corp.com", [], True)],
        )

    def test_group_resolver_keeps_harness_groups(self):
        client, _ = self.client()
        source = MicrosoftGraphSource(tenant_id="tenant-1", client_id="cid", client_secret="s", client=client)
        self.assertEqual(
            source.resolve_groups(subject="x", email="alice@corp.com", claims={}), ["ts-harness-team-payments"]
        )
        self.assertEqual(source.resolve_groups(subject="x", email=None, claims={}), [])


class GoogleSourceTests(unittest.TestCase):
    def test_users_groups_and_suspension(self):
        client, seen = fake_http(
            {
                ("GET", "/admin/directory/v1/users"): {
                    "users": [
                        {"primaryEmail": "alice@corp.com", "name": {"fullName": "Alice"}},
                        {"primaryEmail": "bob@corp.com", "suspended": True},
                    ]
                },
                ("GET", "/admin/directory/v1/groups"): lambda r: (
                    {"groups": [{"email": "ts-harness-admins@corp.com"}]}
                    if r.url.params.get("userKey")
                    else {"groups": [{"id": "g1", "email": "ts-harness-team-web-managers@corp.com"}, {"id": "g2", "email": "all@corp.com"}]}
                ),
                ("GET", "/admin/directory/v1/groups/g1/members"): {"members": [{"email": "Alice@corp.com", "type": "USER"}]},
            }
        )
        source = GoogleDirectorySource(admin_subject="admin@corp.com", token_provider=lambda: "google-token", client=client)
        snapshot = source.fetch()
        self.assertEqual(
            [(p.email, p.display_name, p.groups, p.disabled) for p in snapshot.people],
            [("alice@corp.com", "Alice", ["ts-harness-team-web-managers"], False), ("bob@corp.com", None, [], True)],
        )
        self.assertEqual(seen[0].headers["authorization"], "Bearer google-token")
        self.assertEqual(source.resolve_groups(subject="s", email="alice@corp.com", claims={}), ["ts-harness-admins"])


class ScimSourceTests(unittest.TestCase):
    def test_reads_the_memory_snapshot(self):
        memory = mock.MagicMock()
        memory.scim_request.return_value = (
            200,
            {"users": [{"email": "a@corp.com", "roles": [], "groups": ["ts-harness-users"], "disabled": False}]},
        )
        snapshot = ScimSource(memory=memory).fetch()
        self.assertEqual(snapshot.people[0].roles, None)
        self.assertEqual(snapshot.people[0].groups, ["ts-harness-users"])
        memory.scim_request.assert_called_once_with("GET", "snapshot", tenant_id="default")


class FakeSource:
    name = "fake"

    def __init__(self, snapshot):
        self.snapshot = snapshot

    def fetch(self):
        return self.snapshot


class SyncDirectoryTests(unittest.TestCase):
    def test_groups_become_teams_and_roles(self):
        memory = mock.MagicMock()
        memory.directory_snapshot.return_value = {"disabled": ["auth0|gone"], "disable_skipped": False}
        snapshot = DirectorySnapshot(
            source="auth0_management",
            match="subject",
            complete=True,
            people=[
                DirectoryPerson(
                    subject="auth0|alice",
                    email="alice@corp.com",
                    roles=["requester"],
                    groups=["ts-harness-team-payments-approvers", "ts-harness-admins"],
                ),
                DirectoryPerson(subject="auth0|bob", email="bob@corp.com", disabled=True),
            ],
        )
        with mock.patch.object(directory_links, "clear_cache") as clear:
            result = sync_directory(config(), memory=memory, source=FakeSource(snapshot))
        kwargs = memory.directory_snapshot.call_args.kwargs
        alice, bob = kwargs["users"]
        self.assertEqual(alice["teams"], [{"slug": "payments", "role": "approver"}])
        self.assertEqual(alice["roles"], ["requester", "approver", "admin"])
        self.assertNotIn("teams", bob)
        self.assertTrue(bob["disabled"])
        self.assertEqual((kwargs["match"], kwargs["complete"]), ("subject", True))
        self.assertEqual(result["disabled"], ["auth0|gone"])
        clear.assert_called_once()

    def test_seen_users_changes_nothing(self):
        memory = mock.MagicMock()
        result = sync_directory(config(directory="seen_users"), memory=memory)
        self.assertEqual(result["people"], 0)
        memory.directory_snapshot.assert_not_called()

    def test_unconfigured_source_is_skipped(self):
        with mock.patch.dict(os.environ, {"MS_GRAPH_TENANT_ID": ""}):
            result = sync_directory(config(directory="microsoft_graph"), memory=mock.MagicMock())
        self.assertEqual(result["status"], "skipped")

    def test_preset_picks_the_default_source(self):
        env = {"AUTH0_MGMT_CLIENT_ID": "cid", "AUTH0_MGMT_CLIENT_SECRET": "s", "AUTH0_MGMT_DOMAIN": ""}
        with mock.patch.dict(os.environ, env):
            source = build_source(config(preset="auth0", issuer="https://acme.us.auth0.com/"))
        self.assertIsInstance(source, Auth0ManagementSource)

    def test_build_source_for_scim(self):
        memory = mock.MagicMock()
        self.assertIsInstance(build_source(config(directory="scim"), memory=memory), ScimSource)

    def test_system_job(self):
        with mock.patch("ts_harness.identity_directory.sync_directory", return_value={"status": "ok"}) as run:
            self.assertEqual(dispatch_system_job({"handler": "sync_directory"}), {"status": "ok"})
        run.assert_called_once()
        with mock.patch("ts_harness.identity_directory.sync_directory", side_effect=RuntimeError("boom")):
            self.assertEqual(dispatch_system_job({"handler": "sync_directory"})["status"], "error")

    def test_cron_job_is_scheduled(self):
        jobs = json.loads((REPO_ROOT / "config" / "runtime-cron-jobs.json").read_text())["jobs"]
        job = next(j for j in jobs if j["handler"] == "sync_directory")
        self.assertEqual(job["interval_seconds"], 900)
        self.assertIn('"handler": "sync_directory"', (REPO_ROOT / "scripts/phases/15-automation-cron.sh").read_text())


class GroupResolverRegistrationTests(unittest.TestCase):
    def tearDown(self):
        identity.register_group_resolver("microsoft_graph", None)

    def test_registers_graph_resolver_when_configured(self):
        env = {"MS_GRAPH_TENANT_ID": "t", "MS_GRAPH_CLIENT_ID": "c", "MS_GRAPH_CLIENT_SECRET": "s"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(register_group_resolvers(config(groups_source="microsoft_graph")), "microsoft_graph")
        self.assertIsInstance(identity._GROUP_RESOLVERS["microsoft_graph"], MicrosoftGraphSource)

    def test_token_groups_need_no_resolver(self):
        self.assertIsNone(register_group_resolvers(config()))


class DisabledSubjectTests(unittest.TestCase):
    def setUp(self):
        directory_links.clear_cache()

    def tearDown(self):
        directory_links.clear_cache()

    def test_lookup_is_cached_and_fails_open(self):
        memory = mock.MagicMock()
        memory.directory_user.return_value = {"found": True, "user": {"disabled": True}}
        self.assertTrue(directory_links.subject_disabled("default", "auth0|bob", memory=memory))
        self.assertTrue(directory_links.subject_disabled("default", "auth0|bob", memory=memory))
        memory.directory_user.assert_called_once()
        broken = mock.MagicMock()
        broken.directory_user.side_effect = RuntimeError("down")
        self.assertFalse(directory_links.subject_disabled("default", "auth0|carol", memory=broken))

    def test_disabled_person_cannot_authenticate(self):
        provider = mock.Mock()
        provider.validate_token.return_value = {"actor_id": "auth0|bob", "email": "bob@corp.com", "roles": ["member"]}
        headers = {"authorization": "Bearer eyJ.fake.jwt"}
        with mock.patch("ts_harness.gateway.directory_links.subject_disabled", return_value=True):
            with self.assertRaises(AuthError) as ctx:
                authenticate_request(headers, expected_token="gateway-token", identity_provider=provider)
        self.assertEqual(ctx.exception.status, 401)
        with mock.patch("ts_harness.gateway.directory_links.subject_disabled", return_value=False):
            auth = authenticate_request(headers, expected_token="gateway-token", identity_provider=provider)
        self.assertEqual(auth.actor_id, "auth0|bob")

    def test_service_token_skips_the_lookup(self):
        provider = mock.Mock()
        with mock.patch("ts_harness.gateway.directory_links.subject_disabled") as lookup:
            authenticate_request(
                {"authorization": "Bearer gateway-token-value-long-enough"},
                expected_token="gateway-token-value-long-enough",
                identity_provider=provider,
            )
        lookup.assert_not_called()


class ScimRouteTests(unittest.TestCase):
    def setUp(self):
        self.memory = mock.MagicMock()
        self.runtime = mock.Mock()
        self.runtime.config.gateway.default_tenant_id = "default"
        self.patches = [
            mock.patch.object(asgi_app, "get_memory_client", return_value=self.memory),
            mock.patch.object(asgi_app, "get_runtime", return_value=self.runtime),
            mock.patch.dict(os.environ, {"TS_HARNESS_SCIM_TOKEN": "scim-secret"}),
        ]
        for patch in self.patches:
            patch.start()
        self.client = TestClient(asgi_app.app)
        self.auth = {"Authorization": "Bearer scim-secret"}

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()

    def test_scim_is_off_without_a_token(self):
        with mock.patch.dict(os.environ, {"TS_HARNESS_SCIM_TOKEN": ""}):
            self.assertEqual(self.client.get("/scim/v2/Users", headers=self.auth).status_code, 404)

    def test_wrong_token_is_rejected(self):
        response = self.client.get("/scim/v2/Users", headers={"Authorization": "Bearer nope"})
        self.assertEqual(response.status_code, 401)
        self.memory.scim_request.assert_not_called()

    def test_requests_are_forwarded_with_filters(self):
        self.memory.scim_request.return_value = (200, {"totalResults": 0, "Resources": []})
        response = self.client.get('/scim/v2/Users?filter=userName eq "a@corp.com"', headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/scim+json")
        args, kwargs = self.memory.scim_request.call_args
        self.assertEqual(args, ("GET", "Users"))
        self.assertEqual(kwargs["query"]["filter"], 'userName eq "a@corp.com"')

    def test_patch_and_delete(self):
        self.memory.scim_request.return_value = (200, {"id": "u1", "active": False})
        ops = {"Operations": [{"op": "Replace", "path": "active", "value": "False"}]}
        self.assertEqual(self.client.patch("/scim/v2/Users/u1", json=ops, headers=self.auth).json()["active"], False)
        self.assertEqual(self.memory.scim_request.call_args.kwargs["body"], ops)
        self.memory.scim_request.return_value = (200, {"ok": True})
        self.assertEqual(self.client.delete("/scim/v2/Groups/g1", headers=self.auth).status_code, 204)

    def test_errors_pass_through(self):
        self.memory.scim_request.return_value = (409, {"detail": "exists", "status": "409"})
        response = self.client.post("/scim/v2/Users", json={"userName": "a"}, headers=self.auth)
        self.assertEqual(response.status_code, 409)

    def test_service_provider_config(self):
        response = self.client.get("/scim/v2/ServiceProviderConfig", headers=self.auth)
        self.assertTrue(response.json()["patch"]["supported"])


@requires_pg
class DirectorySnapshotPostgresTests(PgTestCase):
    def seed(self, conn, *people):
        for subject, email in people:
            directory.upsert_directory_user(
                conn, {"tenant_id": "snap", "subject": subject, "email": email, "teams": [{"slug": "web"}]}
            )

    def users(self, conn):
        with conn.cursor() as cur:
            cur.execute("SELECT subject, disabled_at IS NOT NULL AS disabled, roles FROM directory_users WHERE tenant_id = 'snap' ORDER BY subject")
            return {r["subject"]: (r["disabled"], list(r["roles"])) for r in cur.fetchall()}

    def memberships(self, conn, subject):
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM team_members WHERE tenant_id = 'snap' AND subject = %s", (subject,))
            return int(cur.fetchone()["n"])

    def test_subject_snapshot_creates_updates_and_disables(self):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM directory_users WHERE tenant_id = 'snap'")
            conn.execute("DELETE FROM team_members WHERE tenant_id = 'snap'")
            conn.commit()
            self.seed(conn, ("a", "a@corp.com"), ("b", "b@corp.com"), ("c", "c@corp.com"), ("d", "d@corp.com"))
            result = directory.apply_directory_snapshot(
                conn,
                {
                    "tenant_id": "snap",
                    "source": "auth0_management",
                    "match": "subject",
                    "complete": True,
                    "users": [
                        {"subject": "a", "email": "a@corp.com", "roles": ["Approver"], "teams": [{"slug": "payments", "role": "approver"}]},
                        {"subject": "b", "email": "b@corp.com", "disabled": True},
                        {"subject": "c", "email": "c@corp.com"},
                        {"subject": "e", "email": "e@corp.com"},
                    ],
                },
            )
            self.assertEqual(result["disabled"], ["b", "d"])
            self.assertEqual((result["created"], result["updated"]), (1, 2))
            users = self.users(conn)
            self.assertEqual(users["a"], (False, ["approver"]))
            self.assertTrue(users["b"][0] and users["d"][0])
            self.assertFalse(users["e"][0])
            self.assertEqual(self.memberships(conn, "b"), 0)
            self.assertEqual(directory.get_directory_user(conn, tenant_id="snap", subject="a")["user"]["teams"], [{"slug": "payments", "role": "approver"}])

            again = directory.apply_directory_snapshot(
                conn, {"tenant_id": "snap", "match": "subject", "users": [{"subject": "b", "email": "b@corp.com"}]}
            )
            self.assertEqual(again["reenabled"], ["b"])

    def test_partial_snapshot_does_not_mass_disable(self):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM directory_users WHERE tenant_id = 'snap'")
            conn.commit()
            self.seed(conn, ("a", "a@corp.com"), ("b", "b@corp.com"), ("c", "c@corp.com"), ("d", "d@corp.com"))
            result = directory.apply_directory_snapshot(
                conn,
                {"tenant_id": "snap", "match": "email", "complete": True, "users": [{"email": "a@corp.com"}, {"email": "x@corp.com"}]},
            )
            self.assertTrue(result["disable_skipped"])
            self.assertEqual((result["would_disable"], result["unknown"]), (3, 1))
            self.assertFalse(any(disabled for disabled, _ in self.users(conn).values()))

    def test_disable_by_email(self):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM directory_users WHERE tenant_id = 'snap'")
            conn.commit()
            self.seed(conn, ("a", "a@corp.com"))
            self.assertEqual(directory.disable_directory_users(conn, {"tenant_id": "snap", "emails": ["A@corp.com"]})["disabled"], ["a"])
            self.assertTrue(self.users(conn)["a"][0])


@requires_pg
class ScimPostgresTests(PgTestCase):
    def test_user_and_group_lifecycle(self):
        with self.db.connect() as conn:
            directory.upsert_directory_user(conn, {"tenant_id": "default", "subject": "auth0|ann", "email": "ann@corp.com"})
            ann = scim.create_user(
                conn,
                tenant_id="default",
                data={"userName": "ann@corp.com", "externalId": "oid-1", "displayName": "Ann", "emails": [{"value": "Ann@corp.com", "primary": True}]},
            )
            with self.assertRaises(scim.ScimError) as ctx:
                scim.create_user(conn, tenant_id="default", data={"userName": "ANN@corp.com"})
            self.assertEqual(ctx.exception.status, 409)
            found = scim.list_users(conn, tenant_id="default", filter='userName eq "ann@corp.com"')
            self.assertEqual(found["totalResults"], 1)

            group = scim.create_group(conn, tenant_id="default", data={"displayName": "ts-harness-team-payments"})
            group = scim.patch_group(
                conn,
                tenant_id="default",
                group_id=group["id"],
                data={"Operations": [{"op": "Add", "path": "members", "value": [{"value": ann["id"]}]}]},
            )
            self.assertEqual(group["members"], [{"value": ann["id"]}])
            snap = scim.snapshot(conn, tenant_id="default")["users"]
            self.assertEqual(snap[0]["groups"], ["ts-harness-team-payments"])

            patched = scim.patch_user(
                conn, tenant_id="default", user_id=ann["id"], data={"Operations": [{"op": "Replace", "path": "active", "value": "False"}]}
            )
            self.assertFalse(patched["active"])
            self.assertTrue(directory.get_directory_user(conn, tenant_id="default", subject="auth0|ann")["user"]["disabled"])

            scim.patch_group(
                conn,
                tenant_id="default",
                group_id=group["id"],
                data={"Operations": [{"op": "Remove", "path": f'members[value eq "{ann["id"]}"]'}]},
            )
            self.assertEqual(scim.get_group(conn, tenant_id="default", group_id=group["id"])["members"], [])
            scim.delete_user(conn, tenant_id="default", user_id=ann["id"])
            with self.assertRaises(scim.ScimError):
                scim.get_user(conn, tenant_id="default", user_id=ann["id"])

    def test_unsupported_filter_is_rejected(self):
        with self.db.connect() as conn:
            with self.assertRaises(scim.ScimError):
                scim.list_users(conn, tenant_id="default", filter='name.givenName sw "A"')


if __name__ == "__main__":
    unittest.main()
