"""Cron, scheduler and delegation state, audit and Slack dedup on the Postgres state backend."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.memory_api_support import TEST_TOKEN, LiveMemoryApi
from tests.pg_support import PgTestCase, requires_pg
from tests.shared_tree_support import FakeSharedFiles

from ts_harness.audit.writer import AuditWriter
from ts_harness.memory.client import MemoryApiConflict, MemoryClient
from ts_harness.storage import shared_tree as shared_tree_mod
from ts_harness.storage.kv import KvConflict, kv_update

REPO_ROOT = Path(__file__).resolve().parents[2]
MERGE_CRON = REPO_ROOT / "scripts" / "ops" / "merge-cron-jobs.py"
PHASE_15 = REPO_ROOT / "scripts" / "phases" / "15-automation-cron.sh"
POSTGRES = {"TS_HARNESS_STATE_BACKEND": "postgres"}


class FakeKv:
    """Same rules as the runtime-kv routes: versions start at 1, ``expected_version`` 0 means absent."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], tuple[object, int]] = {}
        self.guard = threading.Lock()
        self.before_put = None

    def kv_get(self, namespace: str, key: str) -> dict:
        with self.guard:
            value, version = self.values.get((namespace, key), (None, 0))
            return {"value": copy.deepcopy(value), "version": version}

    def kv_put(self, namespace: str, key: str, value, *, expected_version: int | None = None) -> int:
        hook, self.before_put = self.before_put, None
        if hook is not None:
            hook()
        with self.guard:
            _, version = self.values.get((namespace, key), (None, 0))
            if expected_version is not None and expected_version != version:
                raise MemoryApiConflict("version conflict", {"code": "version_conflict", "version": version})
            self.values[(namespace, key)] = (copy.deepcopy(value), version + 1)
            return version + 1


class KvUpdateTests(unittest.TestCase):
    def test_retries_after_a_concurrent_write(self):
        kv = FakeKv()
        kv.before_put = lambda: kv.kv_put("ns", "k", {"n": 10})
        result = kv_update("ns", "k", lambda v: {"n": v["n"] + 1}, default={"n": 0}, client=kv)
        self.assertEqual(result, {"n": 11})
        self.assertEqual(kv.kv_get("ns", "k"), {"value": {"n": 11}, "version": 2})

    def test_gives_up_when_the_value_keeps_changing(self):
        kv = FakeKv()

        def always_conflict(*_a, **_k):
            raise MemoryApiConflict("version conflict", {"code": "version_conflict"})

        kv.kv_put = always_conflict
        with self.assertRaises(KvConflict):
            kv_update("ns", "k", lambda v: v, default={}, client=kv, retries=3)


class _SharedCronBase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.store = FakeSharedFiles()
        shared_tree_mod.reset_shared_trees()
        self.addCleanup(shared_tree_mod.reset_shared_trees)

    def replica_env(self, name: str) -> dict[str, str]:
        cron = self.base / name / "cron" / "runtime-cron-jobs.json"
        shared_tree_mod.shared_tree("cron", cron.parent, self.store).sync_interval = 0
        return {**POSTGRES, "TS_HARNESS_CRON_CONFIG": str(cron)}


