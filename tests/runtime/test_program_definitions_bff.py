from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from starlette.testclient import TestClient

from ts_harness.gateway import asgi_app


class ProgramDefinitionsBffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store_root = Path(self.tmp.name) / "program-definitions"
        self.env = mock.patch.dict(
            "os.environ",
            {
                "TS_HARNESS_PROGRAM_DEFINITION_STORE": (
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
        runtime.config.agents = [
            SimpleNamespace(id="orchestrator"),
            SimpleNamespace(id="agent-a"),
        ]
        runtime.skill_catalog.index = {}
        runtime.policy.evaluate_identity.return_value = mock.Mock(decision="allow")
        runtime.audit.append = mock.Mock()
        runtime.sessions.get_or_create.return_value = SimpleNamespace(
            session_id="session-1",
        )
        return runtime

    def definition(
        self,
        *,
        program_type: str = "managed_program",
        version: str = "1.0.0",
    ):
        return {
            "program_type": program_type,
            "definition_version": version,
            "display_name": "Managed program",
            "startable_nodes": ["start"],
            "nodes": [
                {
                    "id": "start",
                    "default_workflow_type": "prd_request",
                    "workflow_variants": [],
                    "required_artifacts": [],
                    "produces_artifacts": ["approved_prd"],
                    "input_bindings": [
                        {"kind": "program_summary", "target": "summary"},
                    ],
                    "completion_adapter": "extract_prd_outputs",
                    "terminal": True,
                }
            ],
            "edges": [],
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

    def test_list_and_catalog_include_built_ins_and_catalog_fields(self):
        response = self.client.get("/admin/program-definitions")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        built_in = next(
            item
            for item in body["definitions"]
            if item["program_type"] == "software_delivery"
        )
        self.assertEqual(built_in["status"], "built_in")
        self.assertTrue(built_in["read_only"])
        self.assertNotIn("path", built_in)

        catalog = self.client.get("/admin/program-definitions/catalog").json()
        self.assertIn("manual", catalog["transition_modes"])
        self.assertIn("automatic", catalog["transition_modes"])
        self.assertTrue(
            any(
                item["id"] == "extract_prd_outputs"
                for item in catalog["adapters"]
            )
        )
        self.assertTrue(
            any(
                item["workflow_type"] == "prd_request"
                for item in catalog["workflows"]
            )
        )
        self.assertTrue(
            any(
                t["program_type"] == "software_delivery"
                for t in catalog["templates"]
            )
        )

    def test_create_update_conflict_returns_409(self):
        created = self.client.post(
            "/admin/program-definitions/drafts",
            json={"definition": self.definition()},
        )
        self.assertEqual(created.status_code, 201)
        draft_id = created.json()["draft"]["id"]

        response = self.client.put(
            f"/admin/program-definitions/drafts/{draft_id}",
            json={
                "expected_revision": "stale",
                "definition": self.definition(version="1.0.1"),
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("revision", response.json()["error"])

    def test_validate_reports_errors_and_normalized_definition(self):
        created = self.client.post(
            "/admin/program-definitions/drafts",
            json={"definition": self.definition()},
        )
        draft_id = created.json()["draft"]["id"]

        response = self.client.post(
            f"/admin/program-definitions/drafts/{draft_id}/validate",
            json={
                "definition": {
                    **self.definition(),
                    "nodes": [
                        {
                            "id": "start",
                            "default_workflow_type": "missing_workflow",
                            "workflow_variants": [],
                            "required_artifacts": [],
                            "produces_artifacts": [],
                            "input_bindings": [
                                {"kind": "program_summary", "target": "summary"},
                            ],
                            "completion_adapter": "extract_prd_outputs",
                            "terminal": True,
                        }
                    ],
                }
            },
        )

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(body["valid"])
        codes = {issue["code"] for issue in body["issues"]}
        self.assertIn("unresolved_workflow", codes)
        self.assertIn("normalized_definition", body)
        self.runtime.audit.append.assert_any_call(
            tenant_id="default",
            action_type="program_definition_validated",
            actor_id="admin-1",
            payload=mock.ANY,
        )

    def test_validate_rejects_non_object_json_bodies_but_allows_empty(self):
        created = self.client.post(
            "/admin/program-definitions/drafts",
            json={"definition": self.definition()},
        ).json()["draft"]
        url = f"/admin/program-definitions/drafts/{created['id']}/validate"

        self.assert_non_object_body_returns_400(self.client.post, url)

        response = self.client.post(url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["valid"])

    def test_activate_rejects_semantic_validation_errors(self):
        definition = {
            **self.definition(program_type="managed_bad"),
            "nodes": [
                {
                    "id": "start",
                    "default_workflow_type": "missing_workflow",
                    "workflow_variants": [],
                    "required_artifacts": [],
                    "produces_artifacts": [],
                    "input_bindings": [
                        {"kind": "program_summary", "target": "summary"},
                    ],
                    "completion_adapter": "extract_prd_outputs",
                    "terminal": True,
                }
            ],
        }
        created = self.client.post(
            "/admin/program-definitions/drafts",
            json={"definition": definition},
        ).json()["draft"]

        response = self.client.post(
            f"/admin/program-definitions/drafts/{created['id']}/activate",
            json={"expected_revision": created["revision"]},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("workflow", response.json()["error"].lower())
        draft_response = self.client.get(
            f"/admin/program-definitions/drafts/{created['id']}"
        )
        self.assertEqual(draft_response.status_code, 200)
        active_path = self.store_root / "active" / "managed_bad.json"
        self.assertFalse(active_path.exists())

    def test_activate_reloads_definitions(self):
        created = self.client.post(
            "/admin/program-definitions/drafts",
            json={"definition": self.definition()},
        ).json()["draft"]
        registry = mock.Mock()
        registry.reload_definitions.return_value = True

        with mock.patch.object(
            asgi_app,
            "get_program_definition_registry",
            return_value=registry,
        ):
            response = self.client.post(
                (
                    "/admin/program-definitions/drafts/"
                    f"{created['id']}/activate"
                ),
                json={"expected_revision": created["revision"]},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["active"]["program_type"],
            "managed_program",
        )
        registry.reload_definitions.assert_called_once_with()
        self.runtime.audit.append.assert_any_call(
            tenant_id="default",
            action_type="program_definition_activated",
            actor_id="admin-1",
            payload=mock.ANY,
        )

    def test_activate_returns_warning_when_reload_fails(self):
        created = self.client.post(
            "/admin/program-definitions/drafts",
            json={"definition": self.definition()},
        ).json()["draft"]
        registry = mock.Mock()
        registry.reload_definitions.return_value = False

        with mock.patch.object(
            asgi_app,
            "get_program_definition_registry",
            return_value=registry,
        ):
            response = self.client.post(
                (
                    "/admin/program-definitions/drafts/"
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
            action_type="program_definition_reload_failed",
            actor_id="admin-1",
            payload=mock.ANY,
        )

    def test_activate_warns_when_registry_unavailable_after_commit(self):
        created = self.client.post(
            "/admin/program-definitions/drafts",
            json={"definition": self.definition()},
        ).json()["draft"]

        with mock.patch.object(
            asgi_app,
            "get_program_definition_registry",
            side_effect=RuntimeError("registry unavailable"),
        ):
            response = self.client.post(
                (
                    "/admin/program-definitions/drafts/"
                    f"{created['id']}/activate"
                ),
                json={"expected_revision": created["revision"]},
            )

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(body["reload_ok"])
        self.assertIn("warning", body)
        active = self.client.get("/admin/program-definitions/managed_program")
        self.assertEqual(active.status_code, 200)

    def test_start_rejects_unknown_definition(self):
        response = self.client.post(
            "/admin/program-definitions/missing_program/start",
            json={"summary": "start it", "entry_phase": "prd"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertIn("program", response.json()["error"].lower())

    def test_start_creates_delivery_program_for_known_type(self):
        service = mock.Mock()
        service.create_program.return_value = SimpleNamespace(
            program_id="prog-1",
            workflow_id="wf-1",
            workflow_type="prd_request",
            entry_phase="prd",
            task_ids=["task-1"],
            first_step="intake",
        )

        with mock.patch.object(
            asgi_app,
            "get_delivery_program_service",
            return_value=service,
        ):
            response = self.client.post(
                "/admin/program-definitions/software_delivery/start",
                json={"summary": "start it", "entry_phase": "prd"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "started")
        self.assertEqual(body["program_type"], "software_delivery")
        self.assertEqual(body["program_id"], "prog-1")
        self.assertEqual(body["workflow_id"], "wf-1")
        service.create_program.assert_called_once()
        self.runtime.audit.append.assert_any_call(
            tenant_id="default",
            action_type="program_definition_started",
            actor_id="admin-1",
            payload=mock.ANY,
        )

    def test_start_returns_503_when_service_unavailable(self):
        with mock.patch.object(
            asgi_app,
            "get_delivery_program_service",
            side_effect=RuntimeError("service boom"),
        ):
            response = self.client.post(
                "/admin/program-definitions/software_delivery/start",
                json={"summary": "start it", "entry_phase": "prd"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("unavailable", response.json()["error"])

    def test_non_object_json_bodies_return_400(self):
        created = self.client.post(
            "/admin/program-definitions/drafts",
            json={"definition": self.definition()},
        ).json()["draft"]

        self.assert_non_object_body_returns_400(
            self.client.post,
            "/admin/program-definitions/drafts",
        )
        self.assert_non_object_body_returns_400(
            self.client.put,
            f"/admin/program-definitions/drafts/{created['id']}",
        )
        self.assert_non_object_body_returns_400(
            self.client.post,
            f"/admin/program-definitions/drafts/{created['id']}/activate",
        )
        self.assert_non_object_body_returns_400(
            self.client.post,
            "/admin/program-definitions/software_delivery/start",
        )

    def test_editor_metadata_round_trips_and_active_excludes_it(self):
        metadata = {
            "nodes": [
                {"id": "start", "position": {"x": 10, "y": 20}},
            ],
        }
        created = self.client.post(
            "/admin/program-definitions/drafts",
            json={
                "definition": self.definition(),
                "editor_metadata": metadata,
            },
        )
        self.assertEqual(created.status_code, 201)
        draft = created.json()["draft"]
        self.assertEqual(draft["editor_metadata"], metadata)

        updated = self.client.put(
            f"/admin/program-definitions/drafts/{draft['id']}",
            json={
                "expected_revision": draft["revision"],
                "definition": self.definition(version="1.0.1"),
            },
        )
        self.assertEqual(updated.status_code, 200)
        updated_draft = updated.json()["draft"]
        self.assertEqual(updated_draft["editor_metadata"], metadata)

        replaced = self.client.put(
            f"/admin/program-definitions/drafts/{draft['id']}",
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
            "get_program_definition_registry",
            return_value=mock.Mock(reload_definitions=mock.Mock(return_value=True)),
        ):
            activated = self.client.post(
                (
                    "/admin/program-definitions/drafts/"
                    f"{draft['id']}/activate"
                ),
                json={"expected_revision": replaced_draft["revision"]},
            )
        self.assertEqual(activated.status_code, 200)
        active_payload = (
            self.store_root / "active" / "managed_program.json"
        ).read_text()
        self.assertNotIn("editor_metadata", active_payload)

    def test_admin_role_required(self):
        self.auth.roles = ["member"]
        self.runtime.policy.evaluate_identity.return_value = mock.Mock(
            decision="deny",
            reason="role denied",
        )

        response = self.client.post("/admin/program-definitions/drafts", json={})

        self.assertEqual(response.status_code, 403)

    def test_draft_routes_registered_before_program_type(self):
        created = self.client.post(
            "/admin/program-definitions/drafts",
            json={"definition": self.definition()},
        )
        self.assertEqual(created.status_code, 201)
        draft_id = created.json()["draft"]["id"]

        response = self.client.get(
            f"/admin/program-definitions/drafts/{draft_id}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["draft"]["id"], draft_id)


if __name__ == "__main__":
    unittest.main()
