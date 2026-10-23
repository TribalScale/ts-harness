"""SharedTree: definition stores on a Postgres file tree with a local mirror per process."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.memory_api_support import LiveMemoryApi
from tests.pg_support import PgTestCase, requires_pg
from tests.runtime import test_agents_overlay_store as agents_suite
from tests.runtime import test_mcp_settings_store as mcp_suite
from tests.runtime import test_program_definition_store as program_suite
from tests.runtime import test_routing_overlay as routing_suite
from tests.runtime import test_runtime_settings_store as settings_suite
from tests.runtime import test_workflow_definition_store as workflow_suite
from tests.runtime import test_workflow_fragments as fragment_suite
from tests.shared_tree_support import FakeSharedFiles, SharedTreeBackendMixin

from ts_harness.memory.client import MemoryClient
from ts_harness.storage import shared_tree as shared_tree_mod
from ts_harness.storage.backend import apply_mirror_paths
from ts_harness.storage.shared_tree import SharedTree, sync_definitions
from ts_harness.workflows.definition_store import WorkflowDefinitionStore
from ts_harness.workflows.routing_definition_store import RoutingDefinitionStore
from ts_harness.workflows.versioned_definition_store import ConflictError


def _definition(version: str = "1.0.0", workflow_type: str = "custom_flow") -> dict:
    return {
        "workflow_type": workflow_type,
        "definition_version": version,
        "stages": [{"name": "start", "kind": "task", "agent_id": "agent-a", "approval_required": True}],
    }


def _activate(store: WorkflowDefinitionStore, version: str = "1.0.0", workflow_type: str = "custom_flow") -> dict:
    draft = store.create_draft(_definition(version, workflow_type), actor_id="alice")
    return store.activate(draft["id"], expected_revision=draft["revision"], actor_id="alice")


# The existing store suites, run again with every write going through a SharedTree.

class SharedWorkflowDefinitionStoreTests(SharedTreeBackendMixin, workflow_suite.WorkflowDefinitionStoreTests):
    shared_tree_skip = frozenset({
        "test_concurrent_activation_preserves_manifest_entries",
        # Local lockfile mechanics; the shared path locks in Postgres instead.
        "test_store_lock_recovers_stale_exclusive_lockfile",
        "test_store_lock_uses_exclusive_lockfile_when_fcntl_unavailable",
        # These write half-finished files straight into the mirror. With a shared tree a failed
        # activation rolls back as a whole: see test_failed_activation_rolls_back_as_a_whole.
        "test_activate_resumes_after_exact_history_exists",
        "test_activate_updates_manifest_before_active_file_and_keeps_draft_on_failure",
    })

    def test_failed_activation_rolls_back_as_a_whole(self):
        from ts_harness.workflows import versioned_definition_store

        draft = self.store.create_draft(self.definition(version="atomic"), actor_id="a")
        original_write = versioned_definition_store._atomic_write_json

        def fail_active(path, payload):
            if path == self.root / "active" / "custom_flow.json":
                raise OSError("active write failed")
            original_write(path, payload)

        with mock.patch.object(versioned_definition_store, "_atomic_write_json", side_effect=fail_active):
            with self.assertRaises(OSError):
                self.store.activate(draft["id"], expected_revision=draft["revision"], actor_id="a")
        self.assertEqual(self.store.read_manifest()["generation"], 0)
        self.assertFalse((self.root / "history").exists() and any((self.root / "history").rglob("*.json")))
        self.assertEqual(self.store.get_draft(draft["id"])["revision"], draft["revision"])
        summary = self.store.activate(draft["id"], expected_revision=draft["revision"], actor_id="a")
        self.assertEqual(summary["definition_version"], "atomic")


class SharedProgramDefinitionStoreTests(SharedTreeBackendMixin, program_suite.ProgramDefinitionStoreTests):
    shared_tree_skip = frozenset({
        "test_concurrent_program_activation_preserves_manifest_entries",
        "test_concurrent_workflow_and_program_activations_keep_namespaces_isolated",
    })


class SharedAgentsOverlayStoreTests(SharedTreeBackendMixin, agents_suite.AgentsOverlayStoreTests):
    pass


class SharedMcpSettingsStoreTests(SharedTreeBackendMixin, mcp_suite.McpSettingsStoreTests):
    pass


class SharedRuntimeSettingsStoreTests(SharedTreeBackendMixin, settings_suite.RuntimeSettingsStoreTests):
    pass


class SharedRoutingStoreTests(SharedTreeBackendMixin, routing_suite.RoutingStoreActivateTests):
    pass


class SharedFragmentStoreTests(SharedTreeBackendMixin, fragment_suite.ManagedFragmentTests):
    pass


class SuitesReallyUseTheTreeTests(SharedTreeBackendMixin, unittest.TestCase):
    def test_writes_are_committed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowDefinitionStore(Path(tmp))
            _activate(store)
            tree = self.shared_trees[Path(tmp).resolve()]
            stored = tree.client.live_files("definitions")
            self.assertIn("active/manifest.json", stored)
            self.assertIn("active/custom_flow.json", stored)
            self.assertEqual(len(tree.client.commits), 2)
            self.assertFalse((Path(tmp) / ".store.lock").exists())


class _Trees(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeSharedFiles()

    def tree(self, name: str, fake: FakeSharedFiles | None = None) -> SharedTree:
        return SharedTree("definitions", Path(self.tmp.name) / name, fake or self.fake, sync_interval=0)

    def route(self, *trees: SharedTree):
        """Send each store root to the tree whose mirror contains it."""

        def tree_for(path):
            resolved = Path(path).resolve()
            for tree in trees:
                try:
                    return tree, resolved.relative_to(tree.root.resolve()).as_posix()
                except ValueError:
                    continue
            return None

        patcher = mock.patch.object(shared_tree_mod, "definitions_tree_for", side_effect=tree_for)
        patcher.start()
        self.addCleanup(patcher.stop)


class SharedTreeTests(_Trees):
    def test_activation_in_one_mirror_is_seen_by_the_other(self):
        a, b = self.tree("a"), self.tree("b")
        self.route(a, b)
        _activate(WorkflowDefinitionStore(a.root))
        self.assertTrue(b.sync())
        manifest = json.loads((b.root / "active" / "manifest.json").read_text())
        self.assertEqual(manifest["generation"], 1)
        self.assertEqual(WorkflowDefinitionStore(b.root).get_active("custom_flow")["definition_version"], "1.0.0")
        self.assertFalse(b.sync())

    def test_nested_stores_in_one_tree_do_not_refuse_each_other(self):
        a, b = self.tree("a"), self.tree("b")
        self.route(a, b)
        _activate(WorkflowDefinitionStore(a.root))
        routing = RoutingDefinitionStore(b.root / "routing")
        draft = routing.create_draft(
            {"definition_type": "channel_routing", "definition_version": "1.0.0", "intents": []}, actor_id="admin"
        )
        routing.activate(draft["id"], expected_revision=draft["revision"], actor_id="admin")
        _activate(WorkflowDefinitionStore(a.root), version="1.1.0")
        self.assertTrue((a.root / "routing" / "active" / "manifest.json").is_file())
        b.sync()
        self.assertEqual(WorkflowDefinitionStore(b.root).get_active("custom_flow")["definition_version"], "1.1.0")
        locks = {c["puts"][0].split("/")[0] for c in self.fake.commits}
        self.assertEqual(locks, {"drafts", "active", "routing"})

    def test_stale_commit_raises_conflict_and_restores_the_mirror(self):
        a = self.tree("a")
        self.route(a)
        store = WorkflowDefinitionStore(a.root)
        draft = store.create_draft(_definition(), actor_id="alice")
        with self.assertRaises(ConflictError):
            with a.transaction("store:root", ["drafts", "active", "history"]):
                (a.root / "drafts" / "mine.json").write_text("{}")
                self.fake.commit_shared_files(
                    "definitions", base_generation=None, puts=[{"path": "drafts/theirs.json", "content": "{}"}], deletes=[]
                )
        names = sorted(p.name for p in (a.root / "drafts").iterdir())
        self.assertEqual(names, [f"{draft['id']}.json", "theirs.json"])

    def test_failed_body_restores_files_even_before_any_import(self):
        a = self.tree("a")
        (a.root / "drafts").mkdir(parents=True)
        (a.root / "drafts" / "keep.json").write_text("old")
        with self.assertRaises(RuntimeError):
            with a.transaction("store:root", ["drafts"]):
                (a.root / "drafts" / "keep.json").write_text("new")
                (a.root / "drafts" / "stray.json").write_text("{}")
                raise RuntimeError("boom")
        self.assertEqual(sorted(p.name for p in (a.root / "drafts").iterdir()), ["keep.json"])
        self.assertEqual((a.root / "drafts" / "keep.json").read_text(), "old")
        self.assertEqual(self.fake.commits, [])

    def test_nested_transactions_commit_once(self):
        a = self.tree("a")
        with a.transaction("store:root", ["drafts"]):
            (a.root / "drafts").mkdir(parents=True)
            with a.transaction("store:root", ["drafts"]):
                (a.root / "drafts" / "x.json").write_text("1")
            (a.root / "drafts" / "y.json").write_text("2")
        self.assertEqual(len(self.fake.commits), 1)
        self.assertEqual(sorted(self.fake.commits[0]["puts"]), ["drafts/x.json", "drafts/y.json"])

    def test_lock_wait_times_out(self):
        a = self.tree("a")
        a.lock_wait = 0.1
        self.fake.acquire_shared_lock("definitions", "store:root", holder="someone-else")
        with self.assertRaises(TimeoutError):
            with a.transaction("store:root", ["drafts"]):
                pass

    def test_binary_files_are_refused(self):
        a = self.tree("a")
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            with a.transaction("store:root", ["drafts"]):
                (a.root / "drafts").mkdir(parents=True)
                (a.root / "drafts" / "x.json").write_bytes(b"\xff\xfe")
        self.assertFalse((a.root / "drafts" / "x.json").exists())

    def test_full_sync_prunes_files_the_store_does_not_have_and_hidden_files_are_ignored(self):
        self.fake.commit_shared_files("definitions", base_generation=None, puts=[{"path": "active/a.json", "content": "1"}], deletes=[])
        b = self.tree("b")
        (b.root / "active").mkdir(parents=True)
        (b.root / "active" / "stale.json").write_text("old")
        (b.root / ".store.lock").write_text("")
        self.assertTrue(b.sync())
        self.assertEqual(sorted(b.local_files()), ["active/a.json"])
        self.assertTrue((b.root / ".store.lock").exists())

    def test_generation_persists_across_restarts_and_goes_back_after_a_restore(self):
        self.fake.commit_shared_files("definitions", base_generation=None, puts=[{"path": "a", "content": "1"}], deletes=[])
        b = self.tree("b")
        b.sync()
        self.assertEqual(self.tree("b").generation, 1)
        restored = FakeSharedFiles()
        restored.commit_shared_files("definitions", base_generation=None, puts=[{"path": "z", "content": "9"}], deletes=[])
        self.fake.commit_shared_files("definitions", base_generation=None, puts=[{"path": "c", "content": "3"}], deletes=[])
        b.sync()
        again = self.tree("b", restored)
        self.assertEqual(again.generation, 2)
        again.sync()
        self.assertEqual(sorted(again.local_files()), ["z"])

    def test_sync_is_throttled_unless_forced(self):
        a = SharedTree("definitions", Path(self.tmp.name) / "a", self.fake, sync_interval=60)
        a.sync()
        self.fake.commit_shared_files("definitions", base_generation=None, puts=[{"path": "a", "content": "1"}], deletes=[])
        self.assertFalse(a.sync())
        self.assertTrue(a.sync(force=True))


class ReaderSyncTests(_Trees):
    def test_engine_picks_up_an_activation_from_another_replica(self):
        from ts_harness.workflows.engine import WorkflowEngine

        a, b = self.tree("a"), self.tree("b")
        self.route(a, b)
        env = {
            "TS_HARNESS_STATE_BACKEND": "postgres",
            "TS_HARNESS_WORKFLOW_DEFINITION_STORE": str(b.root),
            "TS_HARNESS_MANAGED_WORKFLOWS_DIR": str(b.root / "active"),
        }
        with mock.patch.dict(os.environ, env), mock.patch.object(shared_tree_mod, "definitions_tree", return_value=b):
            engine = WorkflowEngine(client=mock.Mock())
            self.assertIsNone(engine._definition_manifest_generation)
            _activate(WorkflowDefinitionStore(a.root))
            self.assertTrue(engine.reload_if_changed())
            self.assertEqual(engine._definition_manifest_generation, 1)
            self.assertEqual(str(engine.skeleton("custom_flow").definition_version), "1.0.0")

    def test_builtin_history_snapshots_go_through_the_tree(self):
        from ts_harness.workflows.definition_paths import ensure_history_snapshot
        from ts_harness.workflows.materialize import list_skeleton_types, load_skeleton
        from ts_harness.repo_paths import workflows_dir

        a = self.tree("a")
        self.route(a)
        workflow_type = list_skeleton_types(workflows_dir())[0]
        path = ensure_history_snapshot(a.root, load_skeleton(workflow_type, workflows_dir()))
        self.assertTrue(path.is_file())
        self.assertIn(path.relative_to(a.root).as_posix(), self.fake.live_files("definitions"))
        ensure_history_snapshot(a.root, load_skeleton(workflow_type, workflows_dir()))
        self.assertEqual(len(self.fake.commits), 1)

    def test_reader_sync_never_raises(self):
        broken = mock.Mock()
        broken.pull_shared_files.side_effect = RuntimeError("memory api down")
        tree = SharedTree("definitions", Path(self.tmp.name) / "x", broken, sync_interval=0)
        with mock.patch.object(shared_tree_mod, "definitions_tree", return_value=tree):
            self.assertFalse(sync_definitions(force=True))

    def test_sync_definitions_is_a_no_op_in_local_mode(self):
        with mock.patch.dict(os.environ, {"TS_HARNESS_STATE_BACKEND": "local"}):
            self.assertIsNone(shared_tree_mod.definitions_tree())
            self.assertIsNone(shared_tree_mod.definitions_tree_for(self.tmp.name))
            self.assertFalse(sync_definitions(force=True))

    def test_admin_definition_reads_force_a_sync(self):
        from starlette.testclient import TestClient

        from ts_harness.gateway import asgi_app

        calls = []
        with mock.patch.object(shared_tree_mod, "sync_definitions", side_effect=lambda **kw: calls.append(kw)):
            client = TestClient(asgi_app.app, raise_server_exceptions=False)
            client.get("/admin/workflow-definitions")
            client.get("/admin/sessions")
        self.assertEqual(calls, [{"force": True}])

    def test_runtime_watcher_reloads_only_changed_settings(self):
        from ts_harness.gateway.server import RuntimeState

        state = RuntimeState.__new__(RuntimeState)
        state._seen_settings_generations = {"settings": 1, "routing": 1, "mcp": 1, "agents": 1}
        with mock.patch.object(shared_tree_mod, "sync_definitions", return_value=True), \
                mock.patch.object(RuntimeState, "_settings_generations", return_value={"settings": 1, "routing": 2, "mcp": 1, "agents": 1}), \
                mock.patch("ts_harness.gateway.routing_definitions_bff.reload_routing_into_runtime") as routing, \
                mock.patch("ts_harness.config.runtime_settings.reload_runtime_settings_into_runtime") as settings:
            self.assertEqual(state.apply_shared_definition_changes(), ["routing"])
        routing.assert_called_once_with(state)
        settings.assert_not_called()
        with mock.patch.object(shared_tree_mod, "sync_definitions", return_value=False):
            self.assertEqual(state.apply_shared_definition_changes(), [])


class MirrorPathTests(unittest.TestCase):
    def test_postgres_mode_points_path_variables_into_the_mirror(self):
        env = {
            "TS_HARNESS_STATE_BACKEND": "postgres",
            "TS_HARNESS_OBJECT_BACKEND": "s3",
            "TS_HARNESS_MIRROR_DIR": "/m",
            "TS_HARNESS_WORKFLOW_DEFINITION_STORE": "/workflow-definitions",
            "TS_HARNESS_ROUTING_DEFINITION_STORE": "/elsewhere",
            "SKILLS_MANAGED_DIR": "/skills-managed",
        }
        with mock.patch.dict(os.environ, env):
            changed = apply_mirror_paths()
            self.assertEqual(os.environ["TS_HARNESS_WORKFLOW_DEFINITION_STORE"], "/m/definitions")
            self.assertEqual(os.environ["TS_HARNESS_PROGRAM_DEFINITION_STORE"], "/m/definitions/programs")
            self.assertEqual(os.environ["TS_HARNESS_MANAGED_WORKFLOWS_DIR"], "/m/definitions/active")
            self.assertEqual(os.environ["TS_HARNESS_CRON_CONFIG"], "/m/cron/runtime-cron-jobs.json")
            self.assertEqual(os.environ["SKILLS_MANAGED_DIR"], "/m/skills/current")
            self.assertNotIn("TS_HARNESS_ROUTING_DEFINITION_STORE", os.environ)
            self.assertIn("TS_HARNESS_WORKFLOW_DEFINITION_STORE", changed)
            self.assertEqual(apply_mirror_paths(), {})

    def test_local_mode_changes_nothing(self):
        env = {"TS_HARNESS_STATE_BACKEND": "local", "TS_HARNESS_OBJECT_BACKEND": "local",
               "TS_HARNESS_WORKFLOW_DEFINITION_STORE": "/workflow-definitions"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(apply_mirror_paths(), {})
            self.assertEqual(os.environ["TS_HARNESS_WORKFLOW_DEFINITION_STORE"], "/workflow-definitions")


@requires_pg
class SharedTreePostgresTests(PgTestCase):
    def setUp(self):
        with self.db.connect() as conn:
            for table in ("shared_files", "shared_file_namespaces", "shared_file_locks"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_two_replicas_through_the_real_routes(self):
        with LiveMemoryApi(self.db) as api:
            client = MemoryClient(api.url)
            a = SharedTree("definitions", Path(self.tmp.name) / "a", client, sync_interval=0)
            b = SharedTree("definitions", Path(self.tmp.name) / "b", client, sync_interval=0)

            def tree_for(path):
                for tree in (a, b):
                    try:
                        return tree, Path(path).resolve().relative_to(tree.root.resolve()).as_posix()
                    except ValueError:
                        continue
                return None

            with mock.patch.object(shared_tree_mod, "definitions_tree_for", side_effect=tree_for):
                store_a = WorkflowDefinitionStore(a.root)
                store_b = WorkflowDefinitionStore(b.root)
                _activate(store_a)
                draft = store_b.create_draft(_definition("1.1.0"), actor_id="bob")
                self.assertEqual(store_b.get_active("custom_flow")["definition_version"], "1.0.0")
                a.sync()
                self.assertEqual(store_a.get_draft(draft["id"])["created_by"], "bob")
                store_a.activate(draft["id"], expected_revision=draft["revision"], actor_id="alice")
                b.sync()
                self.assertEqual(store_b.get_active("custom_flow")["definition_version"], "1.1.0")
                self.assertFalse((b.root / "drafts" / f"{draft['id']}.json").exists())
            with self.db.connect() as conn:
                locks = conn.execute("SELECT count(*) AS n FROM shared_file_locks").fetchone()["n"]
            self.assertEqual(locks, 0)


if __name__ == "__main__":
    unittest.main()
