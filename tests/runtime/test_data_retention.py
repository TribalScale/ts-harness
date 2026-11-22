"""Data retention: windows from the environment, the daily system job, the SQL per category and the purge script."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

from tests.memory_api_support import LiveMemoryApi
from tests.pg_support import PgTestCase, requires_pg

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from memory_routes import data_retention as routes  # noqa: E402

from ts_harness import data_retention  # noqa: E402
from ts_harness.memory.client import MemoryClient  # noqa: E402

RETENTION_ENV = [s.env for s in data_retention.SETTINGS] + [data_retention.BATCH_ENV]


def _load_purge_script():
    spec = importlib.util.spec_from_file_location("purge_program_data", REPO_ROOT / "scripts" / "ops" / "purge-program-data.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeStore:
    def __init__(self):
        self.deleted: list[str] = []

    def delete(self, key):
        self.deleted.append(key)


class FakeClient:
    def __init__(self, *, uploads=None, purge=None, fail=False):
        self.calls: list[tuple[str, dict]] = []
        self.uploads = uploads or {"programs": [], "object_keys": []}
        self.purge = purge or {}
        self.fail = fail

    def apply_data_retention(self, **settings):
        self.calls.append(("apply", settings))
        if self.fail:
            raise RuntimeError("memory api down")
        return {"batch": settings["batch"], "transcripts": {"deleted": 3}}

    def expired_uploads(self, **kw):
        self.calls.append(("expired", kw))
        return self.uploads

    def mark_uploads_purged(self, programs):
        self.calls.append(("purged", {"programs": programs}))
        return {"marked": len(programs)}

    def purge_program_data(self, program_id, **kw):
        self.calls.append(("purge", {"program_id": program_id, **kw}))
        return dict(self.purge)


class PolicyTests(unittest.TestCase):
    def policy(self, **env):
        with mock.patch.dict(os.environ, {name: "" for name in RETENTION_ENV}):
            with mock.patch.dict(os.environ, env):
                return data_retention.retention_policy()

    def test_defaults_are_conservative(self):
        policy = self.policy()
        self.assertEqual(
            (policy.transcript_days, policy.coding_log_days, policy.workflow_task_days, policy.audit_days,
             policy.audit_decision_days, policy.upload_days, policy.batch),
            (180, 30, 365, 730, 0, 180, 500),
        )

    def test_env_overrides_zero_keeps_forever_and_bad_values_fall_back(self):
        policy = self.policy(
            TS_HARNESS_RETENTION_TRANSCRIPT_DAYS="0",
            TS_HARNESS_RETENTION_CODING_LOG_DAYS="7",
            TS_HARNESS_RETENTION_AUDIT_DAYS="soon",
            TS_HARNESS_RETENTION_UPLOAD_DAYS="-5",
            TS_HARNESS_RETENTION_BATCH="50",
        )
        self.assertEqual((policy.transcript_days, policy.coding_log_days, policy.audit_days, policy.upload_days, policy.batch),
                         (0, 7, 730, 0, 50))


class TrimTests(unittest.TestCase):
    def test_trim_logs_drops_log_text_at_any_depth_and_keeps_the_summary(self):
        result = {
            "ok": True, "pr_url": "https://github.com/o/r/pull/1", "log_tail": "x" * 9000,
            "usage": [{"model": "m", "input_tokens": 10}], "timings": {"coding": 12.5},
            "verification": {"result": "passed", "checks": [{"check": "unit", "exit_code": 0, "output_tail": "ok"}]},
            "pre_push_review": {"status": "pass", "summary": "fine", "open_blocking": []},
            "security": {"secret_scan": {"tool": "gitleaks", "findings": []}},
        }
        trimmed = routes.trim_logs(result)
        self.assertNotIn("log_tail", trimmed)
        self.assertEqual(trimmed["verification"]["checks"], [{"check": "unit", "exit_code": 0}])
        for key in ("ok", "pr_url", "usage", "timings", "pre_push_review", "security"):
            self.assertEqual(trimmed[key], result[key])

    def test_small_fields_keeps_short_scalars_only(self):
        out = routes.small_fields({"status": "done", "pr_url": "u", "count": 3, "ok": False,
                                   "work_order": "w" * 500, "findings": [{"a": 1}], "nested": {"b": 2}})
        self.assertEqual(out, {"status": "done", "pr_url": "u", "count": 3, "ok": False, "retention_trimmed": True})
        self.assertEqual(routes.small_fields(None), {})

    def test_days_and_batch_validation(self):
        self.assertEqual(routes._days({}, "audit_days"), 0)
        with self.assertRaises(ValueError):
            routes._days({"audit_days": -1}, "audit_days")
        self.assertEqual(routes._batch({"batch": 10**9}), routes.MAX_BATCH)
        self.assertEqual(routes._batch({}), routes.DEFAULT_BATCH)


class JobTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {name: "" for name in RETENTION_ENV})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_job_sends_the_policy_and_deletes_only_design_keys_of_expired_programs(self):
        client = FakeClient(uploads={"programs": ["p1"], "object_keys": ["design/t/a.png", "skills/x"], "kept_in_use": 1})
        store = FakeStore()
        with mock.patch("ts_harness.intake.blobs.intake_store", return_value=store):
            out = data_retention.apply_data_retention(client=client)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(client.calls[0], ("apply", {"transcript_days": 180, "coding_log_days": 30, "workflow_task_days": 365,
                                                    "audit_days": 730, "audit_decision_days": 0, "batch": 500}))
        self.assertEqual(client.calls[1], ("expired", {"days": 180, "batch": 50}))
        self.assertEqual(store.deleted, ["design/t/a.png"])
        self.assertEqual(client.calls[2], ("purged", {"programs": ["p1"]}))
        self.assertEqual(out["uploads"], {"programs": 1, "deleted": 1, "kept_in_use": 1})
        self.assertEqual(out["transcripts"], {"deleted": 3})

    def test_upload_window_zero_keeps_uploads(self):
        client = FakeClient()
        with mock.patch.dict(os.environ, {"TS_HARNESS_RETENTION_UPLOAD_DAYS": "0"}):
            out = data_retention.apply_data_retention(client=client)
        self.assertEqual(out["uploads"], {"kept": "forever"})
        self.assertEqual([c[0] for c in client.calls], ["apply"])

    def test_nothing_expired_marks_nothing(self):
        client = FakeClient()
        out = data_retention.apply_data_retention(client=client)
        self.assertEqual(out["uploads"], {"programs": 0, "deleted": 0})
        self.assertEqual([c[0] for c in client.calls], ["apply", "expired"])

    def test_memory_api_failure_is_reported_not_raised(self):
        out = data_retention.apply_data_retention(client=FakeClient(fail=True))
        self.assertEqual(out["status"], "error")
        self.assertIn("memory api down", out["error"])

    def test_registered_as_a_daily_system_job_and_dispatched(self):
        from ts_harness.gateway import system_jobs

        self.assertIs(system_jobs.SYSTEM_HANDLERS["apply_data_retention"], system_jobs.apply_data_retention)
        cron = json.loads((REPO_ROOT / "config" / "runtime-cron-jobs.json").read_text())
        job = next(j for j in cron["jobs"] if j.get("handler") == "apply_data_retention")
        self.assertEqual((job["name"], job["kind"], job["interval_seconds"]), ("apply-data-retention", "system", 86400))
        self.assertIn('"handler": "apply_data_retention"', (REPO_ROOT / "scripts" / "phases" / "15-automation-cron.sh").read_text())
        with mock.patch("ts_harness.data_retention.apply_data_retention", return_value={"status": "ok"}) as run:
            self.assertEqual(system_jobs.dispatch_system_job({"handler": "apply_data_retention"}), {"status": "ok"})
        run.assert_called_once_with()

    def test_migration_is_registered(self):
        order = (REPO_ROOT / "scripts" / "ops" / "migrate-db.sh").read_text()
        self.assertIn("051_data_retention.sql", order)
        self.assertTrue((REPO_ROOT / "database" / "migrations" / "051_data_retention.sql").is_file())


class PurgeScriptTests(unittest.TestCase):
    def setUp(self):
        self.script = _load_purge_script()

    def test_dry_run_counts_and_deletes_nothing(self):
        client = FakeClient(purge={"dry_run": True, "counts": {"workflows": 2}, "object_keys": ["design/t/a.png"]})
        store = FakeStore()
        args = self.script.parse_args(["11111111-1111-1111-1111-111111111111", "--dry-run", "--session-id", "s1"])
        out = self.script.purge(args, client=client, store=store)
        self.assertEqual(client.calls, [("purge", {"program_id": "11111111-1111-1111-1111-111111111111", "dry_run": True,
                                                   "include_decisions": False, "session_ids": ["s1"], "actor": "ops"})])
        self.assertEqual(store.deleted, [])
        self.assertEqual(out["objects_to_delete"], 1)

    def test_real_run_deletes_design_objects_after_the_database(self):
        client = FakeClient(purge={"dry_run": False, "object_keys": ["design/t/a.png", "../etc"]})
        store = FakeStore()
        args = self.script.parse_args(["p", "--yes", "--include-decisions"])
        out = self.script.purge(args, client=client, store=store)
        self.assertTrue(client.calls[0][1]["include_decisions"])
        self.assertFalse(client.calls[0][1]["dry_run"])
        self.assertEqual(store.deleted, ["design/t/a.png"])
        self.assertEqual(out["objects_deleted"], 1)

    def test_refuses_to_delete_without_yes(self):
        with mock.patch.object(self.script, "purge") as purge:
            self.assertEqual(self.script.main(["p"]), 2)
        purge.assert_not_called()


# --- Postgres ---------------------------------------------------------------------------------

TABLES = (
    "coding_jobs", "workflow_tasks", "workflows", "program_upload_purges", "delivery_programs",
    "runtime_transcript_messages", "runtime_sessions", "runtime_audit_events", "session_audit_log",
    "memories", "llm_usage", "workflow_decisions",
)


class _PgBase(PgTestCase):
    def setUp(self):
        with self.db.connect() as conn:
            for table in TABLES:
                conn.execute(f"DELETE FROM {table}")
            conn.commit()

    def call(self, fn, *args):
        with self.db.connect() as conn:
            return fn(conn, *args)

    def sql(self, query, params=()):
        with self.db.connect() as conn:
            cur = conn.execute(query, params)
            rows = cur.fetchall() if cur.description else []
            conn.commit()
            return rows

    def program(self, status="succeeded", age_days=0, design_keys=(), root=None):
        pid = uuid.uuid4()
        refs = [{"id": f"D{i + 1}", "object_key": key} for i, key in enumerate(design_keys)]
        self.sql(
            """
            INSERT INTO delivery_programs (id, summary, entry_phase, current_phase, status, metadata, root_program_id)
            VALUES (%s, 'p', 'intake', 'done', %s, %s::jsonb, %s)
            """,
            (pid, status, json.dumps({"artifacts": {"design_refs": refs}}), root),
        )
        self.age("delivery_programs", "updated_at", pid, age_days)
        return pid

    def age(self, table, column, row_id, days, key="id"):
        if days:
            self.sql(f"ALTER TABLE {table} DISABLE TRIGGER USER")
            self.sql(f"UPDATE {table} SET {column} = now() - make_interval(days => %s) WHERE {key} = %s", (days, row_id))
            self.sql(f"ALTER TABLE {table} ENABLE TRIGGER USER")

    def workflow(self, status="succeeded", age_days=0, program=None):
        wid = uuid.uuid4()
        completed = "now() - make_interval(days => %s)" if status not in {"running", "blocked"} else "NULL"
        params = (wid, status, program) + ((age_days,) if completed != "NULL" else ())
        self.sql(
            f"INSERT INTO workflows (id, workflow_type, status, program_id, completed_at) VALUES (%s, 'delivery', %s, %s, {completed})",
            params,
        )
        return wid

    def task(self, workflow, status="succeeded", output=None):
        tid = uuid.uuid4()
        self.sql(
            """
            INSERT INTO workflow_tasks (id, workflow_id, step_name, agent_id, status, input, output, error)
            VALUES (%s, %s, 'deliver', 'coder', %s, %s::jsonb, %s::jsonb, %s)
            """,
            (tid, workflow, status, json.dumps({"work_order": "w" * 400, "work_order_id": "WO-1"}),
             json.dumps(output or {"pr_url": "https://x/pull/1", "review": "r" * 400}), "e" * 900),
        )
        return tid

    def job(self, workflow, task, status="succeeded", age_days=40):
        jid = uuid.uuid4()
        result = {"ok": True, "pr_url": "https://x/pull/1", "log_tail": "secret-free log " * 50,
                  "verification": {"checks": [{"check": "unit", "output_tail": "out"}]}, "usage": [{"model": "m"}]}
        finished = "now() - make_interval(days => %s)" if status not in {"running", "queued", "starting"} else "NULL"
        params = (jid, workflow, task, status, json.dumps({"work_order": "w" * 400, "mode": "implement"}),
                  json.dumps(result), json.dumps({"phase": "coding"})) + ((age_days,) if finished != "NULL" else ())
        self.sql(
            f"""
            INSERT INTO coding_jobs (id, workflow_id, task_id, repo_url, status, spec, result, progress, last_output, finished_at)
            VALUES (%s, %s, %s, 'https://github.com/o/r', %s, %s::jsonb, %s::jsonb, %s::jsonb, 'tail', {finished})
            """,
            params,
        )
        return jid


@requires_pg
class RetentionSqlTests(_PgBase):
    def test_transcripts_older_than_the_window_go_recent_stay(self):
        self.sql("INSERT INTO runtime_sessions (session_id, routing_key, tenant_id, agent_id, source) VALUES ('s1', 'r1', 't', 'a', 'slack')")
        self.sql("""INSERT INTO runtime_transcript_messages (session_id, seq, role, content, created_at) VALUES
                    ('s1', 1, 'user', 'old', now() - interval '181 days'),
                    ('s1', 2, 'user', 'edge', now() - interval '179 days'),
                    ('s1', 3, 'user', 'new', now())""")
        out = self.call(routes.apply_retention, {"transcript_days": 180})
        self.assertEqual(out["transcripts"], {"deleted": 1})
        self.assertEqual([r["content"] for r in self.sql("SELECT content FROM runtime_transcript_messages ORDER BY seq")], ["edge", "new"])
        self.assertEqual(self.call(routes.apply_retention, {"transcript_days": 0})["transcripts"], {"kept": "forever"})

    def test_batch_limits_each_run(self):
        self.sql("INSERT INTO runtime_sessions (session_id, routing_key, tenant_id, agent_id, source) VALUES ('s1', 'r1', 't', 'a', 'slack')")
        self.sql("""INSERT INTO runtime_transcript_messages (session_id, seq, role, content, created_at)
                    SELECT 's1', g, 'user', 'x', now() - interval '400 days' FROM generate_series(1, 5) g""")
        self.assertEqual(self.call(routes.apply_retention, {"transcript_days": 180, "batch": 2})["transcripts"], {"deleted": 2})

    def test_coding_logs_trimmed_on_old_finished_jobs_only(self):
        done = self.workflow("succeeded", age_days=40)
        old = self.job(done, self.task(done), "succeeded", age_days=40)
        recent = self.job(done, self.task(done), "failed", age_days=5)
        live_wf = self.workflow("running")
        in_running_workflow = self.job(live_wf, self.task(live_wf, "running"), "lost", age_days=40)
        running = self.job(live_wf, self.task(live_wf, "running"), "running")
        out = self.call(routes.apply_retention, {"coding_log_days": 30})
        self.assertEqual(out["coding_job_logs"], {"trimmed": 1})
        rows = {r["id"]: r for r in self.sql("SELECT id, result, progress, last_output, spec, retention_trimmed_at FROM coding_jobs")}
        self.assertNotIn("log_tail", rows[old]["result"])
        self.assertEqual(rows[old]["result"]["verification"]["checks"], [{"check": "unit"}])
        self.assertEqual((rows[old]["result"]["pr_url"], rows[old]["result"]["usage"]), ("https://x/pull/1", [{"model": "m"}]))
        self.assertIsNone(rows[old]["progress"])
        self.assertIsNone(rows[old]["last_output"])
        self.assertIn("work_order", rows[old]["spec"])
        for untouched in (recent, in_running_workflow, running):
            self.assertIn("log_tail", rows[untouched]["result"])
            self.assertIsNone(rows[untouched]["retention_trimmed_at"])
        self.assertEqual(self.call(routes.apply_retention, {"coding_log_days": 30})["coding_job_logs"], {"trimmed": 0})

    def test_task_fields_trimmed_only_for_long_finished_workflows_outside_open_programs(self):
        old_wf = self.workflow("succeeded", age_days=400)
        old_task = self.task(old_wf)
        job = self.job(old_wf, old_task, "succeeded", age_days=400)
        recent_wf = self.workflow("succeeded", age_days=100)
        recent_task = self.task(recent_wf)
        running_wf = self.workflow("running")
        running_task = self.task(running_wf, "running")
        open_program = self.program("running")
        in_open_program = self.task(self.workflow("succeeded", age_days=400, program=open_program))
        child_of_open_root = self.program("succeeded", age_days=400, root=open_program)
        in_child = self.task(self.workflow("succeeded", age_days=400, program=child_of_open_root))
        out = self.call(routes.apply_retention, {"workflow_task_days": 365})
        self.assertEqual(out["workflow_tasks"], {"trimmed": 1, "coding_job_specs": 1})
        rows = {r["id"]: r for r in self.sql("SELECT id, status, input, output, error FROM workflow_tasks")}
        self.assertEqual(rows[old_task]["input"], {"work_order_id": "WO-1", "retention_trimmed": True})
        self.assertEqual(rows[old_task]["output"], {"pr_url": "https://x/pull/1", "retention_trimmed": True})
        self.assertEqual(rows[old_task]["status"], "succeeded")
        self.assertLess(len(rows[old_task]["error"]), 600)
        for untouched in (recent_task, running_task, in_open_program, in_child):
            self.assertIn("work_order", rows[untouched]["input"])
        spec = self.sql("SELECT spec FROM coding_jobs WHERE id = %s", (job,))[0]["spec"]
        self.assertEqual(spec, {"mode": "implement", "retention_trimmed": True})

    def test_audit_window_spares_decisions_and_recent_rows(self):
        def event(action, days):
            self.sql(
                """INSERT INTO runtime_audit_events (event_id, tenant_id, action_type, actor_id, occurred_at)
                   VALUES (%s, 't', %s, 'a', now() - make_interval(days => %s))""",
                (str(uuid.uuid4()), action, days),
            )

        event("tool_call", 800)
        event("skill_approved", 800)
        event("permission_denied", 800)
        event("tool_call", 10)
        self.sql("""INSERT INTO session_audit_log (session_id, agent_id, action_type, summary, occurred_at) VALUES
                    ('s', 'a', 'tool_invocation', 'x', now() - interval '800 days'),
                    ('s', 'a', 'approval_request', 'x', now() - interval '800 days')""")
        out = self.call(routes.apply_retention, {"audit_days": 730})
        self.assertEqual(out["audit_events"], {"deleted": 2, "decisions_deleted": 0})
        left = sorted(r["action_type"] for r in self.sql("SELECT action_type FROM runtime_audit_events"))
        self.assertEqual(left, ["permission_denied", "skill_approved", "tool_call"])
        self.assertEqual([r["action_type"] for r in self.sql("SELECT action_type FROM session_audit_log")], ["approval_request"])
        out = self.call(routes.apply_retention, {"audit_days": 730, "audit_decision_days": 790})
        self.assertEqual(out["audit_events"]["decisions_deleted"], 3)

    def test_expired_uploads_skip_keys_still_in_use_and_are_marked_once(self):
        old = self.program("succeeded", age_days=200, design_keys=["design/t/a.png", "design/t/shared.png"])
        self.program("running", design_keys=["design/t/shared.png"])
        self.program("succeeded", age_days=10, design_keys=["design/t/recent.png"])
        out = self.call(routes.expired_uploads, {"days": 180})
        self.assertEqual(out, {"programs": [str(old)], "object_keys": ["design/t/a.png"], "kept_in_use": 1})
        self.call(routes.mark_uploads_purged, {"programs": [str(old)]})
        self.assertEqual(self.call(routes.expired_uploads, {"days": 180})["programs"], [])
        self.assertEqual(self.call(routes.expired_uploads, {"days": 0})["programs"], [])

    def test_upload_key_named_by_an_open_workflow_task_is_kept(self):
        self.program("succeeded", age_days=200, design_keys=["design/t/b.png"])
        wf = self.workflow("blocked")
        self.task(wf, "blocked", output={"design_refs": [{"object_key": "design/t/b.png"}]})
        self.assertEqual(self.call(routes.expired_uploads, {"days": 180})["object_keys"], [])


@requires_pg
class PurgeProgramTests(_PgBase):
    def seed(self, program_status="succeeded", workflow_status="succeeded"):
        pid = self.program(program_status, design_keys=["design/t/only.png", "design/t/shared.png"])
        self.program("succeeded", design_keys=["design/t/shared.png"])
        wf = self.workflow(workflow_status, program=pid)
        task = self.task(wf, "succeeded" if workflow_status == "succeeded" else "running")
        self.job(wf, task)
        self.sql("INSERT INTO runtime_sessions (session_id, routing_key, tenant_id, agent_id, source, metadata) "
                 "VALUES ('ps', 'pr', 't', 'a', 'web', %s::jsonb)", (json.dumps({"program_id": str(pid)}),))
        self.sql("INSERT INTO runtime_transcript_messages (session_id, seq, role, content) VALUES ('ps', 1, 'user', 'hi')")
        self.sql("INSERT INTO runtime_audit_events (event_id, tenant_id, action_type, actor_id, correlation_id) "
                 "VALUES (%s, 't', 'workflow_started', 'a', %s)", (str(uuid.uuid4()), str(wf)))
        self.sql("INSERT INTO llm_usage (provider, model, program_id, workflow_id, cost_usd) VALUES ('p', 'm', %s, %s, 1.5)",
                 (str(pid), str(wf)))
        self.sql("INSERT INTO workflow_decisions (tenant_id, program_id, workflow_id, kind, decision, actor_id) "
                 "VALUES ('t', %s, %s, 'gate', 'approved', 'a')", (pid, wf))
        return pid, wf

    def test_refuses_unfinished_programs_and_workflows(self):
        pid, _ = self.seed(program_status="running")
        with self.assertRaisesRegex(ValueError, "unfinished"):
            self.call(routes.purge_program, str(pid), {})
        with self.assertRaises(LookupError):
            self.call(routes.purge_program, str(uuid.uuid4()), {})

    def test_dry_run_counts_without_deleting_then_purge_removes_data(self):
        pid, wf = self.seed()
        dry = self.call(routes.purge_program, str(pid), {"dry_run": True})
        self.assertTrue(dry["dry_run"])
        self.assertEqual({k: dry["counts"][k] for k in ("programs", "workflows", "workflow_tasks", "coding_jobs",
                                                        "transcript_messages", "runtime_audit_events", "llm_usage_detached")},
                         {"programs": 1, "workflows": 1, "workflow_tasks": 1, "coding_jobs": 1,
                          "transcript_messages": 1, "runtime_audit_events": 1, "llm_usage_detached": 1})
        self.assertNotIn("workflow_decisions", dry["counts"])
        self.assertEqual(dry["object_keys"], ["design/t/only.png"])
        self.assertEqual(len(self.sql("SELECT 1 FROM workflows WHERE id = %s", (wf,))), 1)

        with LiveMemoryApi(self.db) as api:
            out = MemoryClient(api.url).purge_program_data(str(pid), dry_run=False, actor="ops@example.com")
        self.assertFalse(out["dry_run"])
        self.assertEqual(self.sql("SELECT count(*) AS n FROM workflows")[0]["n"], 0)
        self.assertEqual(self.sql("SELECT count(*) AS n FROM workflow_tasks")[0]["n"], 0)
        self.assertEqual(self.sql("SELECT count(*) AS n FROM coding_jobs")[0]["n"], 0)
        self.assertEqual(self.sql("SELECT count(*) AS n FROM runtime_transcript_messages")[0]["n"], 0)
        self.assertEqual(self.sql("SELECT count(*) AS n FROM delivery_programs WHERE id = %s", (pid,))[0]["n"], 0)
        usage = self.sql("SELECT program_id, workflow_id, cost_usd FROM llm_usage")
        self.assertEqual((usage[0]["program_id"], usage[0]["workflow_id"], float(usage[0]["cost_usd"])), (None, None, 1.5))
        self.assertEqual(self.sql("SELECT count(*) AS n FROM workflow_decisions")[0]["n"], 1)
        audit = self.sql("SELECT action_type, actor_id, payload FROM runtime_audit_events")
        self.assertEqual([(a["action_type"], a["actor_id"]) for a in audit], [("program_data_purged", "ops@example.com")])
        self.assertEqual(audit[0]["payload"]["counts"]["workflows"], 1)


if __name__ == "__main__":
    unittest.main()
