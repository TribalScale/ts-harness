from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ts_harness.gateway import asgi_app
from starlette.testclient import TestClient


def _fragment(display_name: str = "Review pair"):
    return {
        "fragment_id": "review_pair",
        "definition_version": "1.0.0",
        "display_name": display_name,
        "params": {"source": {"kind": "stage_ref"}},
        "inputs": {"repo_url": {"required": True}},
        "stages": [
            {
                "name": "review",
                "kind": "task",
                "agent_id": "agent-a",
                "depends_on": ["{{param.source}}"],
                "input_map": {"repo_url": "$input.repo_url"},
                "approval_required": True,
            }
        ],
    }


class WorkflowFragmentsBffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store_root = Path(self.tmp.name) / "fragments"
        self.env = mock.patch.dict(
            "os.environ",
            {"TS_HARNESS_FRAGMENT_DEFINITION_STORE": str(self.store_root)},
        )
        self.env.start()
        self.runtime = mock.Mock()
        self.runtime.config.agents = [SimpleNamespace(id="orchestrator"), SimpleNamespace(id="agent-a")]
        self.runtime.skill_catalog.index = {}
        self.runtime.audit.append = mock.Mock()
        self.auth = mock.Mock(actor_id="admin-1", roles=["admin"], tenant_id="default")
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
        self.env.stop()
        self.tmp.cleanup()

    def _audit_actions(self):
        return [call.kwargs["action_type"] for call in self.runtime.audit.append.call_args_list]

    def _create(self, definition=None):
        response = self.client.post("/admin/workflow-fragments/drafts", json={"definition": definition or _fragment()})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["draft"]

    def _activate(self, draft):
        return self.client.post(
            f"/admin/workflow-fragments/drafts/{draft['id']}/activate",
            json={"expected_revision": draft["revision"]},
        )

    def test_list_includes_builtin_verified_merge(self):
        body = self.client.get("/admin/workflow-fragments").json()
        builtin = next(item for item in body["fragments"] if item["fragment_id"] == "verified_merge")
        self.assertEqual(builtin["status"], "built_in")
        self.assertTrue(builtin["read_only"])
        self.assertEqual(builtin["stage_names"], [
                "await_pr_checks",
                "review_precheck",
                "code_review",
                "finalize_code_review",
                "qa_gate",
                "ensure_fresh",
                "await_human_merge",
            ])
        self.assertIn("implement_stage", builtin["params"])

    def test_draft_validate_activate_and_get_by_version(self):
        draft = self._create()
        validation = self.client.post(f"/admin/workflow-fragments/drafts/{draft['id']}/validate", json={}).json()
        self.assertTrue(validation["valid"], validation["issues"])
        response = self._activate(draft)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["active"]["fragment_id"], "review_pair")

        got = self.client.get("/admin/workflow-fragments/review_pair", params={"version": "1.0.0"}).json()
        self.assertEqual(got["definition"]["stages"][0]["name"], "review")
        listed = self.client.get("/admin/workflow-fragments").json()["fragments"]
        active = next(item for item in listed if item.get("fragment_id") == "review_pair")
        self.assertEqual(active["status"], "active")
        self.assertEqual(active["stage_names"], ["review"])
        self.assertEqual(
            self._audit_actions(),
            [
                "permission_allowed",
                "workflow_fragment_draft_created",
                "permission_allowed",
                "workflow_fragment_validated",
                "permission_allowed",
                "workflow_fragment_activated",
            ],
        )

    def test_reusing_an_activated_version_with_new_content_is_rejected(self):
        self._activate(self._create())
        response = self._activate(self._create(_fragment(display_name="changed")))
        self.assertEqual(response.status_code, 400)
        self.assertIn("definition_version", response.json()["error"])

    def test_activation_fails_closed_on_validation_errors(self):
        bad = _fragment()
        bad["stages"][0]["agent_id"] = "ghost"
        response = self._activate(self._create(bad))
        self.assertEqual(response.status_code, 400)
        self.assertIn("unknown_agent", response.json()["error"])

    def test_cannot_shadow_builtin_fragment(self):
        builtin = self.client.get("/admin/workflow-fragments/verified_merge").json()["definition"]
        response = self._activate(self._create(builtin))
        self.assertEqual(response.status_code, 400)
        self.assertIn("built-in fragment", response.json()["error"])

    def test_unknown_fragment_and_version_return_404(self):
        self.assertEqual(self.client.get("/admin/workflow-fragments/nope").status_code, 404)
        response = self.client.get("/admin/workflow-fragments/verified_merge", params={"version": "9.9.9"})
        self.assertEqual(response.status_code, 404)

    def test_non_admin_is_rejected(self):
        with mock.patch.object(
            asgi_app,
            "_require_admin",
            side_effect=asgi_app.AuthError("admin role required", 403),
        ):
            for method, url in (
                ("get", "/admin/workflow-fragments"),
                ("post", "/admin/workflow-fragments/drafts"),
            ):
                with self.subTest(url=url):
                    kwargs = {"json": {"definition": _fragment()}} if method == "post" else {}
                    response = getattr(self.client, method)(url, **kwargs)
                    self.assertEqual(response.status_code, 403)
        self.runtime.audit.append.assert_not_called()


if __name__ == "__main__":
    unittest.main()
