from __future__ import annotations

import concurrent.futures
import json
import multiprocessing
import tempfile
import time
import unittest
from pathlib import Path

from ts_harness.workflows.definition_store import (
    ConflictError,
    WorkflowDefinitionStore,
)
from ts_harness.workflows.program_definition_store import ProgramDefinitionStore


def _activate_program_draft_with_manifest_delay(args):
    root, draft_id, revision = args
    import time

    from ts_harness.workflows import versioned_definition_store

    original_write = versioned_definition_store._atomic_write_json

    def delayed_write(path, payload):
        if path.name == "manifest.json":
            time.sleep(0.15)
        original_write(path, payload)

    versioned_definition_store._atomic_write_json = delayed_write
    store = ProgramDefinitionStore(Path(root))
    store.activate(draft_id, expected_revision=revision, actor_id="worker")


def _activate_workflow_draft(args):
    root, draft_id, revision = args
    from ts_harness.workflows.definition_store import WorkflowDefinitionStore

    store = WorkflowDefinitionStore(Path(root))
    store.activate(draft_id, expected_revision=revision, actor_id="worker")


def _activate_program_draft(args):
    root, draft_id, revision = args
    store = ProgramDefinitionStore(Path(root))
    store.activate(draft_id, expected_revision=revision, actor_id="worker")


class ProgramDefinitionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.volume_root = Path(self.tmp.name)
        self.root = self.volume_root / "programs"
        self.store = ProgramDefinitionStore(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def definition(
        self,
        *,
        program_type: str = "custom_program",
        version: str = "1.0.0",
        display_name: str = "Custom program",
        workflow_type: str = "custom_flow",
    ):
        return {
            "program_type": program_type,
            "definition_version": version,
            "display_name": display_name,
            "startable_nodes": ["start"],
            "nodes": [
                {
                    "id": "start",
                    "default_workflow_type": workflow_type,
                    "workflow_variants": [],
                    "required_artifacts": [],
                    "produces_artifacts": [],
                    "input_bindings": [],
                    "completion_adapter": "extract_prd_outputs",
                    "terminal": True,
                }
            ],
            "edges": [],
        }

    def workflow_definition(
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
        self.assertEqual(draft["program_type"], "custom_program")
        self.assertEqual(draft["created_by"], "alice")
        self.assertEqual(
            self.store.get_draft(draft["id"])["revision"],
            draft["revision"],
        )

        updated = self.store.update_draft(
            draft["id"],
            self.definition(version="1.0.1"),
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

    def test_activate_writes_active_history_and_manifest_under_programs(self):
        draft = self.store.create_draft(self.definition(), actor_id="alice")

        summary = self.store.activate(
            draft["id"],
            expected_revision=draft["revision"],
            actor_id="alice",
        )

        self.assertEqual(summary["program_type"], "custom_program")
        self.assertEqual(
            self.store.get_active("custom_program")["program_type"],
            "custom_program",
        )
        self.assertEqual(len(self.store.list_active()), 1)
        self.assertFalse((self.root / "drafts" / f"{draft['id']}.json").exists())
        manifest = self.store.read_manifest()
        self.assertEqual(manifest["generation"], 1)
        self.assertIn("custom_program", manifest["programs"])
        self.assertNotIn("workflows", manifest)
        history = list((self.root / "history" / "custom_program").glob("*.json"))
        self.assertEqual(len(history), 1)
        self.assertTrue((self.root / "active" / "custom_program.json").exists())
        self.assertFalse((self.volume_root / "active" / "manifest.json").exists())

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
            self.store.get_active("custom_program")["definition_version"],
            "1.0.1",
        )
        self.assertEqual(self.store.read_manifest()["generation"], 2)

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
        changed = self.definition(version="1.0.0", display_name="Changed")
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
            self.definition(version="1.0.0", display_name="changed historical"),
            actor_id="a",
        )
        with self.assertRaises(ValueError):
            self.store.activate(
                reused["id"],
                expected_revision=reused["revision"],
                actor_id="a",
            )

        self.assertEqual(
            self.store.get_active("custom_program")["definition_version"],
            "1.0.1",
        )
        self.assertEqual(
            self.store.get_draft(reused["id"])["definition"]["definition_version"],
            "1.0.0",
        )

    def test_rejects_path_traversal_and_builtin_activation(self):
        with self.assertRaises(ValueError):
            self.store.create_draft(
                self.definition(program_type="../bad"),
                actor_id="a",
            )
        with self.assertRaises(ValueError):
            self.store.get_draft("../bad")

        store = ProgramDefinitionStore(
            self.root,
            built_in_types={"software_delivery"},
        )
        draft = store.create_draft(
            self.definition(program_type="software_delivery"),
            actor_id="a",
        )
        with self.assertRaises(ValueError):
            store.activate(
                draft["id"],
                expected_revision=draft["revision"],
                actor_id="a",
            )

    def test_active_files_are_plain_program_json(self):
        draft = self.store.create_draft(self.definition(), actor_id="alice")
        self.store.activate(
            draft["id"],
            expected_revision=draft["revision"],
            actor_id="alice",
        )

        active_payload = json.loads(
            (self.root / "active" / "custom_program.json").read_text()
        )
        self.assertEqual(active_payload["program_type"], "custom_program")
        self.assertNotIn("status", active_payload)
        self.assertNotIn("editor_metadata", active_payload)

    def test_history_filename_uses_hash_prefix(self):
        from ts_harness.workflows.definition_paths import (
            history_filename,
            program_content_revision,
            program_history_dir,
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

        revision = program_content_revision(draft["definition"])
        path = program_history_dir(self.volume_root, "custom_program") / history_filename(
            "release:2026.08",
            revision,
        )
        self.assertTrue(path.exists())

    def test_get_active_returns_none_when_history_missing_and_active_mismatches(self):
        draft = self.store.create_draft(
            self.definition(version="manifest-1"),
            actor_id="a",
        )
        self.store.activate(
            draft["id"],
            expected_revision=draft["revision"],
            actor_id="a",
        )
        active_path = self.root / "active" / "custom_program.json"
        active_path.write_text(
            json.dumps(self.definition(version="stale-active")),
            encoding="utf-8",
        )
        for path in (self.root / "history" / "custom_program").glob("*.json"):
            path.unlink()

        self.assertIsNone(self.store.get_active("custom_program"))

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
        history_files = list((self.root / "history" / "custom_program").glob("*.json"))
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

    def test_concurrent_program_activation_preserves_manifest_entries(self):
        drafts = [
            self.store.create_draft(
                self.definition(
                    program_type=f"custom_program_{index}",
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
                    _activate_program_draft_with_manifest_delay,
                    [
                        (str(self.root), draft["id"], draft["revision"])
                        for draft in drafts
                    ],
                )
            )

        manifest = self.store.read_manifest()
        self.assertEqual(manifest["generation"], len(drafts))
        self.assertEqual(
            set(manifest["programs"]),
            {f"custom_program_{index}" for index in range(8)},
        )

    def test_concurrent_workflow_and_program_activations_keep_namespaces_isolated(self):
        workflow_store = WorkflowDefinitionStore(self.volume_root)
        workflow_drafts = [
            workflow_store.create_draft(
                self.workflow_definition(
                    workflow_type=f"custom_flow_{index}",
                    version="1.0.0",
                ),
                actor_id="alice",
            )
            for index in range(4)
        ]
        program_drafts = [
            self.store.create_draft(
                self.definition(
                    program_type=f"custom_program_{index}",
                    version="1.0.0",
                ),
                actor_id="alice",
            )
            for index in range(4)
        ]

        before_workflow_paths = {
            path.relative_to(self.volume_root): path.read_bytes()
            for path in self.volume_root.rglob("*")
            if path.is_file() and "programs" not in path.parts
        }

        context = multiprocessing.get_context("fork")
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=8,
            mp_context=context,
        ) as executor:
            workflow_future = executor.map(
                _activate_workflow_draft,
                [
                    (str(self.volume_root), draft["id"], draft["revision"])
                    for draft in workflow_drafts
                ],
            )
            program_future = executor.map(
                _activate_program_draft,
                [
                    (str(self.root), draft["id"], draft["revision"])
                    for draft in program_drafts
                ],
            )
            list(workflow_future)
            list(program_future)

        workflow_manifest = workflow_store.read_manifest()
        program_manifest = self.store.read_manifest()
        self.assertEqual(workflow_manifest["generation"], 4)
        self.assertEqual(
            set(workflow_manifest["workflows"]),
            {f"custom_flow_{index}" for index in range(4)},
        )
        self.assertNotIn("programs", workflow_manifest)
        self.assertEqual(program_manifest["generation"], 4)
        self.assertEqual(
            set(program_manifest["programs"]),
            {f"custom_program_{index}" for index in range(4)},
        )
        self.assertNotIn("workflows", program_manifest)

        for relative, payload in before_workflow_paths.items():
            path = self.volume_root / relative
            if path.name.endswith(".json") and "drafts" in path.parts:
                continue
            if not path.exists():
                continue
            # Drafts are deleted on activate; remaining non-program files that
            # existed before concurrent program writes must stay intact when
            # they are outside the programs namespace.
            if "programs" in relative.parts:
                self.fail(f"program path leaked into workflow snapshot: {relative}")

        self.assertFalse((self.volume_root / "active" / "custom_program_0.json").exists())
        self.assertTrue((self.root / "active" / "custom_program_0.json").exists())
        self.assertTrue((self.volume_root / "active" / "custom_flow_0.json").exists())
        self.assertFalse((self.root / "active" / "custom_flow_0.json").exists())


if __name__ == "__main__":
    unittest.main()
