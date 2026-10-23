"""scripts/ops/migrate-host-state.py: import, verify and export between the volumes and Postgres plus buckets."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tests.memory_api_support import LiveMemoryApi
from tests.object_store_support import FakeS3
from tests.pg_support import PgTestCase, requires_pg
from tests.runtime.test_skill_authoring import DRAFT_MD, REVISED_MD
from tests.runtime.test_skill_authoring_folders import folder_files

from ts_harness.audit.writer import AuditWriter
from ts_harness.memory.client import MemoryClient
from ts_harness.skills.authoring import approve_draft, propose_skill_draft, propose_skill_folder_draft
from ts_harness.skills.repository import IndexedSkillRepository
from ts_harness.storage.objects import S3ObjectStore
from ts_harness.storage.shared_tree import SharedTree

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ops" / "migrate-host-state.py"
WRAPPER = REPO_ROOT / "scripts" / "ops" / "migrate-host-state.sh"


def load_script():
    spec = importlib.util.spec_from_file_location("migrate_host_state_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migrate = load_script()


class Host:
    """The container volumes of one runtime, laid out under a temp directory."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.definitions = base / "workflow-definitions"
        self.cron = base / "config" / "runtime-cron-jobs.json"
        self.skills = base / "skills-managed"
        self.builtin = base / "skills"
        self.state = base / "state"
        self.workspace = base / "workspace"
        for path in (self.definitions, self.skills, self.builtin, self.state, self.workspace, self.cron.parent):
            path.mkdir(parents=True, exist_ok=True)

    def argv(self, mode: str, *extra: str) -> list[str]:
        return [
            f"--{mode}",
            "--object-backend", "s3",
            "--skills-bucket", "skills-b",
            "--workspace-bucket", "ws-b",
            "--definitions-dir", str(self.definitions),
            "--cron-file", str(self.cron),
            "--skills-dir", str(self.skills),
            "--scheduler-state", str(self.state / "scheduler.json"),
            "--delegation-state", str(self.state / "delegation.json"),
            "--audit-log", str(self.state / "audit.jsonl"),
            "--workspace-dir", str(self.workspace),
            *extra,
        ]

    def seed(self) -> list[str]:
        (self.definitions / "active").mkdir()
        (self.definitions / "active" / "wf-a.json").write_text('{"id": "wf-a"}\n')
        (self.definitions / "programs").mkdir()
        (self.definitions / "programs" / "p1.json").write_text('{"id": "p1"}\n')
        (self.definitions / ".shared-tree.json").write_text("{}")
        self.cron.write_text(json.dumps({"jobs": [{"name": "admin-job"}]}))

        roots = [self.builtin, self.skills]
        with mock.patch.dict(os.environ, {"SKILLS_DIR": str(self.builtin), "SKILLS_MANAGED_DIR": str(self.skills)}):
            propose_skill_draft(root=self.skills, agent_id="ops", tenant_id="default", markdown=DRAFT_MD, read_roots=roots)
            approve_draft(root=self.skills, skill_id="team-runbook", reviewer="admin", read_roots=roots)
            propose_skill_folder_draft(
                root=self.skills, actor_id="admin", tenant_id="default", files=folder_files(), read_roots=roots
            )
            approve_draft(root=self.skills, skill_id="launch-kit", reviewer="admin", read_roots=roots)
            propose_skill_draft(
                root=self.skills, agent_id="ops", tenant_id="default", markdown=REVISED_MD,
                revision_of="team-runbook", read_roots=roots,
            )

        (self.state / "scheduler.json").write_text(json.dumps({"standup": {"last_run": 10.0}}))
        (self.state / "delegation.json").write_text(json.dumps({"active_children": 0, "children": {"s1": ["c1"]}}))
        audit = AuditWriter(self.state / "audit.jsonl", shared=False)
        ids = [
            audit.append(tenant_id="t1", action_type="skill_approved", actor_id="admin").event_id,
            audit.append(tenant_id="t2", action_type="cron_edited", actor_id="admin").event_id,
        ]

        (self.workspace / "ops").mkdir()
        (self.workspace / "ops" / "MEMORY.md").write_text("remember this")
        (self.workspace / "ops" / "logo.bin").write_bytes(b"\x89PNG\x00\xff")
        (self.workspace / "ops" / "coding-backend" / "wo-1").mkdir(parents=True)
        (self.workspace / "ops" / "coding-backend" / "wo-1" / "main.py").write_text("checkout")
        (self.workspace / "README.md").write_text("root file")
        return ids


