"""Builtin plus managed skill roots."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ts_harness.skills.catalog import SkillCatalog
from ts_harness.skills.roots import catalog_roots, is_builtin_path, managed_root

SKILL = "---\nid: {id}\nversion: 1.0.0\ncategory: ops\ndescription: d\n---\nBody {where}.\n"


class SkillRootTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.builtin = Path(tmp.name) / "builtin"
        self.managed = Path(tmp.name) / "managed"
        for root in (self.builtin, self.managed):
            (root / "ops").mkdir(parents=True)

    def write(self, root: Path, rel: str, skill_id: str, where: str) -> Path:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SKILL.format(id=skill_id, where=where), encoding="utf-8")
        return path

    def test_catalog_loads_both_roots_with_origin(self):
        self.write(self.builtin, "ops/alpha.md", "alpha", "builtin")
        self.write(self.managed, "ops/beta/SKILL.md", "beta", "managed")
        catalog = SkillCatalog(roots=[self.builtin, self.managed])
        catalog.load()
        self.assertEqual(catalog.index["alpha@1.0.0"].origin, "builtin")
        self.assertEqual(catalog.index["beta@1.0.0"].origin, "managed")

    def test_cross_root_collision_keeps_builtin_and_records_diagnostic(self):
        builtin_path = self.write(self.builtin, "ops/alpha.md", "alpha", "builtin")
        self.write(self.managed, "ops/alpha.md", "alpha", "managed")
        catalog = SkillCatalog(roots=[self.builtin, self.managed])
        catalog.load()
        self.assertEqual(catalog.index["alpha@1.0.0"].source_path, str(builtin_path))
        self.assertEqual(len(catalog.collisions), 1)
        self.assertIn("alpha@1.0.0", catalog.collisions[0])

    def test_same_root_duplicate_still_raises(self):
        self.write(self.managed, "ops/alpha.md", "alpha", "one")
        self.write(self.managed, "sales/alpha.md", "alpha", "two")
        catalog = SkillCatalog(roots=[self.builtin, self.managed])
        with self.assertRaisesRegex(ValueError, "duplicate skill id/version"):
            catalog.load()

    def test_staging_and_backup_trees_are_not_indexed(self):
        self.write(self.managed, ".staging/alpha-1234/SKILL.md", "alpha", "staging")
        self.write(self.managed, "drafts/.staging/beta-1234/SKILL.md", "beta", "staging")
        self.write(self.managed, ".staging/gamma.bak-1234/SKILL.md", "gamma", "backup")
        catalog = SkillCatalog(roots=[self.builtin, self.managed])
        catalog.load()
        self.assertEqual(catalog.index, {})

    def test_env_roots_and_builtin_detection(self):
        alpha = self.write(self.builtin, "ops/alpha.md", "alpha", "builtin")
        env = {"SKILLS_DIR": str(self.builtin), "SKILLS_MANAGED_DIR": str(self.managed)}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(managed_root(), self.managed)
            self.assertEqual(catalog_roots(), [self.builtin, self.managed])
            self.assertTrue(is_builtin_path(alpha))
            self.assertEqual(SkillCatalog.default().roots, [self.builtin, self.managed])
        with mock.patch.dict(os.environ, {"SKILLS_DIR": str(self.builtin), "SKILLS_MANAGED_DIR": ""}):
            self.assertEqual(managed_root(), self.builtin)
            self.assertEqual(catalog_roots(), [self.builtin])
            self.assertFalse(is_builtin_path(alpha))

    def test_learn_tool_writes_to_managed_root(self):
        from ts_harness.tools.core_tools import _handle_propose_skill

        env = {"SKILLS_DIR": str(self.builtin), "SKILLS_MANAGED_DIR": str(self.managed)}
        ctx = mock.Mock(agent_id="ops-agent", tenant_id="default")
        with mock.patch.dict(os.environ, env):
            result = _handle_propose_skill({"markdown": SKILL.format(id="learned", where="chat")}, ctx)
        self.assertEqual(result["status"], "draft")
        self.assertTrue((self.managed / "drafts" / "learned.md").is_file())
        self.assertFalse((self.builtin / "drafts").exists())


if __name__ == "__main__":
    unittest.main()