class SharedCronTests(_SharedCronBase):
    def _runtime(self):
        runtime = mock.Mock()
        runtime.skill_catalog = None
        return runtime

    def test_an_admin_edit_on_one_replica_reaches_the_leader(self):
        from ts_harness.gateway.admin_bff import create_cron_job_admin, delete_cron_job_admin
        from ts_harness.gateway.scheduler import Scheduler

        config = mock.Mock()
        config.agents = []
        leader_env = self.replica_env("leader")
        with mock.patch.dict(os.environ, leader_env):
            scheduler = Scheduler(config, dispatch=mock.Mock(), cron_jobs=[], leader=lambda: True,
                                  cron_path=Path(leader_env["TS_HARNESS_CRON_CONFIG"]))
            scheduler.run_due_once = mock.Mock()
            scheduler.tick()
        self.assertEqual(scheduler.jobs, [])

        with mock.patch.dict(os.environ, self.replica_env("admin")):
            create_cron_job_admin(self._runtime(), {"name": "nightly", "agent_id": "pm-agent", "interval_seconds": 60})
        self.assertIn("nightly", self.store.live_files("cron")["runtime-cron-jobs.json"])

        with mock.patch.dict(os.environ, leader_env):
            scheduler.tick()
            self.assertEqual([job.name for job in scheduler.jobs], ["nightly"])
            with mock.patch.dict(os.environ, self.replica_env("admin")):
                delete_cron_job_admin(self._runtime(), "nightly")
            scheduler.tick()
        self.assertEqual(scheduler.jobs, [])

    def test_a_failed_edit_leaves_the_stored_file_alone(self):
        from ts_harness.gateway.admin_bff import create_cron_job_admin

        with mock.patch.dict(os.environ, self.replica_env("admin")):
            create_cron_job_admin(self._runtime(), {"name": "nightly", "agent_id": "pm-agent", "interval_seconds": 60})
            before = dict(self.store.live_files("cron"))
            with self.assertRaises(ValueError):
                create_cron_job_admin(self._runtime(), {"name": "nightly", "agent_id": "pm-agent", "interval_seconds": 60})
        self.assertEqual(self.store.live_files("cron"), before)

    def test_local_mode_keeps_writing_the_plain_file(self):
        from ts_harness.gateway.cron_store import load_cron_file, save_cron_file

        path = self.base / "local" / "cron.json"
        with mock.patch.dict(os.environ, {"TS_HARNESS_STATE_BACKEND": "local", "TS_HARNESS_CRON_CONFIG": str(path)}):
            save_cron_file([{"name": "a"}])
            self.assertEqual(load_cron_file(), [{"name": "a"}])
        self.assertEqual(self.store.live_files("cron"), {})


class SchedulerStateTests(unittest.TestCase):
    def _scheduler(self, leader):
        from ts_harness.config.schema import load_runtime_config
        from ts_harness.gateway.scheduler import Scheduler

        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        dispatched: list[dict] = []
        scheduler = Scheduler(
            config,
            dispatched.append,
            cron_jobs=[{"name": "hourly", "agent_id": "orchestrator", "prompt": "go", "interval_seconds": 3600}],
            leader=leader,
        )
        scheduler.jobs = [job for job in scheduler.jobs if job.name == "hourly"]
        return scheduler, dispatched

    def _drain(self, dispatched: list[dict]) -> list[str]:
        for thread in threading.enumerate():
            if thread is not threading.current_thread() and thread.daemon and thread.name.startswith("Thread"):
                thread.join(timeout=1)
        return [p.get("name") for p in dispatched]

    def test_last_run_survives_a_change_of_leader(self):
        kv = FakeKv()
        leading = {"a": True, "b": False}
        with mock.patch.dict(os.environ, POSTGRES), mock.patch("ts_harness.memory.client.get_memory_client", return_value=kv):
            a, dispatched_a = self._scheduler(lambda: leading["a"])
            b, dispatched_b = self._scheduler(lambda: leading["b"])
            with mock.patch("ts_harness.storage.shared_tree.sync_cron"):
                self.assertTrue(a.tick())
                self.assertFalse(b.tick())
                self.assertEqual(self._drain(dispatched_a), ["hourly"])
                stored = kv.kv_get("scheduler", "state")["value"]
                self.assertIn("hourly", stored)

                leading.update(a=False, b=True)
                self.assertTrue(b.tick())
        self.assertEqual(self._drain(dispatched_b), [])
        self.assertEqual(b._state["hourly"]["last_run"], stored["hourly"]["last_run"])

    def test_saving_keeps_newer_runs_from_other_replicas(self):
        from ts_harness.gateway.scheduler import Scheduler

        merged = Scheduler._merge_state({"a": {"last_run": 20.0}, "b": {"last_run": 5.0}}, {"a": {"last_run": 10.0}, "b": {"last_run": 7.0}})
        self.assertEqual(merged, {"a": {"last_run": 20.0}, "b": {"last_run": 7.0}})

    def test_local_mode_uses_the_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            from ts_harness.gateway.scheduler import Scheduler

            config = mock.Mock()
            config.agents = []
            path = Path(tmp) / "scheduler.json"
            scheduler = Scheduler(config, dispatch=mock.Mock(), state_path=path)
            scheduler._state = {"x": {"last_run": 1.0}}
            scheduler._save_state()
            self.assertEqual(json.loads(path.read_text()), {"x": {"last_run": 1.0}})


