"""Skill repository: authoring on a local directory, or on object storage plus the Postgres index."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.memory_api_support import LiveMemoryApi
from tests.pg_support import PgTestCase, requires_pg
from tests.runtime import test_skill_authoring as authoring_suite
from tests.runtime import test_skill_authoring_folders as folders_suite
from tests.skill_index_support import FakeSkillIndex, IndexedRepoHarness

from ts_harness.memory.client import MemoryClient
from ts_harness.skills import repository as repository_mod
from ts_harness.skills.authoring import SkillAuthoringError
from ts_harness.skills.catalog import SkillCatalog
from ts_harness.skills.repository import (
    IndexedSkillRepository,
    LocalSkillRepository,
    content_sha,
    skill_repository,
    sync_skill_mirror,
)
from ts_harness.storage.objects import ChecksumMismatch, LocalObjectStore

DRAFT_MD = authoring_suite.DRAFT_MD
REVISED_MD = authoring_suite.REVISED_MD


class _IndexedSuite:
    """Mix in ahead of an authoring test case to send every call through IndexedSkillRepository."""

    indexed_skip: frozenset[str] = frozenset()
    suite_module: object = None

    def setUp(self) -> None:
        if self._testMethodName in self.indexed_skip:
            self.skipTest("seeds files straight into the managed directory, which the index does not know about")
        self.harness = IndexedRepoHarness(self.suite_module)
        self.harness.start()
        self.addCleanup(self.harness.stop)
        super().setUp()


class IndexedSkillAuthoringTests(_IndexedSuite, authoring_suite.SkillAuthoringTests):
    suite_module = authoring_suite
    indexed_skip = frozenset({
        "test_propose_does_not_replace_unattributed_draft",
        "test_propose_malformed_existing_draft_frontmatter_blocks_replace",
        "test_approve_revision_of_still_exfiltrating_draft_fails_closed",
        "test_approve_revision_replaces_directory_form_live_skill",
        "test_propose_revision_of_blocked_live_skill_writes_clean_draft",
    })


class IndexedFolderDraftTests(_IndexedSuite, folders_suite.FolderDraftTests):
    suite_module = folders_suite
    indexed_skip = frozenset({"test_builtin_revision_allowed_when_roots_are_the_same"})


def _write_skill(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class IndexedRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.index = FakeSkillIndex()
        self.objects = LocalObjectStore(self.base / "objects")

    def replica(self, name: str = "a", **kwargs) -> IndexedSkillRepository:
        return IndexedSkillRepository(
            self.base / name / "skills" / "current", client=self.index, objects=self.objects, sync_interval=0, **kwargs
        )

    def object_keys(self, prefix: str = "skills/") -> list[str]:
        return [info.key for info in self.objects.list(prefix)]

    def test_drafts_and_versions_live_in_object_storage(self):
        repo = self.replica()
        repo.propose_markdown(agent_id="ops-agent", tenant_id="default", markdown=DRAFT_MD)
        draft = repo.rows[0]
        self.assertEqual(draft["status"], "draft")
        self.assertTrue(draft["object_prefix"].startswith("skills/drafts/team-runbook-"))
        self.assertEqual(self.object_keys(), [draft["object_prefix"] + "SKILL.md"])

        result = repo.approve(skill_id="team-runbook", reviewer="admin-1")
        live = next(r for r in repo.rows if r["status"] == "active")
        self.assertEqual(live["object_prefix"], f"skills/team-runbook/1.0.0-{live['content_sha'][:12]}/")
        self.assertEqual(live["content_sha"], content_sha(live["files"]))
        self.assertEqual(self.object_keys(), [live["object_prefix"] + "SKILL.md"])
        self.assertTrue(Path(result["path"]).is_file())
        self.assertIn("approval_status: approved", (repo.root / "ops" / "team-runbook.md").read_text())

    def test_root_is_a_symlink_swapped_on_each_rebuild(self):
        repo = self.replica()
        repo.sync(force=True)
        self.assertTrue(repo.root.is_symlink())
        first = repo.root.resolve()
        repo.propose_markdown(agent_id="ops-agent", tenant_id="default", markdown=DRAFT_MD)
        self.assertNotEqual(repo.root.resolve(), first)
        self.assertTrue((repo.root / "drafts" / "team-runbook.md").is_file())
        builds = [p.name for p in (repo.root.parent / ".current-builds").iterdir()]
        self.assertLessEqual(len(builds), 2)

    def test_second_replica_sees_the_approval_after_the_generation_bump(self):
        a, b = self.replica("a"), self.replica("b")
        catalog = SkillCatalog(b.root)
        b.sync(force=True)
        a.propose_markdown(agent_id="ops-agent", tenant_id="default", markdown=DRAFT_MD)
        a.approve(skill_id="team-runbook", reviewer="admin-1")
        self.assertTrue(b.sync())
        catalog.load()
        live = [m for m in catalog.index.values() if m.id == "team-runbook"]
        self.assertEqual([m.approval_status for m in live], ["approved"])
        self.assertFalse(b.sync())

        a.propose_markdown(agent_id="ops-agent", tenant_id="default", markdown=REVISED_MD, revision_of="team-runbook")
        a.approve(skill_id="team-runbook", reviewer="admin-1")
        self.assertTrue(b.sync())
        self.assertIn("Updated checklist.", (b.root / "ops" / "team-runbook.md").read_text())
        self.assertEqual(len(self.object_keys("skills/team-runbook/")), 2)

    def test_failed_approval_leaves_the_old_version_live_and_the_draft_intact(self):
        repo = self.replica()
        repo.propose_markdown(agent_id="ops-agent", tenant_id="default", markdown=DRAFT_MD)
        repo.approve(skill_id="team-runbook", reviewer="admin-1")
        repo.propose_markdown(agent_id="ops-agent", tenant_id="default", markdown=REVISED_MD, revision_of="team-runbook")
        draft_prefix = repo._drafts()["team-runbook"]["object_prefix"]
        live_before = (repo.root / "ops" / "team-runbook.md").read_text()

        self.index.fail_next["approve"] = RuntimeError("memory API down")
        with self.assertRaisesRegex(RuntimeError, "memory API down"):
            repo.approve(skill_id="team-runbook", reviewer="admin-1")

        self.assertEqual((repo.root / "ops" / "team-runbook.md").read_text(), live_before)
        self.assertIn("Updated checklist.", (repo.root / "drafts" / "team-runbook.md").read_text())
        self.assertEqual([r["status"] for r in self.index.rows if r["skill_id"] == "team-runbook"], ["approved", "active", "draft"])
        self.assertIn(draft_prefix + "SKILL.md", self.object_keys())
        # The copied version objects are orphaned, never live; a retry reuses them.
        repo.approve(skill_id="team-runbook", reviewer="admin-1")
        self.assertIn("Updated checklist.", (repo.root / "ops" / "team-runbook.md").read_text())
        self.assertNotIn(draft_prefix + "SKILL.md", self.object_keys())

    def test_failed_draft_upload_keeps_the_previous_draft(self):
        repo = self.replica()
        repo.propose_markdown(agent_id="ops-agent", tenant_id="default", markdown=DRAFT_MD)
        before = list(self.object_keys())
        self.index.fail_next["upsert"] = RuntimeError("memory API down")
        with self.assertRaises(RuntimeError):
            repo.propose_markdown(
                agent_id="ops-agent", tenant_id="default", markdown=DRAFT_MD.replace("Follow the checklist.", "Changed.")
            )
        self.assertEqual(self.object_keys(), before)
        self.assertIn("Follow the checklist.", (repo.root / "drafts" / "team-runbook.md").read_text())

    def test_replacing_a_draft_deletes_the_old_prefix(self):
        repo = self.replica()
        repo.propose_markdown(agent_id="ops-agent", tenant_id="default", markdown=DRAFT_MD)
        first = repo.rows[0]["object_prefix"]
        repo.propose_markdown(agent_id="ops-agent", tenant_id="default", markdown=DRAFT_MD.replace("Follow", "Read"))
        self.assertNotEqual(repo.rows[0]["object_prefix"], first)
        self.assertEqual(len(self.object_keys()), 1)

    def test_reject_marks_the_row_and_deletes_the_draft_objects(self):
        repo = self.replica()
        repo.propose_markdown(agent_id="ops-agent", tenant_id="default", markdown=DRAFT_MD)
        repo.reject(skill_id="team-runbook", reviewer="admin-1", reason="no")
        self.assertEqual(self.index.rows[0]["status"], "rejected")
        self.assertEqual(self.object_keys(), [])
        self.assertFalse((repo.root / "drafts" / "team-runbook.md").exists())
        with self.assertRaisesRegex(SkillAuthoringError, "draft not found"):
            repo.reject(skill_id="team-runbook", reviewer="admin-1")

    def test_index_rule_errors_become_authoring_errors_with_codes(self):
        a, b = self.replica("a"), self.replica("b")
        a.propose_markdown(agent_id="ops-agent", tenant_id="default", markdown=DRAFT_MD)
        b.sync(force=True)
        # Replica b's catalog check passes on its own; the index enforces the rule on the push.
        with mock.patch("ts_harness.skills.authoring._check_new_or_revision"):
            a.approve(skill_id="team-runbook", reviewer="admin-1")
            with mock.patch.object(b, "sync"):
                with self.assertRaises(SkillAuthoringError) as caught:
                    b.propose_markdown(agent_id="ops-agent", tenant_id="default", markdown=DRAFT_MD)
        self.assertEqual(caught.exception.code, "skill_learn_use_revision")

    def test_checksum_mismatch_on_download_is_refused(self):
        repo = self.replica()
        repo.propose_markdown(agent_id="ops-agent", tenant_id="default", markdown=DRAFT_MD)
        repo.approve(skill_id="team-runbook", reviewer="admin-1")
        key = next(k for k in self.object_keys() if k.endswith("SKILL.md"))
        (self.base / "objects" / key).write_text("tampered", encoding="utf-8")
        other = self.replica("b")
        with self.assertRaises(ChecksumMismatch):
            other.sync(force=True)
        self.assertFalse(other.root.exists())

    def test_expired_draft_objects_skip_the_draft_but_not_the_sync(self):
        repo = self.replica()
        repo.propose_markdown(agent_id="ops-agent", tenant_id="default", markdown=DRAFT_MD)
        repo.approve(skill_id="team-runbook", reviewer="admin-1")
        repo.propose_markdown(agent_id="ops-agent", tenant_id="default", markdown=REVISED_MD, revision_of="team-runbook")
        self.objects.delete_prefix("skills/drafts/")
        other = self.replica("b")
        with self.assertLogs("ts_harness.skills.repository", "WARNING") as logs:
            self.assertTrue(other.sync(force=True))
        self.assertIn("skipping draft team-runbook", logs.output[0])
        self.assertTrue((other.root / "ops" / "team-runbook.md").is_file())
        self.assertFalse((other.root / "drafts").exists())

        self.objects.delete_prefix("skills/team-runbook/")
        with self.assertRaises(Exception):
            self.replica("c").sync(force=True)

    def test_rebuild_downloads_only_what_changed(self):
        repo = self.replica()
        repo.propose_markdown(agent_id="ops-agent", tenant_id="default", markdown=DRAFT_MD)
        repo.approve(skill_id="team-runbook", reviewer="admin-1")
        repo.invalidate()
        with mock.patch.object(self.objects, "get", wraps=self.objects.get) as get:
            repo.sync(force=True)
        get.assert_not_called()

    def test_poll_skips_the_listing_when_the_generation_is_unchanged(self):
        repo = self.replica()
        repo.sync(force=True)
        with mock.patch.object(self.index, "list_skill_index", wraps=self.index.list_skill_index) as listing:
            self.assertFalse(repo.sync(force=True))
        listing.assert_not_called()

    def test_lock_timeout(self):
        repo = self.replica(lock_wait=0.05)
        self.index.locks[("skills", "authoring")] = "someone-else"
        with self.assertRaises(TimeoutError):
            repo.propose_markdown(agent_id="ops-agent", tenant_id="default", markdown=DRAFT_MD)

    def test_folder_draft_round_trip(self):
        repo = self.replica()
        files = [
            {"path": "SKILL.md", "content": folders_suite.SKILL_MD},
            {"path": "stages/plan.md", "content": "# Plan\n"},
        ]
        result = repo.propose_folder(actor_id="admin-2", tenant_id="default", files=files)
        skill_id = result["skill_id"]
        self.assertEqual(repo.read_draft_file(skill_id, "stages/plan.md")["content"], "# Plan\n")
        approved = repo.approve(skill_id=skill_id, reviewer="admin-2")
        self.assertEqual(approved["layout"], "folder")
        live = next(r for r in repo.rows if r["status"] == "active")
        self.assertEqual(sorted(f["path"] for f in live["files"]), ["SKILL.md", "stages/plan.md"])
        self.assertTrue((Path(approved["path"]).parent / "stages" / "plan.md").is_file())
        self.assertEqual([a["id"] for a in repo.list_active()], [live["id"]])


@requires_pg
class IndexedRepositoryPostgresTests(PgTestCase):
    def setUp(self):
        with self.db.connect() as conn:
            for table in ("skill_versions", "shared_file_namespaces", "shared_file_locks"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def test_two_replicas_through_the_real_routes(self):
        objects = LocalObjectStore(self.base / "objects")
        with LiveMemoryApi(self.db) as api:
            client = MemoryClient(api.url)
            a = IndexedSkillRepository(self.base / "a" / "current", client=client, objects=objects, sync_interval=0)
            b = IndexedSkillRepository(self.base / "b" / "current", client=client, objects=objects, sync_interval=0)
            b.sync(force=True)

            a.propose_markdown(agent_id="ops-agent", tenant_id="default", markdown=DRAFT_MD)
            self.assertTrue(b.sync())
            self.assertEqual([d["id"] for d in b.list_drafts()], ["team-runbook"])

            b.approve(skill_id="team-runbook", reviewer="admin-1")
            self.assertTrue(a.sync())
            self.assertIn("approval_status: approved", (a.root / "ops" / "team-runbook.md").read_text())

            a.propose_markdown(agent_id="ops-agent", tenant_id="default", markdown=REVISED_MD, revision_of="team-runbook")
            with mock.patch("ts_harness.skills.authoring._check_new_or_revision"):
                with self.assertRaises(SkillAuthoringError) as caught:
                    b.propose_markdown(
                        agent_id="ops-agent",
                        tenant_id="default",
                        markdown=REVISED_MD.replace("version: 1.1.0", "version: 1.0.0"),
                        revision_of="team-runbook",
                    )
            self.assertEqual(caught.exception.code, "skill_version_not_bumped")
            a.approve(skill_id="team-runbook", reviewer="admin-1")
            b.sync()
            self.assertIn("Updated checklist.", (b.root / "ops" / "team-runbook.md").read_text())

        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT status, object_prefix FROM skill_versions WHERE skill_id = 'team-runbook' ORDER BY created_at"
            ).fetchall()
        statuses = [r[0] if isinstance(r, tuple) else r["status"] for r in rows]
        self.assertEqual(statuses, ["approved", "superseded", "approved", "active"])
        self.assertEqual(len([k for k in (i.key for i in objects.list("skills/")) if not k.startswith("skills/drafts/")]), 2)
        self.assertEqual(objects.list("skills/drafts/"), [])


class FactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        repository_mod.reset_skill_repositories()
        self.addCleanup(repository_mod.reset_skill_repositories)

    def test_local_backend_wraps_the_managed_directory(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"SKILLS_DIR": str(Path(tmp) / "builtin"), "SKILLS_MANAGED_DIR": str(Path(tmp) / "managed"), "TS_HARNESS_OBJECT_BACKEND": "local"}
        ):
            repo = skill_repository()
            self.assertIsInstance(repo, LocalSkillRepository)
            self.assertEqual(repo.root, Path(tmp) / "managed")
            self.assertEqual([Path(p) for p in repo.read_roots], [Path(tmp) / "builtin", Path(tmp) / "managed"])
            repo.propose_markdown(agent_id="ops-agent", tenant_id="default", markdown=DRAFT_MD)
            self.assertTrue((Path(tmp) / "managed" / "drafts" / "team-runbook.md").is_file())
            self.assertEqual([d["id"] for d in repo.list_drafts()], ["team-runbook"])
            self.assertFalse(sync_skill_mirror())

    def test_object_backend_uses_the_indexed_repository(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"SKILLS_MANAGED_DIR": str(Path(tmp) / "current"), "TS_HARNESS_OBJECT_BACKEND": "s3", "TS_HARNESS_STATE_BACKEND": "postgres"}
        ):
            repo = skill_repository()
            self.assertIsInstance(repo, IndexedSkillRepository)
            self.assertIs(skill_repository(), repo)

    def test_catalog_reload_rebuilds_the_mirror_first(self):
        with mock.patch.object(repository_mod, "sync_skill_mirror", return_value=False) as sync, tempfile.TemporaryDirectory() as tmp:
            SkillCatalog(tmp).reload()
        sync.assert_called_once_with()


class RuntimeWatcherTests(unittest.TestCase):
    def test_skill_changes_reload_the_catalog(self):
        from ts_harness.gateway.server import RuntimeState

        runtime = RuntimeState.__new__(RuntimeState)
        runtime.skill_catalog = mock.Mock()
        with mock.patch.object(repository_mod, "sync_skill_mirror", return_value=False):
            self.assertFalse(runtime.apply_shared_skill_changes())
        runtime.skill_catalog.load.assert_not_called()
        with mock.patch.object(repository_mod, "sync_skill_mirror", return_value=True):
            self.assertTrue(runtime.apply_shared_skill_changes())
        runtime.skill_catalog.load.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
