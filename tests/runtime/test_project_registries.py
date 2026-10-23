"""Projects, package registries, and how coding jobs receive their credentials."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.pg_support import PgTestCase, requires_pg

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from memory_routes import coding_jobs, projects  # noqa: E402

from ts_harness.gateway import projects_bff as bff  # noqa: E402
from ts_harness.gateway.auth import AuthContext  # noqa: E402

SPEC = {"spec_version": 1, "repo_url": "https://github.com/acme/app", "base_branch": "main", "work_order_id": "WO-1"}


class RegistryUrlTests(unittest.TestCase):
    def test_https_only_and_no_embedded_credentials(self):
        self.assertEqual(
            projects.normalize_registry_url("npm", "https://npm.acme.dev/repo/"), "https://npm.acme.dev/repo"
        )
        self.assertEqual(projects.normalize_registry_url("docker", "registry.acme.dev"), "https://registry.acme.dev")
        for bad in ("http://npm.acme.dev", "https://user:pw@npm.acme.dev", "", "ftp://x"):
            with self.assertRaises(ValueError):
                projects.normalize_registry_url("npm", bad)
        with mock.patch.object(projects, "get_project", return_value={"id": "p"}):
            with self.assertRaisesRegex(ValueError, "type must be"):
                projects.create_registry(mock.MagicMock(), "p", {"type": "cargo", "url": "https://x.dev"})

    def test_sizing_follows_fargate_pairs(self):
        self.assertEqual(projects._sizing({}), (None, None))
        self.assertEqual(projects._sizing({"worker_cpu": 2048, "worker_memory_mb": 8192}), (2048, 8192))
        for body in ({"worker_cpu": 2048}, {"worker_cpu": 1500, "worker_memory_mb": 4096},
                     {"worker_cpu": 1024, "worker_memory_mb": 16384}):
            with self.assertRaises(ValueError):
                projects._sizing(body)


@requires_pg
class ProjectsPostgresTests(PgTestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"CODING_JOB_SECRET": "s3cret"})
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.db.connect() as conn:
            conn.execute("DELETE FROM coding_jobs")
            conn.execute("DELETE FROM project_registries")
            conn.execute("DELETE FROM projects")
            conn.execute("DELETE FROM teams")
            conn.execute("INSERT INTO teams (slug, display_name) VALUES ('web', 'Web')")
            conn.commit()

    def test_project_crud_and_sizing(self):
        with self.db.connect() as conn:
            created = projects.create_project(
                conn, {"key": "APP", "name": "App", "team_slug": "web", "worker_cpu": 4096, "worker_memory_mb": 16384}
            )
            self.assertEqual((created["team_slug"], created["worker_cpu"]), ("web", 4096))
            with self.assertRaises(ValueError):
                projects.create_project(conn, {"key": "APP"})
            with self.assertRaises(ValueError):
                projects.create_project(conn, {"key": "X", "team_slug": "ghost"})
            updated = projects.update_project(conn, created["id"], {"worker_memory_mb": 30720, "team_slug": None})
            self.assertEqual((updated["worker_memory_mb"], updated["team_slug"]), (30720, None))
            with self.assertRaises(ValueError):
                projects.update_project(conn, created["id"], {"worker_memory_mb": 2048})
            self.assertEqual([p["key"] for p in projects.list_projects(conn, {"key": "APP"})["projects"]], ["APP"])
            with self.assertRaises(LookupError):
                projects.get_project(conn, "6f1c1f9e-1111-4222-8333-444455556666")

    def test_registries_hosts_and_job_credentials(self):
        with self.db.connect() as conn:
            project = projects.create_project(conn, {"key": "APP"})
            reg = projects.create_registry(
                conn, project["id"], {"type": "npm", "url": "https://npm.acme.dev/", "scope": "@acme",
                                      "secret_arn": "arn:secret:npm"}
            )
            self.assertTrue(reg["has_credential"])
            projects.create_registry(conn, project["id"], {"type": "pypi", "url": "https://pypi.acme.dev/simple"})
            with self.assertRaises(ValueError):
                projects.create_registry(conn, project["id"], {"type": "npm", "url": "https://npm.acme.dev"})
            self.assertEqual(projects.registry_hosts(conn)["hosts"], ["npm.acme.dev", "pypi.acme.dev"])

            tested = projects.record_registry_test(conn, project["id"], reg["id"], {"ok": False, "error": "HTTP 401"})
            self.assertEqual((tested["last_test_ok"], tested["last_test_error"]), (False, "HTTP 401"))
            reset = projects.update_registry(conn, project["id"], reg["id"], {"scope": "@acme-corp"})
            self.assertIsNone(reset["last_test_ok"])

            job = coding_jobs.create_coding_job(conn, {"spec": SPEC, "project_id": "APP"})
            with self.assertRaises(ValueError):
                projects.job_registries(conn, job["id"], fetch_secret=lambda _: "")
            coding_jobs.claim_coding_jobs(conn, {"max_running": 2})
            fetched = []

            def fetch(arn):
                fetched.append(arn)
                return json.dumps({"username": "bot", "token": "t0k"})

            regs = projects.job_registries(conn, job["id"], fetch_secret=fetch)["registries"]
            self.assertEqual(fetched, ["arn:secret:npm"])
            npm = next(r for r in regs if r["type"] == "npm")
            self.assertEqual((npm["token"], npm["username"], npm["scope"]), ("t0k", "bot", "@acme-corp"))
            self.assertNotIn("token", next(r for r in regs if r["type"] == "pypi"))

            deleted = projects.delete_registry(conn, project["id"], reg["id"], {})
            self.assertEqual(deleted["secret_arn"], "arn:secret:npm")
            self.assertEqual(projects.registry_hosts(conn)["hosts"], ["pypi.acme.dev"])

    def test_claimed_jobs_carry_project_sizing(self):
        with self.db.connect() as conn:
            projects.create_project(conn, {"key": "BIG", "worker_cpu": 8192, "worker_memory_mb": 32768})
            coding_jobs.create_coding_job(conn, {"spec": SPEC, "project_id": "BIG"})
            claimed = coding_jobs.claim_coding_jobs(conn, {"max_running": 2})["jobs"]
            self.assertEqual((claimed[0]["worker_cpu"], claimed[0]["worker_memory_mb"]), (8192, 32768))


class GatewayWorkerAuthTests(unittest.TestCase):
    def test_job_token_can_read_its_registries_only(self):
        spec = importlib.util.spec_from_file_location("memory_gateway_pr", REPO_ROOT / "scripts" / "memory-gateway.py")
        gateway = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gateway)
        job = "6f1c1f9e-1111-4222-8333-444455556666"
        with mock.patch.dict(os.environ, {"CODING_JOB_SECRET": "s3cret", "MEMORY_API_TOKEN": "api"}):
            token = coding_jobs.issue_job_token(job)
            handler = object.__new__(gateway.MemoryHandler)
            handler.path = f"/coding-jobs/{job}/registries"
            handler.headers = {"Authorization": f"Bearer {token}"}
            self.assertTrue(handler._coding_job_worker_authorized("GET"))
            handler.path = "/projects"
            self.assertFalse(handler._coding_job_worker_authorized("GET"))


class FakeMemory:
    def __init__(self):
        self.projects = {
            "p1": {"id": "p1", "tenant_id": "default", "key": "APP", "team_slug": "web"},
            "p2": {"id": "p2", "tenant_id": "other", "key": "OTHER", "team_slug": "web"},
        }
        self.registries: dict[str, dict] = {}
        self.calls: list[tuple] = []

    def get_project(self, pid):
        if pid not in self.projects:
            raise LookupError(pid)
        return self.projects[pid]

    def list_projects(self, *, tenant_id):
        return {"projects": [p for p in self.projects.values() if p["tenant_id"] == tenant_id]}

    def update_project(self, pid, **fields):
        self.calls.append(("update_project", pid, fields))
        return {**self.projects[pid], **{k: v for k, v in fields.items() if k != "actor"}}

    def list_project_registries(self, pid):
        return {"registries": [r for r in self.registries.values() if r["project_id"] == pid]}

    def create_project_registry(self, pid, **fields):
        rid = f"r{len(self.registries) + 1}"
        self.registries[rid] = {"id": rid, "project_id": pid, "secret_arn": None, **fields}
        return self.registries[rid]

    def update_project_registry(self, pid, rid, **fields):
        self.registries[rid].update({k: v for k, v in fields.items() if k != "actor"})
        return self.registries[rid]

    def delete_project_registry(self, pid, rid, *, actor):
        return self.registries.pop(rid)

    def record_project_registry_test(self, pid, rid, *, ok, error, actor):
        self.registries[rid].update(last_test_ok=ok, last_test_error=error)
        return self.registries[rid]

    def registry_hosts(self):
        return sorted({r["url"].split("/")[2] for r in self.registries.values()})


class FakeSecrets:
    def __init__(self, fail=False):
        self.store: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.fail = fail

    def put(self, name, payload):
        if self.fail:
            raise RuntimeError("denied")
        self.store[name] = payload
        return f"arn:{name}"

    def get(self, ref):
        return self.store[ref.removeprefix("arn:")]

    def delete(self, ref):
        self.deleted.append(ref)


def _auth(teams=(), roles=()):
    return AuthContext(actor_id="u1", tenant_id="default", roles=list(roles), teams=list(teams))


class ProjectsBffTests(unittest.TestCase):
    def setUp(self):
        self.memory = FakeMemory()
        self.secrets = FakeSecrets()
        self.synced: list[int] = []
        self.manager = _auth(teams=[{"slug": "web", "role": "manager"}])
        self.member = _auth(teams=[{"slug": "web", "role": "member"}])
        patcher = mock.patch.object(bff, "_is_admin", side_effect=lambda auth, _c: "admin" in (auth.roles or []))
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"SECRETS_PREFIX": "acme"})
        env.start()
        self.addCleanup(env.stop)

    def sync(self, _memory):
        self.synced.append(1)
        return {"managed": True}

    def create(self, auth, body):
        return bff.create_registry_admin(self.memory, auth, None, "p1", body, secrets=self.secrets, sync=self.sync)

    def test_manager_of_owning_team_manages_registries_but_member_cannot(self):
        created = self.create(self.manager, {"type": "npm", "url": "https://npm.acme.dev", "token": "t0k"})
        self.assertNotIn("secret_arn", created)
        self.assertEqual(self.secrets.store, {f"acme/projects/p1/registries/{created['id']}": {"token": "t0k"}})
        self.assertEqual(self.synced, [1])
        listed = bff.list_registries_admin(self.memory, self.manager, None, "p1")["registries"]
        self.assertTrue(all("secret_arn" not in r and "token" not in r for r in listed))
        with self.assertRaises(bff.ProjectAccessDenied):
            self.create(self.member, {"type": "npm", "url": "https://x.dev"})
        with self.assertRaises(bff.ProjectAccessDenied):
            bff.list_registries_admin(self.memory, self.member, None, "p1")

    def test_other_tenant_project_is_not_found(self):
        with self.assertRaises(LookupError):
            bff.list_registries_admin(self.memory, _auth(roles=["admin"]), None, "p2")

    def test_secret_failure_rolls_back_the_registry(self):
        self.secrets.fail = True
        with self.assertRaises(RuntimeError):
            self.create(self.manager, {"type": "npm", "url": "https://npm.acme.dev", "token": "t0k"})
        self.assertEqual(self.memory.registries, {})

    def test_update_clear_and_delete_credentials(self):
        reg = self.create(self.manager, {"type": "pypi", "url": "https://pypi.acme.dev/simple", "token": "a"})
        arn = self.memory.registries[reg["id"]]["secret_arn"]
        bff.update_registry_admin(self.memory, self.manager, None, "p1", reg["id"], {"clear_credential": True},
                                  secrets=self.secrets, sync=self.sync)
        self.assertEqual(self.secrets.deleted, [arn])
        self.assertIsNone(self.memory.registries[reg["id"]]["secret_arn"])
        self.assertEqual(len(self.synced), 1)
        bff.update_registry_admin(self.memory, self.manager, None, "p1", reg["id"],
                                  {"username": "ci", "token": "b"}, secrets=self.secrets, sync=self.sync)
        self.assertEqual(self.secrets.get(self.memory.registries[reg["id"]]["secret_arn"]), {"token": "b", "username": "ci"})
        out = bff.delete_registry_admin(self.memory, self.manager, None, "p1", reg["id"], secrets=self.secrets, sync=self.sync)
        self.assertTrue(out["deleted"])
        self.assertEqual(len(self.secrets.deleted), 2)

    def test_only_admins_create_projects_or_move_teams(self):
        with self.assertRaises(bff.ProjectAccessDenied):
            bff.create_project_admin(self.memory, self.manager, None, {"key": "NEW"})
        with self.assertRaises(bff.ProjectAccessDenied):
            bff.update_project_admin(self.memory, self.manager, None, "p1", {"team_slug": "ops"})
        updated = bff.update_project_admin(self.memory, self.manager, None, "p1", {"worker_cpu": 2048, "worker_memory_mb": 8192})
        self.assertTrue(updated["can_manage"])
        listing = bff.list_projects_admin(self.memory, self.member, None)
        self.assertEqual(([p["can_manage"] for p in listing["projects"]], listing["is_admin"]), ([False], False))

    def test_registry_test_uses_stored_credential_and_proxy(self):
        reg = self.create(self.manager, {"type": "npm", "url": "https://npm.acme.dev", "token": "t0k"})
        seen = {}

        def request(registry, credential, *, proxy):
            seen.update(url=bff.registry_test_url(registry), credential=credential, proxy=proxy)
            return False, "HTTP 401"

        with mock.patch.dict(os.environ, {"CODING_EGRESS_PROXY_URL": "http://proxy:3128"}):
            out = bff.test_registry_admin(self.memory, self.manager, None, "p1", reg["id"], secrets=self.secrets, request=request)
        self.assertEqual(seen, {"url": "https://npm.acme.dev/-/whoami", "credential": {"token": "t0k"}, "proxy": "http://proxy:3128"})
        self.assertEqual((out["last_test_ok"], out["last_test_error"]), (False, "HTTP 401"))

    def test_test_urls_per_type(self):
        self.assertEqual(bff.registry_test_url({"type": "docker", "url": "https://reg.acme.dev/team"}), "https://reg.acme.dev/v2/")
        self.assertEqual(bff.registry_test_url({"type": "pypi", "url": "https://p.dev/simple"}), "https://p.dev/simple")
        self.assertEqual(bff.registry_test_url({"type": "maven", "url": "https://m.dev/repo"}), "https://m.dev/repo/")

    def test_non_aws_secret_store_explains_itself(self):
        with mock.patch.dict(os.environ, {"CLOUD_PROVIDER": "gcp"}):
            with self.assertRaisesRegex(RuntimeError, "Secrets Manager"):
                bff.default_secret_store().put("x", {"token": "y"})


class EgressSyncTests(unittest.TestCase):
    def setUp(self):
        self.memory = FakeMemory()
        self.memory.registries = {"r1": {"id": "r1", "project_id": "p1", "url": "https://npm.acme.dev"}}
        self.ssm = mock.MagicMock()
        self.ecs = mock.MagicMock()

    def test_unmanaged_without_parameter(self):
        with mock.patch.dict(os.environ, {"CODING_EGRESS_DOMAINS_PARAMETER": ""}):
            self.assertEqual(bff.sync_egress_allowlist(self.memory, ssm=self.ssm, ecs=self.ecs), {"managed": False})
        self.ssm.put_parameter.assert_not_called()

    def test_changed_hosts_update_parameter_and_restart_proxy(self):
        env = {"CODING_EGRESS_DOMAINS_PARAMETER": "/p/egress", "CODING_ECS_CLUSTER": "c", "CODING_EGRESS_PROXY_SERVICE": "egress-proxy"}
        self.ssm.get_parameter.return_value = {"Parameter": {"Value": "none"}}
        with mock.patch.dict(os.environ, env):
            out = bff.sync_egress_allowlist(self.memory, ssm=self.ssm, ecs=self.ecs)
            self.assertTrue(out["proxy_restarted"])
            self.ssm.put_parameter.assert_called_once_with(Name="/p/egress", Value="npm.acme.dev", Type="String", Overwrite=True)
            self.ecs.update_service.assert_called_once_with(cluster="c", service="egress-proxy", forceNewDeployment=True)
            self.ssm.get_parameter.return_value = {"Parameter": {"Value": "npm.acme.dev"}}
            self.assertFalse(bff.sync_egress_allowlist(self.memory, ssm=self.ssm, ecs=self.ecs)["changed"])
        self.assertEqual(self.ecs.update_service.call_count, 1)


class ProjectsAsgiTests(unittest.TestCase):
    def setUp(self):
        from starlette.testclient import TestClient

        from ts_harness.gateway import asgi_app

        self.memory = FakeMemory()
        self.auth = _auth(teams=[{"slug": "web", "role": "manager"}])
        patches = [
            mock.patch.object(asgi_app, "get_runtime", return_value=mock.Mock(config=None)),
            mock.patch.object(asgi_app, "_auth", return_value=self.auth),
            mock.patch.object(asgi_app, "_check_origin", return_value=None),
            mock.patch.object(asgi_app, "_require_admin", return_value=None),
            mock.patch.object(asgi_app, "get_memory_client", return_value=self.memory),
            mock.patch.object(bff, "_is_admin", return_value=False),
            mock.patch.object(bff, "default_secret_store", return_value=FakeSecrets()),
            mock.patch.object(bff, "sync_egress_allowlist", return_value={"managed": False}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.client = TestClient(asgi_app.app)

    def test_routes_map_errors_and_hide_credentials(self):
        self.assertEqual(self.client.get("/admin/projects").json()["projects"][0]["key"], "APP")
        created = self.client.post("/admin/projects/p1/registries", json={"type": "npm", "url": "https://n.dev", "token": "t"})
        self.assertEqual(created.status_code, 200)
        self.assertNotIn("secret_arn", created.json())
        rid = created.json()["id"]
        listed = self.client.get("/admin/projects/p1/registries").json()["registries"]
        self.assertEqual([r["id"] for r in listed], [rid])
        self.assertEqual(self.client.post("/admin/projects", json={"key": "X"}).status_code, 403)
        self.assertEqual(self.client.get("/admin/projects/p2/registries").status_code, 404)
        self.assertEqual(self.client.delete(f"/admin/projects/p1/registries/{rid}").json()["deleted"], True)
        self.memory.update_project = mock.Mock(side_effect=RuntimeError(json.dumps({"error": "worker_cpu must be one of"})))
        bad = self.client.put("/admin/projects/p1", json={"worker_cpu": 3})
        self.assertEqual((bad.status_code, bad.json()["error"]), (400, "worker_cpu must be one of"))


if __name__ == "__main__":
    unittest.main()
