"""Private-by-default programs: access levels, enforcement modes, share routes and SQL."""

from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from starlette.testclient import TestClient

from tests.pg_support import PgTestCase, requires_pg
from ts_harness.gateway import asgi_app, program_access
from ts_harness.gateway.program_access import (
    UNLINKED_CHANNEL_MESSAGE,
    ProgramAccessDenied,
    channel_program_refusal,
    check_program_access,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from memory_routes import directory  # noqa: E402
from memory_routes import program_access as sql_access  # noqa: E402

ALICE = {"subject": "auth0|alice", "teams": [], "is_admin": False}


def level(owner=None, visibility="private", grants=(), actor=ALICE):
    return sql_access.compute_level(owner_id=owner, visibility=visibility, grants=list(grants), actor=actor)


def team_actor(slug, role="member"):
    return {"subject": "auth0|bob", "teams": [{"slug": slug, "role": role}], "is_admin": False}


class ComputeLevelTests(unittest.TestCase):
    def test_owner_is_admin(self):
        self.assertEqual(level(owner="auth0|alice"), ("admin", "admin", "owner"))

    def test_private_program_without_grant_is_hidden(self):
        self.assertEqual(level(owner="auth0|zed"), ("none", "none", "none"))

    def test_public_program_is_readable(self):
        self.assertEqual(level(owner="auth0|zed", visibility="public")[0], "read")

    def test_direct_grant(self):
        grants = [{"principal_type": "user", "principal_id": "auth0|alice", "level": "write"}]
        self.assertEqual(level(grants=grants, visibility="public"), ("write", "write", "user"))

    def test_team_grant(self):
        grants = [{"principal_type": "team", "principal_id": "payments", "level": "read"}]
        self.assertEqual(level(grants=grants, actor=team_actor("payments"))[0], "read")
        self.assertEqual(level(grants=grants, actor=team_actor("web"))[0], "none")

    def test_team_approver_is_raised_to_approve_on_write_grants_only(self):
        write = [{"principal_type": "team", "principal_id": "payments", "level": "write"}]
        read = [{"principal_type": "team", "principal_id": "payments", "level": "read"}]
        self.assertEqual(level(grants=write, actor=team_actor("payments", "approver"))[0], "approve")
        self.assertEqual(level(grants=read, actor=team_actor("payments", "approver"))[0], "read")
        self.assertEqual(level(grants=write, actor=team_actor("payments", "member"))[0], "write")

    def test_highest_source_wins(self):
        grants = [
            {"principal_type": "user", "principal_id": "auth0|bob", "level": "read"},
            {"principal_type": "team", "principal_id": "payments", "level": "admin"},
        ]
        self.assertEqual(level(grants=grants, actor=team_actor("payments")), ("admin", "admin", "team"))

    def test_harness_admin_keeps_base_level(self):
        actor = {**ALICE, "is_admin": True}
        self.assertEqual(level(owner="auth0|zed", visibility="public", actor=actor), ("admin", "read", "harness_admin"))


class ParseActorTests(unittest.TestCase):
    def test_query_form(self):
        actor = sql_access.parse_actor(
            {"actor_subject": "auth0|bob", "actor_teams": "Payments:approver,web", "actor_admin": "0"}
        )
        self.assertEqual(
            actor,
            {
                "subject": "auth0|bob",
                "teams": [{"slug": "payments", "role": "approver"}, {"slug": "web", "role": "member"}],
                "is_admin": False,
            },
        )

    def test_missing_actor_is_unfiltered(self):
        self.assertIsNone(sql_access.parse_actor({}))

    def test_list_filter_skips_admins(self):
        self.assertEqual(sql_access.list_filter_clause({**ALICE, "is_admin": True}), ("", []))
        clause, params = sql_access.list_filter_clause(team_actor("payments"))
        self.assertIn("program_access", clause)
        self.assertEqual(params, ["auth0|bob", "auth0|bob", ["payments"]])


def memory_with(info):
    memory = mock.MagicMock()
    memory.program_access_level.return_value = info
    return memory


def info(level_, *, base=None, via="user", tenant="default", found=True):
    return {
        "found": found,
        "tenant_id": tenant,
        "level": level_,
        "base_level": base or level_,
        "via": via,
        "visibility": "private",
        "owner_id": "auth0|zed",
    }


class CheckProgramAccessTests(unittest.TestCase):
    def setUp(self):
        program_access.reset_admin_audit_cache()

    def check(self, memory, need, mode):
        with mock.patch.dict(os.environ, {"TS_HARNESS_PROGRAM_ACCESS": mode}):
            return check_program_access(memory, "p1", ALICE, need, tenant_id="default", route="test")

    def test_enforce_hides_unreadable_programs(self):
        with self.assertRaises(ProgramAccessDenied) as ctx:
            self.check(memory_with(info("none")), "read", "enforce")
        self.assertEqual(ctx.exception.status, 404)

    def test_enforce_forbids_readers_who_need_more(self):
        with self.assertRaises(ProgramAccessDenied) as ctx:
            self.check(memory_with(info("read")), "approve", "enforce")
        self.assertEqual(ctx.exception.status, 403)

    def test_enforce_allows_enough_access(self):
        access = self.check(memory_with(info("approve")), "approve", "enforce")
        self.assertTrue(access.enforced)
        self.assertEqual(access.level, "approve")

    def test_audit_logs_but_allows(self):
        with mock.patch.object(program_access, "log_event") as log:
            access = self.check(memory_with(info("none")), "write", "audit")
        self.assertEqual(access.level, "none")
        self.assertFalse(access.enforced)
        self.assertEqual(log.call_args.args[0], "program_access_would_deny")

    def test_off_skips_the_lookup(self):
        memory = memory_with(info("none"))
        self.assertEqual(self.check(memory, "admin", "off").level, "admin")
        memory.program_access_level.assert_not_called()

    def test_other_tenant_is_not_found_in_every_mode(self):
        for mode in ("audit", "enforce"):
            with self.assertRaises(ProgramAccessDenied):
                self.check(memory_with(info("admin", tenant="other")), "read", mode)

    def test_lookup_failure_is_closed_only_when_enforcing(self):
        memory = mock.MagicMock()
        memory.program_access_level.side_effect = RuntimeError("down")
        self.assertEqual(self.check(memory, "read", "audit").level, "admin")
        with self.assertRaises(ProgramAccessDenied):
            self.check(memory, "read", "enforce")

    def test_service_callers_skip_checks(self):
        memory = memory_with(info("none"))
        with mock.patch.dict(os.environ, {"TS_HARNESS_PROGRAM_ACCESS": "enforce"}):
            self.assertEqual(check_program_access(memory, "p1", None, "admin", tenant_id="default").level, "admin")
        memory.program_access_level.assert_not_called()

    def test_harness_admin_access_is_audited_once_per_hour(self):
        memory = memory_with(info("admin", base="read", via="harness_admin"))
        for _ in range(3):
            self.check(memory, "write", "enforce")
        memory.record_program_admin_access.assert_called_once_with("p1", actor="auth0|alice", route="test", need="write")

    def test_harness_admin_within_own_level_is_not_audited(self):
        memory = memory_with(info("admin", base="read", via="harness_admin"))
        self.check(memory, "read", "enforce")
        memory.record_program_admin_access.assert_not_called()


def oidc_config():
    return SimpleNamespace(identity=SimpleNamespace(provider="oidc"), roles=SimpleNamespace(bundles={}))


class ChannelRefusalTests(unittest.TestCase):
    def test_no_oidc_means_no_change(self):
        config = SimpleNamespace(identity=SimpleNamespace(provider="static"))
        self.assertIsNone(
            channel_program_refusal({"directory_linked": False}, tenant_id="default", config=config, program_id="p1")
        )

    def test_unlinked_chat_user_is_refused(self):
        self.assertEqual(
            channel_program_refusal({"directory_linked": False}, tenant_id="default", config=oidc_config(), program_id="p1"),
            UNLINKED_CHANNEL_MESSAGE,
        )

    def test_linked_user_without_approve_is_refused(self):
        memory = memory_with(info("read"))
        meta = {"directory_linked": True, "directory_subject": "auth0|alice", "directory_teams": [], "directory_roles": []}
        with mock.patch.dict(os.environ, {"TS_HARNESS_PROGRAM_ACCESS": "enforce"}):
            refusal = channel_program_refusal(
                meta, tenant_id="default", config=oidc_config(), memory=memory, program_id="p1"
            )
        self.assertEqual(refusal, "you need approve access to this program")
        self.assertEqual(memory.program_access_level.call_args.args[1]["subject"], "auth0|alice")

    def test_program_is_found_from_the_task(self):
        memory = memory_with(info("approve"))
        memory.task_program.return_value = {"program_id": "p9"}
        meta = {"directory_linked": True, "directory_subject": "auth0|alice"}
        with mock.patch.dict(os.environ, {"TS_HARNESS_PROGRAM_ACCESS": "enforce"}):
            self.assertIsNone(
                channel_program_refusal(meta, tenant_id="default", config=oidc_config(), memory=memory, task_id="t1")
            )
        self.assertEqual(memory.program_access_level.call_args.args[0], "p9")


class RouteTests(unittest.TestCase):
    def setUp(self):
        program_access.reset_admin_audit_cache()
        self.runtime = mock.Mock()
        self.runtime.policy.evaluate_identity.return_value = mock.Mock(decision="allow", reason="")
        self.auth = mock.Mock(
            actor_id="auth0|alice", email="alice@example.com", roles=["admin"], tenant_id="default", is_service=False
        )
        self.memory = mock.MagicMock()
        self.memory.get_program.return_value = {"program": {"id": "p1", "tenant_id": "default"}}
        self.memory.list_program_access.return_value = {"program_id": "p1", "visibility": "private", "grants": []}
        self.memory.grant_program_access.return_value = {"program_id": "p1", "visibility": "private", "grants": []}
        self.patches = [
            mock.patch.object(asgi_app, "get_runtime", return_value=self.runtime),
            mock.patch.object(asgi_app, "_auth", return_value=self.auth),
            mock.patch.object(asgi_app, "_check_origin", return_value=None),
            mock.patch.object(asgi_app, "get_memory_client", return_value=self.memory),
            mock.patch.object(program_access, "actor_context", return_value=dict(ALICE)),
            mock.patch.dict(os.environ, {"TS_HARNESS_PROGRAM_ACCESS": "enforce"}),
        ]
        for patch in self.patches:
            patch.start()
        self.client = TestClient(asgi_app.app)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()

    def test_program_without_access_is_not_found(self):
        self.memory.program_access_level.return_value = info("none")
        self.assertEqual(self.client.get("/admin/programs/p1").status_code, 404)

    def test_reader_cannot_cancel(self):
        self.memory.program_access_level.return_value = info("read")
        response = self.client.post("/admin/programs/p1/cancel", json={"reason": "x"})
        self.assertEqual(response.status_code, 403)
        self.memory.cancel_program.assert_not_called()

    def test_access_list_reports_the_viewer_level(self):
        self.memory.program_access_level.return_value = info("write")
        response = self.client.get("/admin/programs/p1/access")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["access_level"], "write")

    def test_audit_mode_offers_everything_in_the_console(self):
        self.memory.program_access_level.return_value = info("read")
        with mock.patch.dict(os.environ, {"TS_HARNESS_PROGRAM_ACCESS": "audit"}):
            response = self.client.get("/admin/programs/p1/access")
        self.assertEqual(response.json()["access_level"], "admin")

    def test_only_program_admins_can_share(self):
        self.memory.program_access_level.return_value = info("approve")
        body = {"principal_type": "user", "principal_id": "auth0|bob", "level": "read"}
        self.assertEqual(self.client.post("/admin/programs/p1/access", json=body).status_code, 403)
        self.memory.grant_program_access.assert_not_called()

        self.memory.program_access_level.return_value = info("admin", via="owner")
        response = self.client.post("/admin/programs/p1/access", json=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["access_level"], "admin")
        self.memory.grant_program_access.assert_called_once_with(
            "p1", principal_type="user", principal_id="auth0|bob", level="read", actor="auth0|alice"
        )

    def test_revoke_and_visibility_routes(self):
        self.memory.program_access_level.return_value = info("admin", via="owner")
        self.memory.revoke_program_access.return_value = {"program_id": "p1", "grants": []}
        self.memory.set_program_visibility.return_value = {"program_id": "p1", "visibility": "public", "grants": []}
        self.assertEqual(self.client.delete("/admin/programs/p1/access/team/payments").status_code, 200)
        self.memory.revoke_program_access.assert_called_once_with(
            "p1", principal_type="team", principal_id="payments", actor="auth0|alice"
        )
        response = self.client.patch("/admin/programs/p1/visibility", json={"visibility": "public"})
        self.assertEqual(response.json()["visibility"], "public")


