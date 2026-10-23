"""Shared runtime state in Postgres: file trees, locks, skill index, key-value state, audit and dedup."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from tests.memory_api_support import LiveMemoryApi
from tests.pg_support import PgTestCase, requires_pg

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from memory_routes import shared_state, skill_index  # noqa: E402

from ts_harness.memory.client import MemoryApiConflict, MemoryClient  # noqa: E402
from ts_harness.storage.backend import StorageConfigError, validate_storage_config  # noqa: E402

TABLES = (
    "shared_files", "shared_file_namespaces", "shared_file_locks", "skill_versions",
    "runtime_kv", "runtime_audit_events", "channel_event_dedup",
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _files(**contents: str) -> list[dict]:
    return [{"path": p.replace("__", "/"), "sha256": _sha(c), "size": len(c)} for p, c in contents.items()]


class _Base(PgTestCase):
    def setUp(self):
        with self.db.connect() as conn:
            for table in TABLES:
                conn.execute(f"DELETE FROM {table}")
            conn.commit()

    def call(self, fn, *args):
        with self.db.connect() as conn:
            return fn(conn, *args)


@requires_pg
class SharedFilesTests(_Base):
    def test_commit_bumps_generation_once_and_pull_returns_files(self):
        out = self.call(shared_state.commit_files, "definitions", {
            "base_generation": 0,
            "puts": [{"path": "workflows/active/a.json", "content": "{}"}, {"path": "manifest.json", "content": "{\"g\":1}"}],
            "actor": "admin@x",
        })
        self.assertEqual((out["generation"], out["changed"]), (1, 2))
        pulled = self.call(shared_state.pull_files, "definitions", {"since": 0})
        self.assertEqual(pulled["generation"], 1)
        self.assertEqual([f["path"] for f in pulled["files"]], ["manifest.json", "workflows/active/a.json"])
        self.assertEqual(pulled["files"][1]["sha256"], _sha("{}"))

    def test_stale_base_generation_is_refused_and_nothing_is_written(self):
        self.call(shared_state.commit_files, "definitions", {"base_generation": 0, "puts": [{"path": "a", "content": "1"}]})
        with self.assertRaises(shared_state.StaleGeneration) as ctx:
            self.call(shared_state.commit_files, "definitions", {"base_generation": 0, "puts": [{"path": "b", "content": "2"}]})
        self.assertEqual(ctx.exception.current, 1)
        paths = [f["path"] for f in self.call(shared_state.pull_files, "definitions", {})["files"]]
        self.assertEqual(paths, ["a"])

    def test_deletes_leave_tombstones_for_incremental_pulls_only(self):
        self.call(shared_state.commit_files, "cron", {"puts": [{"path": "a", "content": "1"}, {"path": "b", "content": "2"}]})
        self.call(shared_state.commit_files, "cron", {"deletes": ["a"], "puts": [{"path": "c", "content": "3"}]})
        since_one = self.call(shared_state.pull_files, "cron", {"since": 1})
        self.assertEqual(
            [(f["path"], f["deleted"]) for f in since_one["files"]],
            [("a", True), ("c", False)],
        )
        full = self.call(shared_state.pull_files, "cron", {"since": 0})
        self.assertEqual([f["path"] for f in full["files"]], ["b", "c"])
        self.assertEqual(self.call(shared_state.pull_files, "cron", {"since": 2})["files"], [])

    def test_namespaces_are_independent(self):
        self.call(shared_state.commit_files, "cron", {"puts": [{"path": "a", "content": "1"}]})
        self.assertEqual(self.call(shared_state.namespace_generation, "definitions")["generation"], 0)

    def test_limits_and_path_validation(self):
        with self.assertRaisesRegex(ValueError, "larger than"):
            self.call(shared_state.commit_files, "cron", {"puts": [{"path": "big", "content": "x" * (512 * 1024 + 1)}]})
        for bad in ("../etc", "/abs", "a//b", "a/./b"):
            with self.assertRaisesRegex(ValueError, "invalid path"):
                self.call(shared_state.commit_files, "cron", {"puts": [{"path": bad, "content": "1"}]})
        with self.assertRaisesRegex(ValueError, "namespace"):
            self.call(shared_state.pull_files, "Bad NS", {})
        with mock.patch.object(shared_state, "MAX_NAMESPACE_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "exceed"):
                self.call(shared_state.commit_files, "cron", {"puts": [{"path": "a", "content": "x" * 11}]})

    def test_lock_is_exclusive_until_released_or_expired(self):
        first = self.call(shared_state.acquire_lock, "definitions", "workflows", {"holder": "a", "ttl_seconds": 30})
        second = self.call(shared_state.acquire_lock, "definitions", "workflows", {"holder": "b", "ttl_seconds": 30})
        self.assertEqual((first["locked"], second["locked"], second["holder"]), (True, False, "a"))
        self.assertTrue(self.call(shared_state.acquire_lock, "definitions", "workflows", {"holder": "a"})["locked"])
        self.assertFalse(self.call(shared_state.release_lock, "definitions", "workflows", {"holder": "b"})["released"])
        self.assertTrue(self.call(shared_state.release_lock, "definitions", "workflows", {"holder": "a"})["released"])
        self.assertTrue(self.call(shared_state.acquire_lock, "definitions", "workflows", {"holder": "b", "ttl_seconds": 1})["locked"])
        with self.db.connect() as conn:
            conn.execute("UPDATE shared_file_locks SET expires_at = now() - interval '1 second'")
            conn.commit()
        self.assertTrue(self.call(shared_state.acquire_lock, "definitions", "workflows", {"holder": "c"})["locked"])


@requires_pg
class SkillIndexTests(_Base):
    def draft(self, skill_id="ops/triage", version="1.0.0", *, revision_of=None, tenant="default", category="ops", body="v1"):
        return self.call(skill_index.upsert_draft, {
            "skill_id": skill_id, "version": version, "category": category, "layout": "folder",
            "tenant_id": tenant, "revision_of": revision_of, "source": "learn", "created_by": "lead@x",
            "object_prefix": f"skills/drafts/{uuid.uuid4().hex}/",
            "files": _files(**{"SKILL.md": body}), "content_sha": _sha(body),
        })

    def approve(self, draft_id, version="1.0.0", body="v1"):
        return self.call(skill_index.approve_draft, draft_id, {
            "reviewer": "admin@x", "object_prefix": f"skills/ops/triage/{version}-{_sha(body)[:12]}/",
            "files": _files(**{"SKILL.md": body}), "content_sha": _sha(body),
        })

    def live(self):
        return self.call(skill_index.list_skill_index, {"status": "active"})["skills"]

    def test_approval_flips_pointer_and_supersedes_previous(self):
        first = self.draft()
        gen0 = first["generation"]
        out = self.approve(first["skill"]["id"])
        self.assertGreater(out["generation"], gen0)
        self.assertTrue(out["draft_prefix"].startswith("skills/drafts/"))
        self.assertEqual([r["version"] for r in self.live()], ["1.0.0"])

        second = self.draft(version="1.1.0", revision_of="ops/triage", body="v2")
        out2 = self.approve(second["skill"]["id"], version="1.1.0", body="v2")
        self.assertEqual(out2["superseded_id"], out["skill"]["id"])
        live = self.live()
        self.assertEqual([(r["version"], r["draft_id"]) for r in live], [("1.1.0", second["skill"]["id"])])
        statuses = sorted(r["status"] for r in self.call(skill_index.list_skill_index, {"status": "active,superseded,approved"})["skills"])
        self.assertEqual(statuses, ["active", "approved", "approved", "superseded"])

    def test_version_rules(self):
        self.approve(self.draft()["skill"]["id"])
        with self.assertRaises(skill_index.SkillIndexConflict) as ctx:
            self.draft()
        self.assertEqual(ctx.exception.code, "skill_learn_use_revision")
        with self.assertRaises(skill_index.SkillIndexConflict) as ctx:
            self.draft(version="1.0.0", revision_of="ops/triage")
        self.assertEqual(ctx.exception.code, "skill_version_not_bumped")
        with self.assertRaises(skill_index.SkillIndexConflict) as ctx:
            self.draft(version="2.0.0", revision_of="ops/triage", category="dev")
        self.assertEqual(ctx.exception.code, "skill_category_changed")
        with self.assertRaises(skill_index.SkillIndexConflict) as ctx:
            self.draft(skill_id="ops/other", revision_of="ops/other")
        self.assertEqual(ctx.exception.code, "skill_revision_not_live")

    def test_approve_rechecks_rules_and_refuses_draft_prefix(self):
        stale = self.draft(version="1.1.0", skill_id="ops/triage")
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.call(skill_index.approve_draft, stale["skill"]["id"], {
                "reviewer": "admin@x", "object_prefix": "skills/drafts/x/",
                "files": _files(**{"SKILL.md": "v1"}), "content_sha": _sha("v1"),
            })
        self.approve(stale["skill"]["id"])
        with self.assertRaises(skill_index.SkillIndexConflict) as ctx:
            self.approve(stale["skill"]["id"])
        self.assertEqual(ctx.exception.code, "skill_draft_decided")
        with self.assertRaises(LookupError):
            self.approve(str(uuid.uuid4()))

    def test_draft_replace_returns_previous_prefix_and_other_tenant_is_refused(self):
        first = self.draft()
        second = self.draft(body="v1b")
        self.assertEqual(first["skill"]["id"], second["skill"]["id"])
        self.assertEqual(second["previous_prefix"], first["skill"]["object_prefix"])
        with self.assertRaises(skill_index.SkillIndexConflict):
            self.draft(tenant="other")

    def test_reject_marks_row_and_allows_a_new_draft(self):
        first = self.draft()
        out = self.call(skill_index.reject_draft, first["skill"]["id"], {"reviewer": "admin@x", "reason": "no"})
        self.assertEqual((out["skill"]["status"], out["skill"]["reason"]), ("rejected", "no"))
        self.assertEqual(self.call(skill_index.list_skill_index, {"status": "draft"})["skills"], [])
        self.assertNotEqual(self.draft()["skill"]["id"], first["skill"]["id"])

    def test_one_active_row_per_skill_is_enforced_by_the_database(self):
        self.approve(self.draft()["skill"]["id"])
        import psycopg

        with self.assertRaises(psycopg.errors.UniqueViolation):
            with self.db.connect() as conn:
                conn.execute(
                    "INSERT INTO skill_versions (skill_id, version, category, leaf, status, object_prefix, content_sha) "
                    "VALUES ('ops/triage', '9.0.0', 'ops', 'triage', 'active', 'skills/x/', %s)",
                    (_sha("x"),),
                )


@requires_pg
class KvAuditDedupTests(_Base):
    def test_kv_compare_and_set(self):
        self.assertEqual(self.call(shared_state.kv_get, "scheduler", "state")["version"], 0)
        v1 = self.call(shared_state.kv_put, "scheduler", "state", {"value": {"a": 1}, "expected_version": 0})["version"]
        with self.assertRaises(shared_state.VersionConflict):
            self.call(shared_state.kv_put, "scheduler", "state", {"value": {"a": 2}, "expected_version": 0})
        v2 = self.call(shared_state.kv_put, "scheduler", "state", {"value": {"a": 2}, "expected_version": v1})["version"]
        with self.assertRaises(shared_state.VersionConflict) as ctx:
            self.call(shared_state.kv_put, "scheduler", "state", {"value": {"a": 3}, "expected_version": v1})
        self.assertEqual(ctx.exception.current, v2)
        self.call(shared_state.kv_put, "scheduler", "state", {"value": {"a": 4}})
        got = self.call(shared_state.kv_get, "scheduler", "state")
        self.assertEqual((got["value"], got["version"]), ({"a": 4}, v2 + 1))

    def test_audit_append_is_idempotent_and_filters(self):
        event = {"event_id": str(uuid.uuid4()), "tenant_id": "t1", "action_type": "tool", "actor_id": "a",
                 "payload": {"x": 1}, "timestamp": "2026-01-02T03:04:05Z"}
        other = {**event, "event_id": str(uuid.uuid4()), "tenant_id": "t2"}
        self.assertEqual(self.call(shared_state.append_audit_events, {"events": [event, other]})["inserted"], 2)
        self.assertEqual(self.call(shared_state.append_audit_events, {"events": [event]})["inserted"], 0)
        rows = self.call(shared_state.list_audit_events, {"tenant_id": "t1"})["events"]
        self.assertEqual([(r["event_id"], r["timestamp"], r["payload"]) for r in rows],
                         [(event["event_id"], "2026-01-02T03:04:05Z", {"x": 1})])
        self.assertEqual(self.call(shared_state.list_audit_events, {"tenant_id": "t1", "since": "2026-02-01T00:00:00Z"})["events"], [])
        with self.assertRaisesRegex(ValueError, "UUID"):
            self.call(shared_state.append_audit_events, {"events": [{**event, "event_id": "nope"}]})

    def test_dedup_marks_once_and_sweep_removes_old_keys(self):
        self.assertTrue(self.call(shared_state.mark_channel_event, {"key": "slack:T1:Ev1"})["new"])
        self.assertFalse(self.call(shared_state.mark_channel_event, {"key": "slack:T1:Ev1"})["new"])
        with self.db.connect() as conn:
            conn.execute("UPDATE channel_event_dedup SET seen_at = now() - interval '8 days'")
            conn.commit()
        self.assertEqual(self.call(shared_state.sweep_channel_dedup, {})["deleted"], 1)
        self.assertTrue(self.call(shared_state.mark_channel_event, {"key": "slack:T1:Ev1"})["new"])


@requires_pg
class ThroughHttpTests(_Base):
    """The same routes reached the way the runtime reaches them: MemoryClient over HTTP."""

    def test_client_round_trip_and_conflicts(self):
        with LiveMemoryApi(self.db) as api:
            client = MemoryClient(api.url)
            out = client.commit_shared_files("definitions", base_generation=0, puts=[{"path": "a", "content": "1"}], deletes=[])
            self.assertEqual(out["generation"], 1)
            self.assertEqual(client.shared_files_generation("definitions"), 1)
            with self.assertRaises(MemoryApiConflict) as ctx:
                client.commit_shared_files("definitions", base_generation=0, puts=[], deletes=["a"])
            self.assertEqual((ctx.exception.code, ctx.exception.detail["generation"]), ("stale_generation", 1))
            self.assertEqual(client.pull_shared_files("definitions", since=0)["files"][0]["content"], "1")

            self.assertTrue(client.acquire_shared_lock("definitions", "workflows", holder="h1"))
            self.assertFalse(client.acquire_shared_lock("definitions", "workflows", holder="h2"))
            self.assertTrue(client.release_shared_lock("definitions", "workflows", holder="h1"))

            self.assertEqual(client.kv_put("delegation", "state", {"n": 1}, expected_version=0), 1)
            with self.assertRaises(MemoryApiConflict) as ctx:
                client.kv_put("delegation", "state", {"n": 2}, expected_version=0)
            self.assertEqual(ctx.exception.code, "version_conflict")
            self.assertEqual(client.kv_get("delegation", "state")["value"], {"n": 1})

            draft = client.upsert_skill_draft(
                skill_id="ops/triage", version="1.0.0", category="ops", layout="file",
                object_prefix="skills/drafts/d1/", files=_files(**{"SKILL.md": "v1"}), content_sha=_sha("v1"),
            )
            client.approve_skill_draft(
                draft["skill"]["id"], reviewer="admin@x", object_prefix="skills/ops/triage/1.0.0-abc/",
                files=_files(**{"SKILL.md": "v1"}), content_sha=_sha("v1"),
            )
            with self.assertRaises(MemoryApiConflict) as ctx:
                client.upsert_skill_draft(
                    skill_id="ops/triage", version="1.0.0", category="ops", object_prefix="skills/drafts/d2/",
                    files=_files(**{"SKILL.md": "v1"}), content_sha=_sha("v1"),
                )
            self.assertEqual(ctx.exception.code, "skill_learn_use_revision")
            index = client.list_skill_index()
            self.assertEqual([(s["skill_id"], s["status"]) for s in index["skills"]], [("ops/triage", "active")])
            self.assertGreater(index["generation"], 0)

            event = {"event_id": str(uuid.uuid4()), "tenant_id": "t", "action_type": "x", "actor_id": "a"}
            client.append_audit_events([event])
            self.assertEqual(len(client.list_audit_events(tenant_id="t")), 1)
            self.assertTrue(client.mark_channel_event("k"))
            self.assertFalse(client.mark_channel_event("k"))

    def test_routes_require_the_api_token(self):
        import urllib.error
        import urllib.request

        with LiveMemoryApi(self.db) as api:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"{api.url}/shared-files/definitions")
            self.assertEqual(ctx.exception.code, 401)


class ContractTests(unittest.TestCase):
    def test_memory_routes_never_import_ts_harness(self):
        for path in sorted((REPO_ROOT / "scripts" / "memory_routes").glob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    self.assertFalse(name.startswith("ts_harness"), f"{path.name} imports {name}")

    def test_migration_is_registered(self):
        order = (REPO_ROOT / "scripts" / "ops" / "migrate-db.sh").read_text()
        self.assertIn("043_shared_runtime_state.sql", order)
        self.assertIn("| 44 | `043_shared_runtime_state.sql`", (REPO_ROOT / "database" / "MIGRATION_ORDER.md").read_text())

    def test_dedup_sweep_is_a_daily_system_job(self):
        from ts_harness.gateway.system_jobs import SYSTEM_HANDLERS, sweep_channel_dedup

        self.assertIs(SYSTEM_HANDLERS["sweep_channel_dedup"], sweep_channel_dedup)
        cron = json.loads((REPO_ROOT / "config" / "runtime-cron-jobs.json").read_text())
        job = next(j for j in cron["jobs"] if j.get("handler") == "sweep_channel_dedup")
        self.assertEqual(job["interval_seconds"], 86400)
        self.assertIn('"handler": "sweep_channel_dedup"', (REPO_ROOT / "scripts" / "phases" / "15-automation-cron.sh").read_text())
        with mock.patch.dict(os.environ, {"TS_HARNESS_STATE_BACKEND": "local"}):
            self.assertEqual(sweep_channel_dedup()["status"], "skipped")


class StorageConfigTests(unittest.TestCase):
    def check(self, **env):
        base = {k: "" for k in ("TS_HARNESS_STATE_BACKEND", "TS_HARNESS_OBJECT_BACKEND", "TS_HARNESS_SKILLS_BUCKET",
                                "TS_HARNESS_WORKSPACE_BUCKET", "TS_HARNESS_SESSION_DB_URL", "CODING_JOBS_ENABLED")}
        with mock.patch.dict(os.environ, {**base, **env}):
            return validate_storage_config()

    def test_defaults_are_local_and_valid(self):
        self.assertEqual(self.check(), [])

    def test_object_storage_needs_the_postgres_index(self):
        with self.assertRaisesRegex(StorageConfigError, "Postgres index"):
            self.check(TS_HARNESS_OBJECT_BACKEND="s3", TS_HARNESS_SKILLS_BUCKET="s", TS_HARNESS_WORKSPACE_BUCKET="w")

    def test_postgres_state_needs_the_session_database(self):
        with self.assertRaisesRegex(StorageConfigError, "TS_HARNESS_SESSION_DB_URL"):
            self.check(TS_HARNESS_STATE_BACKEND="postgres")

    def test_s3_needs_buckets_and_warns_about_in_process_checkouts(self):
        with self.assertRaisesRegex(StorageConfigError, "TS_HARNESS_WORKSPACE_BUCKET"):
            self.check(TS_HARNESS_STATE_BACKEND="postgres", TS_HARNESS_SESSION_DB_URL="postgresql://x",
                       TS_HARNESS_OBJECT_BACKEND="s3", TS_HARNESS_SKILLS_BUCKET="s")
        warnings = self.check(TS_HARNESS_STATE_BACKEND="postgres", TS_HARNESS_SESSION_DB_URL="postgresql://x",
                              TS_HARNESS_OBJECT_BACKEND="s3", TS_HARNESS_SKILLS_BUCKET="s", TS_HARNESS_WORKSPACE_BUCKET="w")
        self.assertEqual(len(warnings), 1)
        self.assertEqual(self.check(TS_HARNESS_STATE_BACKEND="postgres", TS_HARNESS_SESSION_DB_URL="postgresql://x",
                                    TS_HARNESS_OBJECT_BACKEND="s3", TS_HARNESS_SKILLS_BUCKET="s",
                                    TS_HARNESS_WORKSPACE_BUCKET="w", CODING_JOBS_ENABLED="true"), [])

    def test_unknown_values_are_refused(self):
        with self.assertRaises(StorageConfigError):
            self.check(TS_HARNESS_STATE_BACKEND="redis")


if __name__ == "__main__":
    unittest.main()
