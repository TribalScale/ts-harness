"""Admin endpoints for pasted and uploaded skill drafts."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

from ts_harness.gateway import asgi_app
from ts_harness.gateway.auth import AuthError

SKILL_MD = "---\nid: launch-kit\nversion: 1.0.0\ncategory: marketing\ndescription: Launch kit\n---\n# Kit\n"


class SkillDraftEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.builtin = Path(tmp.name) / "builtin"
        self.managed = Path(tmp.name) / "managed"
        self.builtin.mkdir()
        self.managed.mkdir()
        self.granted = {"edit_config", "view"}
        self.runtime = mock.Mock()
        self.runtime.config.gateway.default_tenant_id = "default"
        self.runtime.audit.append = mock.Mock()

        def require_admin(runtime, auth, permission="edit_config"):
            if permission not in self.granted:
                raise AuthError(f"missing {permission}", status=403)

        patches = [
            mock.patch.dict(
                os.environ, {"SKILLS_DIR": str(self.builtin), "SKILLS_MANAGED_DIR": str(self.managed)}
            ),
            mock.patch.object(asgi_app, "get_runtime", return_value=self.runtime),
            mock.patch.object(asgi_app, "_auth", return_value=mock.Mock(actor_id="admin-1", tenant_id="default")),
            mock.patch.object(asgi_app, "_check_origin", return_value=None),
            mock.patch.object(asgi_app, "_require_admin", side_effect=require_admin),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.client = TestClient(asgi_app.app)

    def post(self, body):
        return self.client.post("/admin/skills/drafts", json=body)

    def folder(self):
        return {
            "mode": "folder",
            "files": [
                {"path": "SKILL.md", "content": SKILL_MD},
                {"path": "stages/plan.md", "content": "# Plan\n"},
            ],
        }

    def test_text_mode_creates_draft_and_audits(self):
        response = self.post(
            {"mode": "text", "markdown": "# Body\n", "id": "paste-skill", "category": "ops", "description": "Pasted"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["inbox_href"], "#/inbox/skill_draft/paste-skill")
        self.assertTrue((self.managed / "drafts" / "paste-skill.md").is_file())
        audit = self.runtime.audit.append.call_args.kwargs
        self.assertEqual(audit["action_type"], "skill_proposed")
        self.assertEqual(audit["payload"]["source"], "admin_paste")

    def test_folder_mode_and_file_read(self):
        self.assertEqual(self.post(self.folder()).status_code, 200)
        self.granted = {"view"}
        response = self.client.get("/admin/skills/drafts/launch-kit/files/stages/plan.md")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"path": "stages/plan.md", "content": "# Plan\n"})

    def test_create_requires_edit_config(self):
        self.granted = {"view"}
        self.assertEqual(self.post(self.folder()).status_code, 403)

    def test_error_includes_code_and_path(self):
        body = self.folder()
        body["files"].append({"path": "tools/run.py", "content": "print(1)"})
        response = self.post(body)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], "skill_upload_extension")
        self.assertEqual(response.json()["path"], "tools/run.py")

    def test_size_cap_applies_without_content_length(self):
        big = json.dumps({"mode": "text", "markdown": "x" * (asgi_app.MAX_SKILL_DRAFT_BODY_BYTES + 10)}).encode()

        def chunks():
            for start in range(0, len(big), 65536):
                yield big[start : start + 65536]

        response = self.client.post(
            "/admin/skills/drafts", content=chunks(), headers={"Content-Type": "application/json"}
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error_code"], "skill_upload_total_size")

    def test_routes_are_not_swallowed_by_catch_all(self):
        self.assertEqual(self.post(self.folder()).status_code, 200)
        detail = self.client.get("/admin/skills/drafts/launch-kit")
        self.assertEqual(detail.json()["layout"], "folder")
        self.assertEqual(self.client.get("/admin/skills/drafts/launch-kit/files/SKILL.md").json()["path"], "SKILL.md")

        self.runtime.skill_catalog.reload = mock.Mock()
        approved = self.client.post("/admin/skills/drafts/launch-kit/approve", json={})
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["layout"], "folder")
        self.assertTrue((self.managed / "marketing" / "launch-kit" / "stages" / "plan.md").is_file())

        self.assertEqual(self.post(dict(self.folder(), revision_of="launch-kit")).status_code, 400)
        body = self.folder()
        body["files"][0]["content"] = SKILL_MD.replace("launch-kit", "other-kit")
        self.assertEqual(self.post(body).status_code, 200)
        rejected = self.client.post("/admin/skills/drafts/other-kit/reject", json={})
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.assertFalse((self.managed / "drafts" / "other-kit").exists())


if __name__ == "__main__":
    unittest.main()