def _insert_program(cur, *, owner=None, visibility="private", root=None, tenant="default"):
    pid = str(uuid.uuid4())
    cur.execute(
        """
        INSERT INTO delivery_programs (id, tenant_id, summary, entry_phase, current_phase, owner_id, visibility, root_program_id, parent_program_id)
        VALUES (%s, %s, 'p', 'prd', 'prd', %s, %s, %s, %s)
        """,
        (pid, tenant, owner, visibility, root, root),
    )
    return pid


@requires_pg
class ProgramAccessPostgresTests(PgTestCase):
    def test_levels_follow_grants_on_the_root(self):
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                root = _insert_program(cur, owner="auth0|alice")
                child = _insert_program(cur, owner="auth0|zed", root=root)
                directory.ensure_team(cur, "default", "payments")
            conn.commit()
            bob = team_actor("payments", "approver")
            self.assertEqual(sql_access.program_access_level(conn, child, bob)["level"], "none")
            sql_access.grant_program_access(
                conn, child, {"principal_type": "team", "principal_id": "Payments", "level": "write", "actor": "auth0|alice"}
            )
            result = sql_access.program_access_level(conn, child, bob)
            self.assertEqual((result["level"], result["access_root_id"]), ("approve", root))
            self.assertEqual(sql_access.program_access_level(conn, child, ALICE)["level"], "admin")
            listed = sql_access.list_program_access(conn, child)
            self.assertTrue(listed["inherited"])
            self.assertEqual(listed["grants"][0]["principal_id"], "payments")

            sql_access.revoke_program_access(conn, root, "team", "payments", {"actor": "auth0|alice"})
            self.assertEqual(sql_access.program_access_level(conn, child, bob)["level"], "none")
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT action_type FROM session_audit_log WHERE action_type LIKE 'program_access_%%' ORDER BY occurred_at"
                )
                self.assertEqual(
                    [r["action_type"] for r in cur.fetchall()], ["program_access_granted", "program_access_revoked"]
                )

    def test_list_filter_and_visibility(self):
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                mine = _insert_program(cur, owner="auth0|alice")
                theirs = _insert_program(cur, owner="auth0|zed")
                public = _insert_program(cur, owner="auth0|zed", visibility="public")
                child_of_public = _insert_program(cur, root=public)
            conn.commit()

            ours = {mine, theirs, public, child_of_public}

            def visible(actor):
                clause, params = sql_access.list_filter_clause(actor)
                with conn.cursor() as cur:
                    cur.execute(f"SELECT id FROM delivery_programs WHERE {clause}", params)
                    return {str(r["id"]) for r in cur.fetchall()} & ours

            self.assertEqual(visible(ALICE), {mine, public, child_of_public})
            sql_access.set_program_visibility(conn, theirs, {"visibility": "public", "actor": "auth0|zed"})
            self.assertIn(theirs, visible(ALICE))
            with self.assertRaises(ValueError):
                sql_access.set_program_visibility(conn, theirs, {"visibility": "secret"})

    def test_new_programs_default_to_private(self):
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO delivery_programs (summary, entry_phase, current_phase) VALUES ('p', 'prd', 'prd') RETURNING visibility"
                )
                self.assertEqual(cur.fetchone()["visibility"], "private")

    def test_task_program_walks_parent_workflows(self):
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                program = _insert_program(cur, owner="auth0|alice")
                cur.execute(
                    "INSERT INTO workflows (workflow_type, program_id) VALUES ('implement_delivery', %s) RETURNING id",
                    (program,),
                )
                parent = cur.fetchone()["id"]
                cur.execute(
                    "INSERT INTO workflows (workflow_type, parent_workflow_id) VALUES ('deliver_work_order', %s) RETURNING id",
                    (parent,),
                )
                child = cur.fetchone()["id"]
                cur.execute(
                    "INSERT INTO workflow_tasks (workflow_id, step_name, agent_id) VALUES (%s, 'review', 'review-agent') RETURNING id",
                    (child,),
                )
                task = str(cur.fetchone()["id"])
            conn.commit()
            self.assertEqual(sql_access.task_program(conn, task)["program_id"], program)

    def test_admin_access_is_recorded(self):
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                program = _insert_program(cur, owner="auth0|zed")
            conn.commit()
            sql_access.record_program_admin_access(conn, program, {"actor": "auth0|ops", "route": "get", "need": "read"})
            with conn.cursor() as cur:
                cur.execute("SELECT agent_id, details FROM session_audit_log WHERE action_type = 'program_admin_access'")
                row = cur.fetchone()
        self.assertEqual(row["agent_id"], "auth0|ops")
        self.assertEqual(row["details"]["route"], "get")


if __name__ == "__main__":
    unittest.main()
