from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

from ts_harness.config.schema import load_runtime_config
from ts_harness.gateway import asgi_app
from ts_harness.gateway.auth import AuthContext
from ts_harness.gateway.agents_overlay_bff import (
    activate_agents_admin,
    create_agents_draft_admin,
    get_agents_admin,
    validate_agents_draft_admin,
)
from ts_harness.gateway.mcp_settings_bff import (
    activate_mcp_settings_admin,
    create_mcp_settings_draft_admin,
)
from ts_harness.skills.catalog import SkillCatalog
from ts_harness.workflows.agents_overlay_store import AgentsOverlayStore
import ts_harness.workflows.agents_overlay_store as agents_store_mod
import ts_harness.workflows.mcp_settings_store as mcp_store_mod
from ts_harness.workflows.mcp_settings_store import McpSettingsStore


REPO_ROOT = Path(__file__).resolve().parents[2]
YAML_PATH = REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"


def _auth() -> AuthContext:
    return AuthContext(actor_id="admin", tenant_id="default", roles=["admin"])


def _legal_definition() -> dict:
    return {
        "definition_type": "agents",
        "definition_version": "1.0.0",
        "agents": {
            "legal-agent": {
                "workspace": "/workspace/legal-agent",
                "model": "openai/gpt-4o",
                "display_name": "Legal",
                "soul": "Cautious counsel.",
                "skills": ["ops"],
                "toolsets": ["core-read"],
                "tools": {"allow": ["recall_memory"], "deny": ["exec"]},
                "default": False,
                "delegatable": True,
            }
        },
    }


def _runtime(*, workspace_root: Path | None = None) -> mock.MagicMock:
    config = load_runtime_config(YAML_PATH)
    catalog = SkillCatalog(REPO_ROOT / "skills")
    catalog.load()
    runtime = mock.MagicMock()
    runtime.config = config
    runtime._yaml_agents_baseline = deepcopy(list(config.agents))
    runtime.skill_catalog = catalog
    runtime.agents_workspace_root = workspace_root
    runtime.audit = mock.MagicMock()
    runtime.policy.evaluate_identity.return_value = mock.Mock(decision="allow")
    return runtime


class AgentsOverlayBffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        agents_store_mod._STORE = AgentsOverlayStore(Path(self.tmp.name) / "agents")
        self.workspace = Path(self.tmp.name) / "workspace"

    def tearDown(self):
        agents_store_mod._STORE = None
        self.tmp.cleanup()

    def test_activate_reloads_get_agent_and_subagent_allow(self):
        runtime = _runtime(workspace_root=self.workspace)
        created = create_agents_draft_admin(
            runtime, _auth(), {"definition": _legal_definition()}
        )
        draft = created["draft"]
        activate_agents_admin(
            runtime,
            _auth(),
            {"draft_id": draft["id"], "expected_revision": draft["revision"]},
        )
        self.assertEqual(runtime.config.get_agent("legal-agent").model, "openai/gpt-4o")
        self.assertIn("legal-agent", runtime.config.default_agent().subagent_allow)

    def test_activate_writes_agents_md_and_leaves_yaml_untouched(self):
        before = YAML_PATH.read_bytes()
        runtime = _runtime(workspace_root=self.workspace)
        created = create_agents_draft_admin(
            runtime, _auth(), {"definition": _legal_definition()}
        )
        draft = created["draft"]
        activate_agents_admin(
            runtime,
            _auth(),
            {"draft_id": draft["id"], "expected_revision": draft["revision"]},
        )
        agents_md = self.workspace / "legal-agent" / "AGENTS.md"
        text = agents_md.read_text(encoding="utf-8")
        self.assertIn("Legal", text)
        self.assertIn("legal-agent", text)
        self.assertIn("Cautious counsel.", text)
        self.assertEqual(YAML_PATH.read_bytes(), before)

    def test_get_roster_includes_draft_only_overlay_agent(self):
        runtime = _runtime(workspace_root=self.workspace)
        create_agents_draft_admin(runtime, _auth(), {"definition": _legal_definition()})
        roster = get_agents_admin(runtime)["roster"]
        row = next(item for item in roster if item["id"] == "legal-agent")
        self.assertEqual(row["source"], "overlay")
        self.assertEqual(row["model"], "openai/gpt-4o")

    def test_activate_resolves_overlay_replacement_of_yaml_agent(self):
        runtime = _runtime(workspace_root=self.workspace)
        definition = {
            "definition_type": "agents",
            "definition_version": "1.0.0",
            "agents": {
                "orchestrator": {
                    "workspace": "/workspace/orchestrator",
                    "model": "openai/gpt-4o",
                    "skills": ["ops"],
                    "toolsets": ["core-read"],
                    "default": False,
                }
            },
        }
        created = create_agents_draft_admin(runtime, _auth(), {"definition": definition})
        runtime.skill_catalog.resolve_for_agent = mock.Mock(
            return_value=([], ["skip orchestrator: none"])
        )
        with self.assertRaises(ValueError):
            activate_agents_admin(
                runtime,
                _auth(),
                {
                    "draft_id": created["draft"]["id"],
                    "expected_revision": created["draft"]["revision"],
                },
            )
        self.assertIsNone(agents_store_mod.agents_overlay_store().get_active("agents"))

    def test_validate_fail_closed_when_resolve_returns_empty(self):
        runtime = _runtime(workspace_root=self.workspace)
        created = create_agents_draft_admin(
            runtime, _auth(), {"definition": _legal_definition()}
        )
        runtime.skill_catalog.resolve_for_agent = mock.Mock(
            return_value=([], ["skip legal-agent: none"])
        )
        result = validate_agents_draft_admin(
            runtime, created["draft"]["id"], tenant_id="default"
        )
        self.assertFalse(result["valid"])
        self.assertTrue(
            any("unresolvable overlay agent" in issue.get("message", "") for issue in result["issues"])
        )

    def test_settings_and_mcp_activate_do_not_write_agents_store(self):
        mcp_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(mcp_tmp.cleanup)
        mcp_store_mod._STORE = McpSettingsStore(Path(mcp_tmp.name))
        self.addCleanup(setattr, mcp_store_mod, "_STORE", None)
        runtime = _runtime(workspace_root=self.workspace)
        created = create_mcp_settings_draft_admin(
            runtime,
            _auth(),
            {
                "definition": {
                    "definition_type": "mcp_settings",
                    "definition_version": "1.0.0",
                    "servers": {},
                }
            },
        )
        draft = created["draft"]
        activate_mcp_settings_admin(
            runtime,
            _auth(),
            {"draft_id": draft["id"], "expected_revision": draft["revision"]},
        )
        store = agents_store_mod.agents_overlay_store()
        self.assertIsNone(store.get_active("agents"))


class AgentsOverlayAsgiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        agents_store_mod._STORE = AgentsOverlayStore(Path(self.tmp.name))
        self.runtime = _runtime(workspace_root=Path(self.tmp.name) / "workspace")
        self.auth = mock.Mock(actor_id="admin", roles=["admin"], tenant_id="default")
        self.runtime.policy.evaluate_identity.return_value = mock.Mock(decision="allow")
        self.patches = [
            mock.patch.object(asgi_app, "get_runtime", return_value=self.runtime),
            mock.patch.object(asgi_app, "_auth", return_value=self.auth),
            mock.patch.object(asgi_app, "_check_origin", return_value=None),
        ]
        for patch in self.patches:
            patch.start()
        self.client = TestClient(asgi_app.app)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        agents_store_mod._STORE = None
        self.tmp.cleanup()

    def test_non_admin_forbidden(self):
        self.auth.roles = ["member"]
        self.runtime.policy.evaluate_identity.return_value = mock.Mock(
            decision="deny",
            reason="role denied",
        )
        response = self.client.post("/admin/agents/drafts", json={})
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
