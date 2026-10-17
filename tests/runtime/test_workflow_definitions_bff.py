from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from starlette.testclient import TestClient

from ts_harness.gateway import asgi_app


class WorkflowDefinitionsBffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store_root = Path(self.tmp.name) / "workflow-definitions"
        self.env = mock.patch.dict(
            "os.environ",
            {
                "TS_HARNESS_WORKFLOW_DEFINITION_STORE": (
                    str(self.store_root)
                )
            },
        )
        self.env.start()
        self.runtime = self._runtime()
        self.auth = mock.Mock(
            actor_id="admin-1",
            roles=["admin"],
            tenant_id="default",
        )
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

    def _runtime(self):
        runtime = mock.Mock()
        runtime.config.gateway.default_tenant_id = "default"
        runtime.config.toolsets = {}
        runtime.config.policy.approval_required_tools = []
        runtime.config.agents = [
            SimpleNamespace(
                id="orchestrator", tool_allow=[], tool_deny=[], toolsets=[], skills=[]
            ),
            SimpleNamespace(
                id="agent-a",
                tool_allow=["read_file", "exec", "google_workspace_docs_append"],
                tool_deny=["exec"],
                toolsets=[],
                skills=[],
            ),
        ]
        runtime.skill_catalog.index = {
            "approved:1.0.0": SimpleNamespace(
                id="approved-skill",
                approval_status="approved",
                trust_level="trusted",
                category="ops",
            ),
            "draft:1.0.0": SimpleNamespace(
                id="draft-skill",
                approval_status="draft",
                trust_level="agent-created",
                category="drafts",
            ),
        }
        runtime.policy.evaluate_identity.return_value = mock.Mock(decision="allow")
        runtime.audit.append = mock.Mock()
        runtime.sessions.get_or_create.return_value = SimpleNamespace(
            session_id="session-1",
        )
        return runtime

    def definition(
        self,
        *,
        workflow_type: str = "managed_flow",
        version: str = "1.0.0",
    ):
        return {
            "workflow_type": workflow_type,
            "definition_version": version,
            "display_name": "Managed flow",
            "stages": [
                {
                    "name": "start",
                    "kind": "task",
                    "agent_id": "agent-a",
                    "skill_hints": ["approved-skill"],
                    "approval_required": True,
                }
            ],
        }

    def assert_non_object_body_returns_400(self, method, url: str):
        for payload in ([], "text"):
            with self.subTest(url=url, payload=payload):
                response = method(url, json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertIn("object", response.json()["error"])
        response = self.client.request(
            method.__name__.upper(),
            url,
            content="null",
            headers={"content-type": "application/json"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("object", response.json()["error"])

    def test_list_and_catalog_include_built_ins_and_injectable_skills(self):
        response = self.client.get("/admin/workflow-definitions")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        built_in = next(
            item
            for item in body["definitions"]
            if item["workflow_type"] == "prd_request"
        )
        self.assertEqual(built_in["status"], "built_in")
        self.assertTrue(built_in["read_only"])
        self.assertNotIn("path", built_in)

        catalog = self.client.get("/admin/workflow-definitions/catalog").json()
        self.assertIn("promote", catalog["hooks"])
        self.assertEqual(catalog["stage_kinds"], ["task", "expand", "compensate"])
        self.assertIn(
            {"id": "orchestrator", "label": "orchestrator"},
            catalog["agents"],
        )
        self.assertIn(
            {"id": "approved-skill", "label": "approved-skill"},
            catalog["skills"],
        )
        self.assertNotIn(
            {"id": "draft-skill", "label": "draft-skill"},
            catalog["skills"],
        )
        self.assertTrue(
            any(
                t["workflow_type"] == "prd_request"
                for t in catalog["templates"]
            )
        )
        children = {item["workflow_type"]: item for item in catalog["child_workflows"]}
        self.assertIn("repo_url", children["deliver_work_order"]["workflow_input_keys"])

    def test_create_update_conflict_returns_409(self):
        created = self.client.post(
            "/admin/workflow-definitions/drafts",
            json={"definition": self.definition()},
        )
        self.assertEqual(created.status_code, 201)
        draft_id = created.json()["draft"]["id"]

        response = self.client.put(
            f"/admin/workflow-definitions/drafts/{draft_id}",
            json={
                "expected_revision": "stale",
                "definition": self.definition(version="1.0.1"),
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("revision", response.json()["error"])

    def test_validate_reports_errors_and_normalized_definition(self):
        created = self.client.post(
            "/admin/workflow-definitions/drafts",
            json={"definition": self.definition()},
        )
        draft_id = created.json()["draft"]["id"]

        response = self.client.post(
            f"/admin/workflow-definitions/drafts/{draft_id}/validate",
            json={
                "definition": {
                    **self.definition(),
                    "stages": [
                        {
                            "name": "start",
                            "kind": "task",
                            "agent_id": "missing-agent",
                            "skill_hints": ["missing-skill"],
                        }
                    ],
                }
            },
        )

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(body["valid"])
        self.assertIn(
            "unknown_agent",
            {issue["code"] for issue in body["issues"]},
        )
        self.assertIn("normalized_definition", body)
        self.runtime.audit.append.assert_any_call(
            tenant_id="default",
            action_type="workflow_definition_validated",
            actor_id="admin-1",
            payload=mock.ANY,
        )

    def test_catalog_lists_agent_tools_without_denied_names(self):
        catalog = self.client.get("/admin/workflow-definitions/catalog").json()

        self.assertEqual(
            catalog["agent_tools"]["agent-a"],
            ["google_workspace_docs_append", "read_file"],
        )
        self.assertEqual(catalog["agent_tools"]["orchestrator"], [])

    def test_validate_checks_stage_tool_allow_against_agent_grant(self):
        created = self.client.post(
            "/admin/workflow-definitions/drafts",
            json={"definition": self.definition()},
        ).json()["draft"]
        url = f"/admin/workflow-definitions/drafts/{created['id']}/validate"

        def validate(tool_allow):
            stage = {**self.definition()["stages"][0], "tool_allow": tool_allow}
            body = self.client.post(
                url, json={"definition": {**self.definition(), "stages": [stage]}}
            ).json()
            return body, [i for i in body["issues"] if i["code"] == "unknown_tool"]

        body, unknown = validate(["google_workspace_docs_append"])
        self.assertTrue(body["valid"])
        self.assertEqual(
            body["normalized_definition"]["stages"][0]["tool_allow"],
            ["google_workspace_docs_append"],
        )

        body, unknown = validate(["read_file", "channel_send"])
        self.assertFalse(body["valid"])
        self.assertEqual([i["path"] for i in unknown], ["$.stages[0].tool_allow[1]"])

        body, unknown = validate(["exec"])
        self.assertFalse(body["valid"])
        self.assertEqual([i["path"] for i in unknown], ["$.stages[0].tool_allow[0]"])

        body, _unknown = validate([])
        self.assertFalse(body["valid"])
        self.assertIn("schema_load_error", {i["code"] for i in body["issues"]})

    def test_validate_rejects_non_object_json_bodies_but_allows_empty(self):
        created = self.client.post(
            "/admin/workflow-definitions/drafts",
            json={"definition": self.definition()},
        ).json()["draft"]
        url = f"/admin/workflow-definitions/drafts/{created['id']}/validate"

        self.assert_non_object_body_returns_400(self.client.post, url)

        response = self.client.post(url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["valid"])

    def test_activate_rejects_semantic_validation_errors(self):
        invalid_cases = [
            (
                "unknown agent",
                {
                    **self.definition(),
                    "stages": [
                        {
                            "name": "start",
                            "kind": "task",
                            "agent_id": "missing-agent",
                            "approval_required": True,
                        }
                    ],
                },
                "unknown_agent",
            ),
            (
                "dependency cycle",
                {
                    **self.definition(),
                    "workflow_type": "managed_cycle",
                    "stages": [
                        {
                            "name": "a",
                            "kind": "task",
                            "agent_id": "agent-a",
                            "depends_on": ["c"],
                        },
                        {
                            "name": "b",
                            "kind": "task",
                            "agent_id": "agent-a",
                            "depends_on": ["a"],
                        },
                        {
                            "name": "c",
                            "kind": "task",
                            "agent_id": "agent-a",
                            "depends_on": ["b"],
                        },
                    ],
                },
                "cycle",
            ),
        ]
        for label, definition, expected_code in invalid_cases:
            with self.subTest(label=label):
                created = self.client.post(
                    "/admin/workflow-definitions/drafts",
                    json={"definition": definition},
                ).json()["draft"]

                response = self.client.post(
                    (
                        "/admin/workflow-definitions/drafts/"
                        f"{created['id']}/activate"
                    ),
                    json={"expected_revision": created["revision"]},
                )

                self.assertEqual(response.status_code, 400)
                self.assertIn(expected_code, response.json()["error"])
                draft_response = self.client.get(
                    f"/admin/workflow-definitions/drafts/{created['id']}"
                )
                self.assertEqual(draft_response.status_code, 200)
                active_path = (
                    self.store_root
                    / "active"
                    / f"{definition['workflow_type']}.json"
                )
                self.assertFalse(active_path.exists())

    def test_activate_reloads_definitions(self):
        created = self.client.post(
            "/admin/workflow-definitions/drafts",
            json={"definition": self.definition()},
        ).json()["draft"]
        engine = mock.Mock()

        with mock.patch.object(asgi_app, "get_workflow_engine", return_value=engine):
            response = self.client.post(
                (
                    "/admin/workflow-definitions/drafts/"
                    f"{created['id']}/activate"
                ),
                json={"expected_revision": created["revision"]},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["active"]["workflow_type"],
            "managed_flow",
        )
        engine.reload_definitions.assert_called_once_with()
        self.runtime.audit.append.assert_any_call(
            tenant_id="default",
            action_type="workflow_definition_activated",
            actor_id="admin-1",
            payload=mock.ANY,
        )

    def test_activate_returns_warning_when_reload_fails(self):
        created = self.client.post(
            "/admin/workflow-definitions/drafts",
            json={"definition": self.definition()},
        ).json()["draft"]
        engine = mock.Mock()
        engine.reload_definitions.return_value = False

        with mock.patch.object(asgi_app, "get_workflow_engine", return_value=engine):
            response = self.client.post(
                (
                    "/admin/workflow-definitions/drafts/"
                    f"{created['id']}/activate"
                ),
                json={"expected_revision": created["revision"]},
            )

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(body["reload_ok"])
        self.assertEqual(body["reload"], {"ok": False})
        self.assertIn("warning", body)
        self.runtime.audit.append.assert_any_call(
            tenant_id="default",
            action_type="workflow_definition_reload_failed",
            actor_id="admin-1",
            payload=mock.ANY,
        )

    def test_activate_warns_when_engine_unavailable_after_commit(self):
        created = self.client.post(
            "/admin/workflow-definitions/drafts",
            json={"definition": self.definition()},
        ).json()["draft"]

        with mock.patch.object(
            asgi_app,
            "get_workflow_engine",
            side_effect=RuntimeError("engine unavailable"),
        ):
            response = self.client.post(
                (
                    "/admin/workflow-definitions/drafts/"
                    f"{created['id']}/activate"
                ),
                json={"expected_revision": created["revision"]},
            )

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(body["reload_ok"])
        self.assertIn("warning", body)
        active = self.client.get("/admin/workflow-definitions/managed_flow")
        self.assertEqual(active.status_code, 200)

    def test_start_rejects_inactive_definition(self):
        response = self.client.post(
            "/admin/workflow-definitions/managed_flow/start",
            json={"summary": "start it"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertIn("active", response.json()["error"])

    def test_start_reloads_engine_if_changed_before_starting(self):
        created = self.client.post(
            "/admin/workflow-definitions/drafts",
            json={"definition": self.definition()},
        ).json()["draft"]
        with mock.patch.object(
            asgi_app,
            "get_workflow_engine",
            return_value=mock.Mock(),
        ):
            activated = self.client.post(
                (
                    "/admin/workflow-definitions/drafts/"
                    f"{created['id']}/activate"
                ),
                json={"expected_revision": created["revision"]},
            )
        self.assertEqual(activated.status_code, 200)
        engine = mock.Mock()
        engine.start_promotion.return_value = {"workflow_id": "workflow-1"}

        with mock.patch.object(
            asgi_app,
            "get_workflow_engine",
            return_value=engine,
        ):
            response = self.client.post(
                "/admin/workflow-definitions/managed_flow/start",
                json={"summary": "start it"},
            )

        self.assertEqual(response.status_code, 200)
        engine.reload_if_changed.assert_called_once_with()
        self.assertEqual(response.json()["workflow_id"], "workflow-1")

    def test_start_returns_503_when_engine_unavailable(self):
        created = self.client.post(
            "/admin/workflow-definitions/drafts",
            json={"definition": self.definition()},
        ).json()["draft"]
        with mock.patch.object(
            asgi_app,
            "get_workflow_engine",
            return_value=mock.Mock(),
        ):
            activated = self.client.post(
                (
                    "/admin/workflow-definitions/drafts/"
                    f"{created['id']}/activate"
                ),
                json={"expected_revision": created["revision"]},
            )
        self.assertEqual(activated.status_code, 200)

        with mock.patch.object(
            asgi_app,
            "get_workflow_engine",
            side_effect=RuntimeError("engine boom"),
        ):
            response = self.client.post(
                "/admin/workflow-definitions/managed_flow/start",
                json={"summary": "start it"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("unavailable", response.json()["error"])

    def test_start_returns_503_when_engine_reload_needed_but_fails(self):
        created = self.client.post(
            "/admin/workflow-definitions/drafts",
            json={"definition": self.definition()},
        ).json()["draft"]
        with mock.patch.object(
            asgi_app,
            "get_workflow_engine",
            return_value=mock.Mock(),
        ):
            activated = self.client.post(
                (
                    "/admin/workflow-definitions/drafts/"
                    f"{created['id']}/activate"
                ),
                json={"expected_revision": created["revision"]},
            )
        self.assertEqual(activated.status_code, 200)
        engine = mock.Mock()
        engine.reload_if_changed.return_value = False

        with mock.patch.object(
            asgi_app,
            "get_workflow_engine",
            return_value=engine,
        ):
            response = self.client.post(
                "/admin/workflow-definitions/managed_flow/start",
                json={"summary": "start it"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("reload", response.json()["error"])
        engine.start_promotion.assert_not_called()

    def test_non_object_json_bodies_return_400(self):
        created = self.client.post(
            "/admin/workflow-definitions/drafts",
            json={"definition": self.definition()},
        ).json()["draft"]

        self.assert_non_object_body_returns_400(
            self.client.post,
            "/admin/workflow-definitions/drafts",
        )
        self.assert_non_object_body_returns_400(
            self.client.put,
            f"/admin/workflow-definitions/drafts/{created['id']}",
        )
        self.assert_non_object_body_returns_400(
            self.client.post,
            f"/admin/workflow-definitions/drafts/{created['id']}/activate",
        )
        self.assert_non_object_body_returns_400(
            self.client.post,
            "/admin/workflow-definitions/managed_flow/start",
        )

    def test_editor_metadata_round_trips_and_active_excludes_it(self):
        metadata = {
            "nodes": [
                {"id": "start", "position": {"x": 10, "y": 20}},
            ],
        }
        created = self.client.post(
            "/admin/workflow-definitions/drafts",
            json={
                "definition": self.definition(),
                "editor_metadata": metadata,
            },
        )
        self.assertEqual(created.status_code, 201)
        draft = created.json()["draft"]
        self.assertEqual(draft["editor_metadata"], metadata)

        updated = self.client.put(
            f"/admin/workflow-definitions/drafts/{draft['id']}",
            json={
                "expected_revision": draft["revision"],
                "definition": self.definition(version="1.0.1"),
            },
        )
        self.assertEqual(updated.status_code, 200)
        updated_draft = updated.json()["draft"]
        self.assertEqual(updated_draft["editor_metadata"], metadata)

        replaced = self.client.put(
            f"/admin/workflow-definitions/drafts/{draft['id']}",
            json={
                "expected_revision": updated_draft["revision"],
                "definition": self.definition(version="1.0.2"),
                "editor_metadata": {"viewport": {"zoom": 1.25}},
            },
        )
        self.assertEqual(replaced.status_code, 200)
        replaced_draft = replaced.json()["draft"]
        self.assertEqual(
            replaced_draft["editor_metadata"],
            {"viewport": {"zoom": 1.25}},
        )

        with mock.patch.object(
            asgi_app,
            "get_workflow_engine",
            return_value=mock.Mock(),
        ):
            activated = self.client.post(
                (
                    "/admin/workflow-definitions/drafts/"
                    f"{draft['id']}/activate"
                ),
                json={"expected_revision": replaced_draft["revision"]},
            )
        self.assertEqual(activated.status_code, 200)
        active_payload = (
            self.store_root / "active" / "managed_flow.json"
        ).read_text()
        self.assertNotIn("editor_metadata", active_payload)

    def test_admin_role_required(self):
        self.auth.roles = ["member"]
        self.runtime.policy.evaluate_identity.return_value = mock.Mock(
            decision="deny",
            reason="role denied",
        )

        response = self.client.post("/admin/workflow-definitions/drafts", json={})

        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
