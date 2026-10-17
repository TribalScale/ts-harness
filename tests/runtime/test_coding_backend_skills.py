"""Tests for coding-backend skill install/materialize."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ts_harness.implementation.coding_backend_skills import (
    SUPERPOWERS_OPENCODE_PLUGIN,
    ensure_opencode_project_config,
    ensure_opencode_superpowers_plugin,
    install_coding_backend_skills,
    list_coding_backend_skill_dirs,
    materialize_coding_backend_skills,
    project_skills_relpath,
)


class CodingBackendSkillsTests(unittest.TestCase):
    def test_project_skills_relpath_opencode(self):
        self.assertEqual(project_skills_relpath("opencode"), ".opencode/skills")

    def test_project_skills_relpath_unknown(self):
        with self.assertRaises(ValueError):
            project_skills_relpath("unknown-tool")

    def test_list_skills_from_repo_source(self):
        dirs = list_coding_backend_skill_dirs()
        names = {d.name for d in dirs}
        self.assertIn("implement-work-order", names)
        self.assertIn("coding-practices", names)
        self.assertIn("bdd-acceptance", names)

    def test_materialize_into_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "app"
            repo.mkdir()
            written = materialize_coding_backend_skills(repo, coding_backend="opencode")
            self.assertTrue(any(p.endswith("implement-work-order/SKILL.md") for p in written))
            skill = repo / ".opencode/skills/implement-work-order/SKILL.md"
            self.assertTrue(skill.is_file())
            text = skill.read_text(encoding="utf-8")
            self.assertIn("name: implement-work-order", text)
            self.assertIn("docs/planner/work-orders/", text)
            self.assertNotIn("superpowers", text.lower())
            practices = (repo / ".opencode/skills/coding-practices/SKILL.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST", practices)
            self.assertIn("NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE", practices)
            self.assertIn("NO FIXES WITHOUT ROOT CAUSE INVESTIGATION FIRST", practices)
            self.assertNotIn("superpowers", practices.lower())

    def test_install_to_tools_mirror(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "coding-backend-skills"
            names = install_coding_backend_skills(dest)
            self.assertIn("implement-work-order", names)
            self.assertTrue((dest / "implement-work-order" / "SKILL.md").is_file())

    def test_ensure_opencode_project_config_strips_superpowers(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".opencode"
            target.mkdir()
            (target / "opencode.json").write_text(
                json.dumps({"plugin": [SUPERPOWERS_OPENCODE_PLUGIN, "other@npm:example"]}),
                encoding="utf-8",
            )
            path = ensure_opencode_project_config(target)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn(SUPERPOWERS_OPENCODE_PLUGIN, data["plugin"])
            self.assertIn("other@npm:example", data["plugin"])

    def test_coding_backend_skills_do_not_name_superpowers(self):
        root = Path(__file__).resolve().parents[2] / "skills" / "coding-backend"
        for path in root.rglob("*.md"):
            text = path.read_text(encoding="utf-8").lower()
            self.assertNotIn("superpowers", text, msg=str(path))
            self.assertNotIn("obra/superpowers", text, msg=str(path))

    def test_legacy_alias_also_strips_superpowers(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".opencode"
            path = ensure_opencode_superpowers_plugin(target)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data.get("plugin"), [])


if __name__ == "__main__":
    unittest.main()