class ArgumentTests(unittest.TestCase):
    def test_a_mode_is_required_and_sections_are_checked(self):
        with self.assertRaises(SystemExit):
            migrate.parse_args([])
        with self.assertRaises(SystemExit):
            migrate.parse_args(["--import", "--verify"])
        with self.assertRaises(SystemExit):
            migrate.parse_args(["--verify", "--only", "skills,bogus"])
        args = migrate.parse_args(["--export", "--only", "kv, cron"])
        self.assertEqual((args.export, args.only), (True, ["kv", "cron"]))

    def test_local_object_backend_skips_skills_and_workspace(self):
        args = migrate.parse_args(["--import", "--only", "skills,workspace", "--object-backend", "local"])
        report = migrate.run(args, client=mock.Mock(side_effect=AssertionError("no calls")))
        self.assertTrue(report.ok)
        self.assertEqual(report.as_dict()["skills"]["skipped_object_backend_local"], 1)
        self.assertEqual(report.as_dict()["workspace"]["skipped_object_backend_local"], 1)

    def test_bucket_is_required_for_cloud_backends(self):
        args = migrate.parse_args(["--verify", "--only", "workspace", "--object-backend", "s3", "--workspace-bucket", ""])
        report = migrate.run(args, client=mock.Mock())
        self.assertFalse(report.ok)
        self.assertIn("--workspace-bucket is required", report.as_dict()["workspace"]["problems"][0])

    def test_main_prints_json_and_exits_1_on_problems(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "audit.jsonl"
            audit.write_text("not json\n")
            client = mock.Mock()
            client.list_audit_events.return_value = []
            out = io.StringIO()
            with mock.patch("ts_harness.memory.client.MemoryClient", return_value=client), redirect_stdout(out):
                code = migrate.main(["--verify", "--only", "audit", "--audit-log", str(audit)])
        self.assertEqual(code, 1)
        printed = json.loads(out.getvalue())
        self.assertFalse(printed["ok"])
        self.assertIn("unreadable line 1", printed["sections"]["audit"]["problems"][0])


class WrapperContractTests(unittest.TestCase):
    def test_wrapper_attaches_to_the_runtime_and_cleans_up(self):
        text = WRAPPER.read_text()
        self.assertIn('--network "container:${CID}"', text)
        self.assertIn('--volumes-from "${CID}:rw"', text)
        self.assertIn("trap 'rm -f \"$ENV_FILE\"' EXIT", text)
        self.assertIn('chmod 600 "$ENV_FILE"', text)
        self.assertNotIn("exec docker run", text)
        self.assertIn('SCRIPT_PATH="/app/scripts/ops/migrate-host-state.py"', text)
        self.assertTrue(os.access(WRAPPER, os.X_OK))
        self.assertTrue(os.access(SCRIPT, os.X_OK))


@requires_pg
class MigrateHostStatePostgresTests(PgTestCase):
    def setUp(self):
        with self.db.connect() as conn:
            for table in (
                "shared_files", "shared_file_namespaces", "shared_file_locks", "runtime_kv",
                "runtime_audit_events", "skill_versions",
            ):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.s3 = FakeS3()
        self.stores = {"skills": S3ObjectStore("skills-b", self.s3), "workspaces": S3ObjectStore("ws-b", self.s3)}

    def run_mode(self, client, host: Host, mode: str, *extra: str):
        return migrate.run(migrate.parse_args(host.argv(mode, *extra)), client=client, stores=self.stores)

    def test_import_verify_export_round_trip(self):
        host = Host(self.base / "host")
        event_ids = host.seed()
        with LiveMemoryApi(self.db) as api:
            client = MemoryClient(api.url)

            imported = self.run_mode(client, host, "import")
            self.assertTrue(imported.ok, imported.as_dict())
            sections = imported.as_dict()
            self.assertEqual(sections["definitions"]["imported"], 2)
            self.assertEqual(sections["cron"]["imported"], 1)
            self.assertEqual((sections["skills"]["imported_active"], sections["skills"]["imported_draft"]), (2, 1))
            self.assertEqual(sections["kv"]["imported"], 2)
            self.assertEqual(sections["audit"]["inserted"], 2)
            self.assertEqual(sections["workspace"]["uploaded"], 3)

            verified = self.run_mode(client, host, "verify")
            self.assertTrue(verified.ok, verified.as_dict())
            self.assertEqual(verified.as_dict()["skills"]["matched"], 3)
            self.assertEqual(verified.as_dict()["audit"]["matched"], 2)

            again = self.run_mode(client, host, "import")
            self.assertTrue(again.ok, again.as_dict())
            again_sections = again.as_dict()
            self.assertEqual(again_sections["definitions"]["unchanged"], 2)
            self.assertEqual((again_sections["skills"]["unchanged_active"], again_sections["skills"]["unchanged_draft"]), (2, 1))
            self.assertEqual(again_sections["audit"]["inserted"], 0)
            self.assertEqual(again_sections["workspace"]["unchanged"], 3)

            # The runtime's own readers see the imported state.
            tree = SharedTree("definitions", self.base / "mirror" / "definitions", client, sync_interval=0)
            tree.sync(force=True)
            self.assertEqual((tree.root / "active" / "wf-a.json").read_text(), '{"id": "wf-a"}\n')
            pulled = {f["path"] for f in client.pull_shared_files("definitions", since=0)["files"]}
            self.assertEqual(pulled, {"active/wf-a.json", "programs/p1.json"})
            repo = IndexedSkillRepository(
                self.base / "mirror" / "skills" / "current", client=client, objects=self.stores["skills"], sync_interval=0
            )
            repo.sync(force=True)
            self.assertIn("Follow the checklist.", (repo.root / "ops" / "team-runbook.md").read_text())
            self.assertEqual((repo.root / "marketing" / "launch-kit" / "stages" / "plan.md").read_text(), "# Plan\n")
            self.assertIn("Updated checklist.", (repo.root / "drafts" / "team-runbook.md").read_text())
            self.assertEqual(client.kv_get("scheduler", "state")["value"], {"standup": {"last_run": 10.0}})
            self.assertEqual({e["event_id"] for e in client.list_audit_events()}, set(event_ids))
            ws_keys = sorted(k for b, k in self.s3.objects if b == "ws-b")
            self.assertEqual(ws_keys, ["workspaces/README.md", "workspaces/ops/MEMORY.md", "workspaces/ops/logo.bin"])

            # Rollback: export into empty volumes, with stale files that must go.
            rollback = Host(self.base / "rollback")
            (rollback.definitions / "active").mkdir()
            (rollback.definitions / "active" / "stale.json").write_text("{}")
            (rollback.skills / "drafts").mkdir()
            (rollback.skills / "drafts" / "old-draft.md").write_text("stale draft")
            exported = self.run_mode(client, rollback, "export")
            self.assertTrue(exported.ok, exported.as_dict())
            self.assertFalse((rollback.definitions / "active" / "stale.json").exists())
            self.assertFalse((rollback.skills / "drafts" / "old-draft.md").exists())
            self.assertEqual((rollback.definitions / "programs" / "p1.json").read_text(), '{"id": "p1"}\n')
            self.assertEqual(json.loads(rollback.cron.read_text()), {"jobs": [{"name": "admin-job"}]})
            self.assertEqual(json.loads((rollback.state / "delegation.json").read_text())["children"], {"s1": ["c1"]})
            self.assertEqual((rollback.workspace / "ops" / "logo.bin").read_bytes(), b"\x89PNG\x00\xff")
            self.assertEqual(
                (rollback.skills / "ops" / "team-runbook.md").read_bytes(), (host.skills / "ops" / "team-runbook.md").read_bytes()
            )
            self.assertTrue((rollback.skills / "marketing" / "launch-kit" / "schemas" / "output" / "brief.json").is_file())
            back = self.run_mode(client, rollback, "verify", "--only", "definitions,cron,skills,kv,workspace")
            self.assertTrue(back.ok, back.as_dict())

    def test_import_reports_conflicts_and_force_overwrites(self):
        host = Host(self.base / "host")
        host.seed()
        with LiveMemoryApi(self.db) as api:
            client = MemoryClient(api.url)
            self.assertTrue(self.run_mode(client, host, "import").ok)

            (host.definitions / "active" / "wf-a.json").write_text('{"id": "wf-a", "v": 2}\n')
            (host.state / "scheduler.json").write_text(json.dumps({"standup": {"last_run": 99.0}}))
            (host.workspace / "ops" / "MEMORY.md").write_text("edited on the host")

            verified = self.run_mode(client, host, "verify")
            self.assertFalse(verified.ok)
            problems = verified.as_dict()
            self.assertIn("hash differs: active/wf-a.json", problems["definitions"]["problems"])
            self.assertIn("scheduler/state differs", problems["kv"]["problems"][0])
            self.assertIn("hash differs: ops/MEMORY.md", problems["workspace"]["problems"])

            refused = self.run_mode(client, host, "import", "--only", "definitions,kv,workspace")
            self.assertFalse(refused.ok)
            self.assertIn("use --force", refused.as_dict()["definitions"]["problems"][0])
            self.assertEqual(client.kv_get("scheduler", "state")["value"], {"standup": {"last_run": 10.0}})

            forced = self.run_mode(client, host, "import", "--only", "definitions,kv,workspace", "--force")
            self.assertTrue(forced.ok, forced.as_dict())
            self.assertTrue(self.run_mode(client, host, "verify").ok)

    def test_draft_whose_live_skill_is_missing_is_reported(self):
        host = Host(self.base / "host")
        host.seed()
        (host.skills / "ops" / "team-runbook.md").unlink()
        with LiveMemoryApi(self.db) as api:
            client = MemoryClient(api.url)
            report = self.run_mode(client, host, "import", "--only", "skills")
        self.assertFalse(report.ok)
        self.assertEqual(report.as_dict()["skills"]["problems"], ["draft team-runbook: skill_revision_not_live"])
        self.assertEqual(report.as_dict()["skills"]["imported_active"], 1)


if __name__ == "__main__":
    unittest.main()