class DelegationStateTests(unittest.TestCase):
    def setUp(self) -> None:
        from ts_harness.tools import delegation

        self.delegation = delegation
        saved = (delegation._ACTIVE_CHILDREN, dict(delegation._subagent_children))

        def restore():
            delegation._ACTIVE_CHILDREN, delegation._subagent_children = saved[0], saved[1]

        self.addCleanup(restore)
        self.kv = FakeKv()
        for patcher in (
            mock.patch.dict(os.environ, POSTGRES),
            mock.patch("ts_harness.memory.client.get_memory_client", return_value=self.kv),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_children_and_counts_retry_after_a_compare_and_set_conflict(self):
        d = self.delegation
        d._record_child("parent-1", "child-a")
        # Another replica records a child between our read and our write.
        self.kv.before_put = lambda: kv_update(
            "delegation", "state", lambda s: {**s, "children": {**s["children"], "parent-1": s["children"]["parent-1"] + ["child-b"]}},
            client=self.kv,
        )
        d._record_child("parent-1", "child-c")
        stored = self.kv.kv_get("delegation", "state")["value"]
        self.assertEqual(stored["children"]["parent-1"], ["child-a", "child-b", "child-c"])
        self.assertEqual(d._children_of("parent-1"), ["child-a", "child-b", "child-c"])

        d._adjust_active(1)
        d._adjust_active(1)
        d._adjust_active(-1)
        self.assertEqual(self.kv.kv_get("delegation", "state")["value"]["active_children"], 1)
        d._adjust_active(-5)
        self.assertEqual(d._ACTIVE_CHILDREN, 0)

    def test_load_reads_the_shared_state(self):
        self.kv.kv_put("delegation", "state", {"active_children": 2, "children": {"p": ["c"]}})
        self.delegation._load_state()
        self.assertEqual(self.delegation._ACTIVE_CHILDREN, 2)
        self.assertEqual(self.delegation._subagent_children, {"p": ["c"]})


class FakeAuditApi:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.down = False

    def append_audit_events(self, events):
        if self.down:
            raise ConnectionError("memory API unreachable")
        inserted = 0
        for event in events:
            if event["event_id"] not in self.rows:
                self.rows[event["event_id"]] = dict(event)
                inserted += 1
        return {"inserted": inserted, "received": len(events)}

    def list_audit_events(self, *, tenant_id="", since="", until=""):
        rows = list(self.rows.values())
        return [r for r in rows if not tenant_id or r["tenant_id"] == tenant_id]


class AuditWriterSharedTests(unittest.TestCase):
    def test_append_and_export_go_to_the_memory_api(self):
        api = FakeAuditApi()
        audit = AuditWriter(path="/nonexistent/audit.jsonl", shared=True, client=api)
        audit.append(tenant_id="t1", action_type="a", actor_id="u", payload={"api_key": "x"})
        audit.append(tenant_id="t2", action_type="b", actor_id="u")
        self.assertIsNone(audit.path)
        self.assertEqual(len(api.rows), 2)
        exported = audit.export(tenant_id="t1")
        self.assertEqual([e["action_type"] for e in exported], ["a"])
        self.assertEqual(exported[0]["payload"], {"api_key": "[REDACTED]"})

    def test_events_wait_while_the_api_is_down_and_flush_on_the_next_append(self):
        api = FakeAuditApi()
        audit = AuditWriter(shared=True, client=api)
        api.down = True
        with self.assertLogs("ts_harness.audit.writer", "WARNING"):
            audit.append(tenant_id="t", action_type="a", actor_id="u")
            audit.append(tenant_id="t", action_type="b", actor_id="u")
        self.assertEqual(audit.pending(), 2)
        self.assertEqual(api.rows, {})
        api.down = False
        audit.append(tenant_id="t", action_type="c", actor_id="u")
        self.assertEqual(audit.pending(), 0)
        self.assertEqual(sorted(e["action_type"] for e in api.rows.values()), ["a", "b", "c"])

    def test_export_includes_events_still_waiting(self):
        api = FakeAuditApi()
        audit = AuditWriter(shared=True, client=api)
        api.down = True
        with self.assertLogs("ts_harness.audit.writer", "WARNING"):
            audit.append(tenant_id="t", action_type="a", actor_id="u")
            api.list_audit_events = lambda **_: []
            self.assertEqual([e["action_type"] for e in audit.export(tenant_id="t")], ["a"])

    def test_the_buffer_drops_the_oldest_events_when_full(self):
        api = FakeAuditApi()
        api.down = True
        audit = AuditWriter(shared=True, client=api, buffer_limit=3)
        with self.assertLogs("ts_harness.audit.writer", "WARNING") as logs:
            for i in range(5):
                audit.append(tenant_id="t", action_type=f"a{i}", actor_id="u")
        self.assertEqual(audit.pending(), 3)
        self.assertTrue(any("dropped" in line for line in logs.output))
        api.down = False
        audit.flush()
        self.assertEqual(sorted(e["action_type"] for e in api.rows.values()), ["a2", "a3", "a4"])

    def test_local_mode_keeps_the_jsonl_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            AuditWriter(path=path, shared=False).append(tenant_id="t", action_type="a", actor_id="u")
            self.assertEqual(len(AuditWriter(path=path, shared=False).export()), 1)


class SlackDedupTests(unittest.TestCase):
    def _event(self, ts: str = "1.0") -> dict:
        return {"type": "message", "channel": "C1", "user": "U1", "text": "hi", "ts": ts}

    def test_two_adapters_handle_an_event_once(self):
        from ts_harness.gateway.channels.slack import SlackAdapter

        seen: set[str] = set()
        client = mock.Mock()
        client.mark_channel_event.side_effect = lambda key: not (key in seen or seen.add(key))
        a = SlackAdapter(token="t", dedup_client=client)
        b = SlackAdapter(token="t", dedup_client=client)
        a.normalize_inbound(self._event(), tenant_id="default")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            b.normalize_inbound(self._event(), tenant_id="default")
        b.normalize_inbound(self._event("2.0"), tenant_id="default")
        self.assertEqual(sorted(seen), ["slack:1.0", "slack:2.0"])
        self.assertIsNone(a.dedup_path)

    def test_falls_back_to_local_memory_when_the_api_is_down(self):
        from ts_harness.gateway.channels.slack import SlackAdapter

        client = mock.Mock()
        client.mark_channel_event.side_effect = ConnectionError("down")
        adapter = SlackAdapter(token="t", dedup_client=client)
        with self.assertLogs("ts_harness.gateway.channels.slack", "WARNING"):
            adapter.normalize_inbound(self._event(), tenant_id="default")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            adapter.normalize_inbound(self._event(), tenant_id="default")


class Phase15MergeTests(unittest.TestCase):
    def _run(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(MERGE_CRON), *args], capture_output=True, text=True, env=env, timeout=60)

    def test_merge_replaces_system_jobs_by_name_and_keeps_admin_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            cron = Path(tmp) / "runtime-cron-jobs.json"
            cron.write_text(json.dumps({"jobs": [
                {"name": "daily-standup", "cron": "0 7 * * *"},
                {"name": "admin-added", "interval_seconds": 60},
            ]}))
            system = Path(tmp) / "system.json"
            system.write_text(json.dumps({"jobs": [
                {"name": "daily-standup", "cron": "0 9 * * *"},
                {"name": "poll-github-merges", "interval_seconds": 60},
            ]}))
            result = self._run("--jobs", str(system), "--file", str(cron))
            self.assertEqual(result.returncode, 0, result.stderr)
            jobs = json.loads(cron.read_text())["jobs"]
            self.assertEqual([j["name"] for j in jobs], ["daily-standup", "admin-added", "poll-github-merges"])
            self.assertEqual(jobs[0]["cron"], "0 9 * * *")
            # Running it again changes nothing.
            before = cron.read_text()
            self._run("--jobs", str(system), "--file", str(cron))
            self.assertEqual(cron.read_text(), before)

    def test_memory_api_mode_needs_a_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = Path(tmp) / "system.json"
            system.write_text('{"jobs": []}')
            env = {k: v for k, v in os.environ.items() if k != "MEMORY_API_TOKEN"}
            result = self._run("--jobs", str(system), "--memory-api", "http://127.0.0.1:9", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MEMORY_API_TOKEN", result.stderr)

    def test_phase_15_merges_instead_of_overwriting(self):
        phase = PHASE_15.read_text()
        self.assertIn("merge-cron-jobs.py", phase)
        self.assertIn('--file "$CRON_CONFIG"', phase)
        self.assertIn("--memory-api", phase)
        self.assertIn('TS_HARNESS_STATE_BACKEND:-local}" == "postgres"', phase)
        self.assertIn('python3 - "$SYSTEM_JOBS"', phase)
        self.assertNotIn('python3 - "$CRON_CONFIG"', phase)


@requires_pg
class SharedStatePostgresTests(PgTestCase):
    def setUp(self):
        with self.db.connect() as conn:
            for table in ("shared_files", "shared_file_namespaces", "shared_file_locks", "runtime_kv", "runtime_audit_events", "channel_event_dedup"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()

    def test_phase_15_merge_through_the_memory_api_keeps_admin_jobs(self):
        with LiveMemoryApi(self.db) as api, tempfile.TemporaryDirectory() as tmp:
            client = MemoryClient(api.url)
            tree = shared_tree_mod.SharedTree("cron", Path(tmp) / "cron", client, sync_interval=0)
            with tree.transaction("cron-file", ["runtime-cron-jobs.json"]):
                (Path(tmp) / "cron" / "runtime-cron-jobs.json").write_text(json.dumps({"jobs": [{"name": "admin-added"}]}))
            system = Path(tmp) / "system.json"
            system.write_text(json.dumps({"jobs": [{"name": "poll-github-merges", "interval_seconds": 60}]}))
            env = {**os.environ, "MEMORY_API_TOKEN": TEST_TOKEN}
            result = subprocess.run([sys.executable, str(MERGE_CRON), "--jobs", str(system), "--memory-api", api.url],
                                    capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            tree.sync(force=True)
            jobs = json.loads((Path(tmp) / "cron" / "runtime-cron-jobs.json").read_text())["jobs"]
        self.assertEqual([j["name"] for j in jobs], ["admin-added", "poll-github-merges"])

    def test_audit_kv_and_dedup_through_the_real_routes(self):
        with LiveMemoryApi(self.db) as api:
            client = MemoryClient(api.url)
            audit = AuditWriter(shared=True, client=client)
            event = audit.append(tenant_id="t1", action_type="skill_approved", actor_id="admin")
            audit.append(tenant_id="t2", action_type="other", actor_id="admin")
            client.append_audit_events([{**audit._to_dict(event)}])
            exported = AuditWriter(shared=True, client=client).export(tenant_id="t1")
            self.assertEqual([(e["event_id"], e["action_type"]) for e in exported], [(event.event_id, "skill_approved")])

            self.assertEqual(kv_update("scheduler", "state", lambda s: {**s, "j": {"last_run": 1.0}}, default={}, client=client),
                             {"j": {"last_run": 1.0}})
            self.assertEqual(client.kv_get("scheduler", "state")["version"], 1)

            from ts_harness.gateway.channels.slack import SlackAdapter

            a, b = SlackAdapter(token="t", dedup_client=client), SlackAdapter(token="t", dedup_client=client)
            a.normalize_inbound({"type": "message", "channel": "C1", "user": "U1", "ts": "9.9"}, tenant_id="default")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                b.normalize_inbound({"type": "message", "channel": "C1", "user": "U1", "ts": "9.9"}, tenant_id="default")


class StartupCheckTests(unittest.TestCase):
    def test_postgres_state_needs_the_session_database(self):
        from ts_harness.storage.backend import StorageConfigError, validate_storage_config

        env = {k: v for k, v in os.environ.items() if k != "TS_HARNESS_SESSION_DB_URL"}
        with mock.patch.dict(os.environ, {**env, **POSTGRES}, clear=True):
            with self.assertRaisesRegex(StorageConfigError, "TS_HARNESS_SESSION_DB_URL"):
                validate_storage_config()


if __name__ == "__main__":
    unittest.main()
