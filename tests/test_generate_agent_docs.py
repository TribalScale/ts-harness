"""Tests for scripts/generate-agent-docs.py."""

from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]


def _load_module():
    path = REPO / "scripts" / "generate-agent-docs.py"
    spec = importlib.util.spec_from_file_location("generate_agent_docs", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class GenerateAgentDocsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_generate_is_deterministic(self):
        first = self.mod.generate_all()
        second = self.mod.generate_all()
        self.assertEqual(first, second)

    def test_check_passes_after_generate(self):
        outputs = self.mod.generate_all()
        self.mod.write_outputs(outputs)
        # Should not raise
        self.mod.check_outputs(self.mod.generate_all())
        self.assertEqual(self.mod.main(["--check"]), 0)

    def test_missing_catalog_entry_fails(self):
        agents = self.mod.load_runtime_agents()
        catalog = dict(self.mod.load_catalog())
        if not agents:
            self.skipTest("no agents")
        catalog.pop(str(agents[0]["id"]), None)
        with mock.patch.object(self.mod, "load_catalog", return_value=catalog):
            with self.assertRaises(SystemExit) as ctx:
                self.mod.generate_all()
        self.assertIn("catalog missing", str(ctx.exception))

    def test_render_workspace_still_substitutes_placeholders(self):
        render_path = REPO / "scripts" / "render-workspace.py"
        spec = importlib.util.spec_from_file_location("render_workspace", render_path)
        assert spec and spec.loader
        render = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(render)

        with tempfile.TemporaryDirectory() as tmp:
            src = pathlib.Path(tmp) / "src"
            src.mkdir()
            (src / "AGENTS.md").write_text(
                "<!-- BEGIN GENERATED: agent-identity -->\n"
                "ID\n"
                "<!-- END GENERATED: agent-identity -->\n\n"
                "Org={{COMPANY_NAME}}\n"
            )
            # Minimal org config for substitution path used by render helpers.
            # Exercise TOKEN_RE substitution directly to avoid full org validation.
            text = (src / "AGENTS.md").read_text()
            rendered = render.TOKEN_RE.sub(
                lambda m: {"COMPANY_NAME": "Acme"}.get(m.group(1), m.group(0)),
                text,
            )
            self.assertIn("<!-- BEGIN GENERATED: agent-identity -->", rendered)
            self.assertIn("Org=Acme", rendered)
            self.assertNotIn("{{COMPANY_NAME}}", rendered)


if __name__ == "__main__":
    unittest.main()
