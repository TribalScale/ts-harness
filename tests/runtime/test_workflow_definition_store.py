from __future__ import annotations

import concurrent.futures
import json
import multiprocessing
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ts_harness.workflows.definition_store import (
    ConflictError,
    WorkflowDefinitionStore,
)


def _activate_draft_with_manifest_delay(args):
    root, draft_id, revision = args
    import time

    from ts_harness.workflows import versioned_definition_store

    original_write = versioned_definition_store._atomic_write_json

    def delayed_write(path, payload):
        if path.name == "manifest.json":
            time.sleep(0.15)
        original_write(path, payload)

    versioned_definition_store._atomic_write_json = delayed_write
    store = WorkflowDefinitionStore(Path(root))
    store.activate(draft_id, expected_revision=revision, actor_id="worker")


class WorkflowDefinitionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = WorkflowDefinitionStore(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def definition(
        self,
        *,
        workflow_type: str = "custom_flow",
        version: str = "1.0.0",
    ):
        return {
            "workflow_type": workflow_type,
            "definition_version": version,
            "stages": [
                {
                    "name": "start",
                    "kind": "task",
                    "agent_id": "agent-a",
                    "approval_required": True,
                }
            ],
        }

    def test_create_update_and_delete_draft(self):
        draft = self.store.create_draft(self.definition(), actor_id="alice")

        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["created_by"], "alice")
        self.assertEqual(
            self.store.get_draft(draft["id"])["revision"],
            draft["revision"],
        )

        updated_definition = self.definition(version="1.0.1")
        updated = self.store.update_draft(
            draft["id"],
            updated_definition,
            expected_revision=draft["revision"],
            actor_id="bob",
        )

        self.assertNotEqual(updated["revision"], draft["revision"])
        self.assertEqual(updated["updated_by"], "bob")
        self.assertEqual(updated["definition"]["definition_version"], "1.0.1")

        self.store.delete_draft(draft["id"])
        with self.assertRaises(FileNotFoundError):
            self.store.get_draft(draft["id"])

    def test_update_draft_rejects_revision_conflict(self):
        draft = self.store.create_draft(self.definition(), actor_id="alice")

        with self.assertRaises(ConflictError):
            self.store.update_draft(
                draft["id"],
                self.definition(version="1.0.1"),
                expected_revision="sha256-stale",
                actor_id="bob",
            )

    def test_activate_writes_active_history_and_manifest(self):
        draft = self.store.create_draft(self.definition(), actor_id="alice")

        summary = self.store.activate(
            draft["id"],
            expected_revision=draft["revision"],
            actor_id="alice",
        )

        self.assertEqual(summary["workflow_type"], "custom_flow")
        self.assertEqual(
            self.store.get_active("custom_flow")["workflow_type"],
            "custom_flow",
        )
        self.assertEqual(len(self.store.list_active()), 1)
        self.assertFalse(
            (self.root / "drafts" / f"{draft['id']}.json").exists()
        )
        manifest = self.store.read_manifest()
        self.assertEqual(manifest["generation"], 1)
        self.assertIn("custom_flow", manifest["workflows"])
        history = list((self.root / "history" / "custom_flow").glob("*.json"))
        self.assertEqual(len(history), 1)

    def test_activate_allows_new_version_and_resumes_same_definition_revision(self):
        first = self.store.create_draft(
            self.definition(version="1.0.0"),
            actor_id="a",
            editor_metadata={"layout": "first"},
        )
        self.store.activate(
            first["id"],
            expected_revision=first["revision"],
            actor_id="a",
        )

        duplicate = self.store.create_draft(
            self.definition(version="1.0.0"),
            actor_id="a",
            editor_metadata={"layout": "different"},
        )
        resumed = self.store.activate(
            duplicate["id"],
            expected_revision=duplicate["revision"],
            actor_id="a",
        )
        self.assertEqual(resumed["definition_version"], "1.0.0")
        self.assertEqual(self.store.read_manifest()["generation"], 1)

        second = self.store.create_draft(
            self.definition(version="1.0.1"),
            actor_id="a",
        )
        self.store.activate(
            second["id"],
            expected_revision=second["revision"],
            actor_id="a",
        )
        self.assertEqual(
            self.store.get_active("custom_flow")["definition_version"],
            "1.0.1",
        )
        manifest = self.store.read_manifest()
        self.assertEqual(manifest["generation"], 2)

    def test_activate_rejects_same_version_with_different_definition_content(self):
        first = self.store.create_draft(
            self.definition(version="1.0.0"),
            actor_id="a",
        )
        self.store.activate(
            first["id"],
            expected_revision=first["revision"],
            actor_id="a",
        )
        changed = self.definition(version="1.0.0")
        changed["stages"][0]["agent_id"] = "agent-b"
        duplicate = self.store.create_draft(changed, actor_id="a")

        with self.assertRaises(ValueError):
            self.store.activate(
                duplicate["id"],
                expected_revision=duplicate["revision"],
                actor_id="a",
            )

    def test_activate_rejects_historical_version_reuse_and_keeps_draft(self):
        first = self.store.create_draft(
            self.definition(version="1.0.0"),
            actor_id="a",
        )
        self.store.activate(
            first["id"],
            expected_revision=first["revision"],
            actor_id="a",
        )
        second = self.store.create_draft(
            self.definition(version="1.0.1"),
            actor_id="a",
        )
        self.store.activate(
            second["id"],
            expected_revision=second["revision"],
            actor_id="a",
        )

        reused = self.store.create_draft(
            {
                **self.definition(version="1.0.0"),
                "display_name": "changed historical content",
            },
            actor_id="a",
            editor_metadata={"layout": "different"},
        )
        with self.assertRaises(ValueError):
            self.store.activate(
                reused["id"],
                expected_revision=reused["revision"],
                actor_id="a",
            )

        self.assertEqual(
            self.store.get_active("custom_flow")["definition_version"],
            "1.0.1",
        )
        self.assertEqual(
            self.store.get_draft(reused["id"])["definition"]["definition_version"],
            "1.0.0",
        )

    def test_rejects_path_traversal_and_builtin_activation(self):
        with self.assertRaises(ValueError):
            self.store.create_draft(
                self.definition(workflow_type="../bad"),
                actor_id="a",
            )
        with self.assertRaises(ValueError):
            self.store.get_draft("../bad")

        store = WorkflowDefinitionStore(
            self.root,
            built_in_types={"prd_request"},
        )
        draft = store.create_draft(
            self.definition(workflow_type="prd_request"),
            actor_id="a",
        )
        with self.assertRaises(ValueError):
            store.activate(
                draft["id"],
                expected_revision=draft["revision"],
                actor_id="a",
            )

    def test_list_definitions_returns_managed_summaries(self):
        draft = self.store.create_draft(self.definition(), actor_id="alice")
        summaries = self.store.list_definitions()

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["id"], draft["id"])
        self.assertNotIn("definition", summaries[0])

    def test_active_files_are_plain_skeleton_json(self):
        draft = self.store.create_draft(self.definition(), actor_id="alice")
        self.store.activate(
            draft["id"],
            expected_revision=draft["revision"],
            actor_id="alice",
        )

        active_payload = json.loads(
            (self.root / "active" / "custom_flow.json").read_text()
        )
        self.assertEqual(active_payload["workflow_type"], "custom_flow")
        self.assertNotIn("status", active_payload)

    def test_draft_editor_metadata_round_trips_and_is_not_activated(self):
        metadata = {"nodes": [{"id": "start", "x": 1, "y": 2}]}
        draft = self.store.create_draft(
            self.definition(),
            actor_id="alice",
            editor_metadata=metadata,
        )

        self.assertEqual(draft["editor_metadata"], metadata)

        updated = self.store.update_draft(
            draft["id"],
            self.definition(version="1.0.1"),
            expected_revision=draft["revision"],
            actor_id="bob",
        )
        self.assertEqual(updated["editor_metadata"], metadata)

        replaced = self.store.update_draft(
            draft["id"],
            self.definition(version="1.0.2"),
            expected_revision=updated["revision"],
            actor_id="carol",
            editor_metadata={"viewport": {"zoom": 0.8}},
        )
        self.assertEqual(
            replaced["editor_metadata"],
            {"viewport": {"zoom": 0.8}},
        )

        self.store.activate(
            replaced["id"],
            expected_revision=replaced["revision"],
            actor_id="carol",
        )
        active_payload = json.loads(
            (self.root / "active" / "custom_flow.json").read_text()
        )
        self.assertNotIn("editor_metadata", active_payload)

    def test_metadata_only_update_changes_revision(self):
        draft = self.store.create_draft(
            self.definition(),
            actor_id="alice",
            editor_metadata={"nodes": [{"id": "start", "x": 1, "y": 2}]},
        )

        updated = self.store.update_draft(
            draft["id"],
            self.definition(),
            expected_revision=draft["revision"],
            actor_id="bob",
            editor_metadata={"nodes": [{"id": "start", "x": 3, "y": 4}]},
        )

        self.assertNotEqual(updated["revision"], draft["revision"])

    def test_concurrent_same_revision_update_conflicts(self):
        draft = self.store.create_draft(self.definition(), actor_id="alice")
        original_read_json = self.store._read_json

        def delayed_read(path):
            payload = original_read_json(path)
            if path == self.root / "drafts" / f"{draft['id']}.json":
                time.sleep(0.1)
            return payload

        self.store._read_json = delayed_read

        def update(worker):
            try:
                return self.store.update_draft(
                    draft["id"],
                    self.definition(version="1.0.1"),
                    expected_revision=draft["revision"],
                    actor_id=worker,
                    editor_metadata={"worker": worker},
                )
            except ConflictError as exc:
                return exc

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(update, ["one", "two"]))

        self.assertEqual(
            sum(isinstance(result, ConflictError) for result in results),
            1,
        )
        self.assertEqual(
            sum(isinstance(result, dict) for result in results),
            1,
        )

    def test_activate_resumes_after_exact_history_exists(self):
        from ts_harness.workflows.definition_paths import (
            definition_content_revision,
            history_filename,
        )

        draft = self.store.create_draft(self.definition(version="resume-1"), actor_id="a")
        history_dir = self.root / "history" / "custom_flow"
        history_dir.mkdir(parents=True)
        revision = definition_content_revision(draft["definition"])
        (history_dir / history_filename("resume-1", revision)).write_text(
            json.dumps(draft["definition"]),
            encoding="utf-8",
        )

        summary = self.store.activate(
            draft["id"],
            expected_revision=draft["revision"],
            actor_id="a",
        )

        self.assertEqual(summary["definition_version"], "resume-1")
        self.assertEqual(self.store.read_manifest()["generation"], 1)
        self.assertFalse((self.root / "drafts" / f"{draft['id']}.json").exists())

    def test_history_snapshot_is_immutable_on_idempotent_activation(self):
        first = self.store.create_draft(
            self.definition(version="1.0.0"),
            actor_id="a",
            editor_metadata={"layout": "first"},
        )
        self.store.activate(
            first["id"],
            expected_revision=first["revision"],
            actor_id="a",
        )
        history_files = list((self.root / "history" / "custom_flow").glob("*.json"))
        self.assertEqual(len(history_files), 1)
        original = history_files[0].read_bytes()
        mtime = history_files[0].stat().st_mtime_ns

        duplicate = self.store.create_draft(
            self.definition(version="1.0.0"),
            actor_id="a",
            editor_metadata={"layout": "second"},
        )
        time.sleep(0.02)
        self.store.activate(
            duplicate["id"],
            expected_revision=duplicate["revision"],
            actor_id="a",
        )

        self.assertEqual(history_files[0].read_bytes(), original)
        self.assertEqual(history_files[0].stat().st_mtime_ns, mtime)

    def test_colon_version_history_uses_shared_safe_filename(self):
        from ts_harness.workflows.definition_paths import (
            definition_content_revision,
            history_dir,
            history_filename,
        )

        draft = self.store.create_draft(
            self.definition(version="release:2026.08"),
            actor_id="alice",
        )

        self.store.activate(
            draft["id"],
            expected_revision=draft["revision"],
            actor_id="alice",
        )

        revision = definition_content_revision(draft["definition"])
        path = history_dir(self.root, "custom_flow") / history_filename(
            "release:2026.08",
            revision,
        )
        self.assertTrue(path.exists())

    def test_history_filename_is_injective_and_prefix_safe(self):
        from ts_harness.workflows.definition_paths import history_filename

        colon = history_filename("release:2026", "rev")
        underscore = history_filename("release_2026", "rev")
        beta = history_filename("v1-beta", "rev")
        v1_prefix = history_filename("v1", "rev")[: -len("rev.json")]

        self.assertNotEqual(colon, underscore)
        self.assertFalse(beta.startswith(v1_prefix))

    def test_legacy_history_prefix_does_not_collide_across_versions(self):
        from ts_harness.workflows.definition_paths import (
            list_history_paths_for_version,
            safe_definition_version,
        )

        history = self.root / "history" / "custom_flow"
        history.mkdir(parents=True)
        beta_path = history / f"{safe_definition_version('v1-beta')}-rev-beta.json"
        beta_path.write_text(
            json.dumps(self.definition(version="v1-beta")),
            encoding="utf-8",
        )
        v1_path = history / f"{safe_definition_version('v1')}-rev-v1.json"
        v1_path.write_text(
            json.dumps(self.definition(version="v1")),
            encoding="utf-8",
        )

        self.assertEqual(
            list_history_paths_for_version(self.root, "custom_flow", "v1"),
            [v1_path],
        )
        self.assertEqual(
            self.store._history_version_revisions("custom_flow", "v1"),
            {"rev-v1"},
        )

    def test_get_active_returns_none_when_history_missing_and_active_mismatches(self):
        draft = self.store.create_draft(self.definition(version="manifest-1"), actor_id="a")
        self.store.activate(
            draft["id"],
            expected_revision=draft["revision"],
            actor_id="a",
        )
        active_path = self.root / "active" / "custom_flow.json"
        active_path.write_text(
            json.dumps(self.definition(version="stale-active")),
            encoding="utf-8",
        )
        for path in (self.root / "history" / "custom_flow").glob("*.json"):
            path.unlink()

        self.assertIsNone(self.store.get_active("custom_flow"))

    def test_store_lock_recovers_stale_exclusive_lockfile(self):
        from ts_harness.workflows import versioned_definition_store

        lock_path = self.root / ".store.lock.exclusive"
        lock_path.write_text("stale", encoding="utf-8")
        old = time.time() - versioned_definition_store._LOCKFILE_STALE_SECONDS - 1
        os.utime(lock_path, (old, old))

        with mock.patch.object(versioned_definition_store, "fcntl", None):
            with self.store._store_lock():
                self.assertTrue(lock_path.exists())

        self.assertFalse(lock_path.exists())

    def test_get_active_loads_manifest_history_when_active_file_missing(self):
        draft = self.store.create_draft(self.definition(version="manifest-1"), actor_id="a")
        self.store.activate(
            draft["id"],
            expected_revision=draft["revision"],
            actor_id="a",
        )
        (self.root / "active" / "custom_flow.json").unlink()

        active = self.store.get_active("custom_flow")

        self.assertIsNotNone(active)
        self.assertEqual(active["definition_version"], "manifest-1")

    def test_activate_updates_manifest_before_active_file_and_keeps_draft_on_failure(self):
        from ts_harness.workflows import versioned_definition_store

        draft = self.store.create_draft(self.definition(version="manifest-first"), actor_id="a")
        original_write = versioned_definition_store._atomic_write_json

        def fail_active_after_manifest(path, payload):
            if path == self.root / "active" / "custom_flow.json":
                raise OSError("active write failed")
            original_write(path, payload)

        with mock.patch.object(
            versioned_definition_store,
            "_atomic_write_json",
            side_effect=fail_active_after_manifest,
        ):
            with self.assertRaises(OSError):
                self.store.activate(
                    draft["id"],
                    expected_revision=draft["revision"],
                    actor_id="a",
                )

        manifest = self.store.read_manifest()
        self.assertEqual(manifest["generation"], 1)
        self.assertIn("custom_flow", manifest["workflows"])
        self.assertTrue((self.root / "drafts" / f"{draft['id']}.json").exists())

    def test_store_lock_uses_exclusive_lockfile_when_fcntl_unavailable(self):
        from ts_harness.workflows import versioned_definition_store

        lock_path = self.root / ".store.lock.exclusive"
        with mock.patch.object(versioned_definition_store, "fcntl", None):
            with self.store._store_lock():
                self.assertTrue(lock_path.exists())

        self.assertFalse(lock_path.exists())

    def test_concurrent_activation_preserves_manifest_entries(self):
        drafts = [
            self.store.create_draft(
                self.definition(
                    workflow_type=f"custom_flow_{index}",
                    version="1.0.0",
                ),
                actor_id="alice",
            )
            for index in range(8)
        ]

        context = multiprocessing.get_context("fork")
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=len(drafts),
            mp_context=context,
        ) as executor:
            list(
                executor.map(
                    _activate_draft_with_manifest_delay,
                    [
                        (str(self.root), draft["id"], draft["revision"])
                        for draft in drafts
                    ],
                )
            )

        manifest = self.store.read_manifest()
        self.assertEqual(manifest["generation"], len(drafts))
        self.assertEqual(
            set(manifest["workflows"]),
            {f"custom_flow_{index}" for index in range(8)},
        )


if __name__ == "__main__":
    unittest.main()
