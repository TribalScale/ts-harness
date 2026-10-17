from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

from ts_harness.config.schema import AgentConfig, GatewayConfig, RuntimeConfig, TenantConfig
from ts_harness.gateway import asgi_app
from ts_harness.skills.catalog import SkillCatalog


HELLO_MD = """---
id: hello-skill
version: 1.0.0
category: general
description: Hello skill
trust_level: builtin
approval_status: approved
---
Hello body.
"""


class SkillsCatalogBffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "general").mkdir()
        (root / "general" / "hello-skill.md").write_text(HELLO_MD, encoding="utf-8")
        catalog = SkillCatalog(root)
        catalog.load()
        self.runtime = mock.Mock()
        self.runtime.config = RuntimeConfig(
            gateway=GatewayConfig(token="t", default_tenant_id="default"),
            tenants=[TenantConfig(id="default", display_name="Default")],
            agents=[AgentConfig(id="orchestrator", workspace="/w", model="m", default=True, skills=["general"])],
        )
        self.runtime.skill_catalog = catalog
        self.runtime.policy.evaluate_identity.return_value = mock.Mock(decision="allow")
        self.auth = mock.Mock(actor_id="admin", roles=["admin"], tenant_id="default")
        self.patches = [
            mock.patch.object(asgi_app, "get_runtime", return_value=self.runtime),
            mock.patch.object(asgi_app, "_auth", return_value=self.auth),
            mock.patch.object(asgi_app, "_check_origin", return_value=None),
            mock.patch(
                "ts_harness.skills.catalog_view._iter_workflow_documents",
                return_value=[
                    {
                        "workflow_type": "demo_flow",
                        "stages": [{"name": "intake", "skill_hints": ["hello-skill"]}],
                    }
                ],
            ),
            mock.patch(
                "ts_harness.skills.catalog_view.load_cron_file",
                return_value=[{"name": "nightly", "skill_id": "hello-skill"}],
            ),
        ]
        for patch in self.patches:
            patch.start()
        self.client = TestClient(asgi_app.app)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def test_list_and_detail(self):
        listed = self.client.get("/admin/skills")
        self.assertEqual(listed.status_code, 200)
        ids = {item["id"] for item in listed.json()["skills"]}
        self.assertIn("hello-skill", ids)
        detail = self.client.get("/admin/skills/hello-skill")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["used_by"]["workflows"][0]["stage"], "intake")
        self.assertEqual(detail.json()["used_by"]["cron"][0]["name"], "nightly")

    def test_unknown_id_404(self):
        response = self.client.get("/admin/skills/missing")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
