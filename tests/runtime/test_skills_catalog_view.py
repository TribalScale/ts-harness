from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ts_harness.config.schema import AgentConfig, GatewayConfig, RuntimeConfig, TenantConfig
from ts_harness.skills.catalog import SkillCatalog
from ts_harness.skills.catalog_view import get_skill_catalog_item, list_skill_catalog


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

DRAFT_MD = """---
id: pending-skill
version: 1.0.0
category: drafts
description: Pending
trust_level: agent-created
approval_status: draft
---
Draft body.
"""

CODING_MD = """---
id: coding-hidden
version: 1.0.0
category: coding-backend
description: Hidden
trust_level: builtin
approval_status: approved
---
Hidden.
"""


def _write_skills(root: Path) -> None:
    (root / "general").mkdir(parents=True)
    (root / "general" / "hello-skill.md").write_text(HELLO_MD, encoding="utf-8")
    (root / "drafts").mkdir(parents=True)
    (root / "drafts" / "pending-skill.md").write_text(DRAFT_MD, encoding="utf-8")
    (root / "coding-backend" / "foo").mkdir(parents=True)
    (root / "coding-backend" / "foo" / "SKILL.md").write_text(CODING_MD, encoding="utf-8")


def _runtime(root: Path) -> SimpleNamespace:
    catalog = SkillCatalog(root)
    catalog.load()
    config = RuntimeConfig(
        gateway=GatewayConfig(token="t", default_tenant_id="default"),
        tenants=[TenantConfig(id="default", display_name="Default")],
        agents=[
            AgentConfig(
                id="orchestrator",
                workspace="/w",
                model="m",
                default=True,
                skills=["general"],
            ),
            AgentConfig(
                id="dev-agent",
                workspace="/w",
                model="m",
                skills=["workflows"],
            ),
        ],
    )
    return SimpleNamespace(skill_catalog=catalog, config=config)


class SkillsCatalogViewTests(unittest.TestCase):
    def test_hides_coding_backend_and_drafts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_skills(root)
            payload = list_skill_catalog(_runtime(root), workflows=[], cron_jobs=[])
            ids = {item["id"] for item in payload["skills"]}
            self.assertIn("hello-skill", ids)
            self.assertNotIn("coding-hidden", ids)
            self.assertNotIn("pending-skill", ids)
            hello = next(item for item in payload["skills"] if item["id"] == "hello-skill")
            self.assertTrue(hello["doctor_ok"])
            self.assertIn("orchestrator", hello["agents"])

    def test_used_by_workflow_and_cron(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_skills(root)
            workflows = [
                {
                    "workflow_type": "demo_flow",
                    "stages": [{"name": "intake", "skill_hints": ["hello-skill"]}],
                }
            ]
            cron_jobs = [{"name": "nightly", "skill_id": "hello-skill"}]
            detail = get_skill_catalog_item(
                _runtime(root),
                "hello-skill",
                workflows=workflows,
                cron_jobs=cron_jobs,
            )
            self.assertEqual(detail["used_by_workflows"], 1)
            self.assertEqual(detail["used_by_cron"], 1)
            self.assertEqual(detail["used_by"]["workflows"], [{"workflow_type": "demo_flow", "stage": "intake"}])
            self.assertEqual(detail["used_by"]["cron"], [{"name": "nightly"}])

    def test_used_by_reads_managed_active_definition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_skills(root)
            store = SimpleNamespace(
                list_active=lambda: [{"workflow_type": "managed_flow"}],
                get_active=lambda identity: (
                    {
                        "workflow_type": identity,
                        "stages": [{"name": "review", "skill_hints": ["hello-skill"]}],
                    }
                    if identity == "managed_flow"
                    else None
                ),
                list_definitions=lambda: [
                    {
                        "definition": {
                            "workflow_type": "draft_flow",
                            "stages": [{"name": "intake", "skill_hints": ["hello-skill"]}],
                        }
                    }
                ],
            )
            with patch(
                "ts_harness.gateway.workflow_definitions_bff.workflow_definition_store",
                return_value=store,
            ):
                detail = get_skill_catalog_item(_runtime(root), "hello-skill", cron_jobs=[])
            usages = detail["used_by"]["workflows"]
            self.assertIn({"workflow_type": "managed_flow", "stage": "review"}, usages)
            self.assertIn({"workflow_type": "draft_flow", "stage": "intake"}, usages)

    def test_unknown_skill_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_skills(root)
            with self.assertRaises(KeyError):
                get_skill_catalog_item(_runtime(root), "missing")


if __name__ == "__main__":
    unittest.main()
