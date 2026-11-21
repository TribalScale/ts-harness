from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ts_harness.config.runtime_settings import yaml_settings_baseline
from ts_harness.config.schema import (
    AgentConfig,
    GatewayConfig,
    IdentityConfig,
    RuntimeConfig,
    TenantConfig,
)
from ts_harness.gateway.auth import AuthContext
from ts_harness.gateway.runtime_settings_bff import (
    activate_runtime_settings_draft_admin,
    activate_runtime_settings_page_admin,
    create_runtime_settings_draft_admin,
    get_runtime_settings_admin,
    update_runtime_settings_draft_admin,
    validate_runtime_settings_draft_admin,
)
from ts_harness.config.schema import RoutingConfig, RoutingIntentRule
from ts_harness.workflows.runtime_settings_store import RuntimeSettingsStore
from ts_harness.workflows.routing_definition_store import RoutingDefinitionStore
import ts_harness.workflows.runtime_settings_store as settings_store_mod
import ts_harness.workflows.routing_definition_store as routing_store_mod


def _config() -> RuntimeConfig:
    return RuntimeConfig(
        gateway=GatewayConfig(token="super-secret-token"),
        tenants=[TenantConfig(id="default", display_name="Default")],
        agents=[AgentConfig(id="orchestrator", workspace="/w", model="m", default=True)],
        identity=IdentityConfig(
            slack_user_emails={"U1": "yaml@example.com"},
            jira_account_ids={"old@example.com": "acct-old"},
        ),
        program_defaults={
            "repo_picker": {"repos": [{"id": "org/yaml", "default_branch": "main"}]}
        },
        routing=RoutingConfig(
            intents=[
                RoutingIntentRule(
                    id="governed_prd",
                    signals=["create a prd"],
                    workflow_type="prd_request",
                )
            ]
        ),
    )


def _runtime(config: RuntimeConfig | None = None) -> MagicMock:
    runtime = MagicMock()
    runtime.config = config or _config()
    runtime._yaml_settings_baseline = yaml_settings_baseline(runtime.config)
    runtime._yaml_routing_baseline = runtime.config.routing
    runtime.secret_resolver = MagicMock()
    runtime.secret_resolver._cache = {"k": "super-secret-value"}
    return runtime


def _auth() -> AuthContext:
    return AuthContext(actor_id="admin", tenant_id="default")


class RuntimeSettingsBffTests(unittest.TestCase):
    def tearDown(self):
        settings_store_mod._STORE = None
        routing_store_mod._STORE = None

    def _settings_store(self, tmp: str) -> RuntimeSettingsStore:
        store = RuntimeSettingsStore(Path(tmp) / "settings")
        settings_store_mod._STORE = store
        return store

    def test_get_redacts_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._settings_store(tmp)
            payload = get_runtime_settings_admin(_runtime())
            self.assertNotIn("super-secret-token", payload["redacted_yaml_live"])
            self.assertNotIn("super-secret-value", payload["redacted_yaml_draft"])
            self.assertIn("***", payload["redacted_yaml_live"])

    def test_validate_and_settings_only_activate(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._settings_store(tmp)
            runtime = _runtime()
            created = create_runtime_settings_draft_admin(
                runtime,
                _auth(),
                {
                    "definition": {
                        "definition_type": "runtime_settings",
                        "definition_version": "1.0.0",
                        "identity": {
                            "jira_account_ids": {"ada@example.com": "acct-1"}
                        },
                    }
                },
            )
            draft = created["draft"]
            validated = validate_runtime_settings_draft_admin(runtime, draft["id"])
            self.assertTrue(validated["valid"])
            result = activate_runtime_settings_draft_admin(
                runtime,
                _auth(),
                draft["id"],
                {"expected_revision": draft["revision"]},
            )
            self.assertTrue(result["reload_ok"])
            self.assertEqual(
                runtime.config.identity.jira_account_ids["ada@example.com"],
                "acct-1",
            )
            self.assertEqual(
                runtime.config.identity.slack_user_emails["U1"],
                "yaml@example.com",
            )

    def test_expected_revision_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._settings_store(tmp)
            runtime = _runtime()
            created = create_runtime_settings_draft_admin(
                runtime,
                _auth(),
                {
                    "definition": {
                        "definition_type": "runtime_settings",
                        "definition_version": "1.0.0",
                    }
                },
            )
            with self.assertRaises(Exception):
                update_runtime_settings_draft_admin(
                    runtime,
                    _auth(),
                    created["draft"]["id"],
                    {
                        "definition": created["draft"]["definition"],
                        "expected_revision": "nope",
                    },
                )

    def test_apply_failure_does_not_persist_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._settings_store(tmp)
            runtime = _runtime()
            created = create_runtime_settings_draft_admin(
                runtime,
                _auth(),
                {
                    "definition": {
                        "definition_type": "runtime_settings",
                        "definition_version": "1.0.0",
                        "identity": {
                            "jira_account_ids": {"ada@example.com": "acct-1"}
                        },
                    }
                },
            )
            draft = created["draft"]
            with patch(
                "ts_harness.gateway.runtime_settings_bff.apply_settings_to_config",
                side_effect=RuntimeError("boom"),
            ):
                result = activate_runtime_settings_page_admin(
                    runtime,
                    _auth(),
                    {
                        "runtime_settings_draft_id": draft["id"],
                        "runtime_settings_expected_revision": draft["revision"],
                    },
                )
            self.assertFalse(result["reload_ok"])
            self.assertIsNone(store.get_active("runtime_settings"))

    def test_dual_activate_and_collision_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._settings_store(tmp)
            routing_store = RoutingDefinitionStore(Path(tmp) / "routing")
            routing_store_mod._STORE = routing_store
            runtime = _runtime()
            settings = create_runtime_settings_draft_admin(
                runtime,
                _auth(),
                {
                    "definition": {
                        "definition_type": "runtime_settings",
                        "definition_version": "1.0.0",
                        "program_defaults": {
                            "repo_picker": {
                                "repos": [
                                    {
                                        "id": "org/checkout-service",
                                        "default_branch": "dev",
                                    }
                                ]
                            }
                        },
                    }
                },
            )
            routing = routing_store.create_draft(
                {
                    "definition_type": "channel_routing",
                    "definition_version": "1.0.0",
                    "intents": [
                        {
                            "id": "governed_prd",
                            "signals": ["create a prd"],
                            "delegate_agent": "pm-agent",
                        },
                        {
                            "id": "clone_prd",
                            "signals": ["create a prd"],
                            "delegate_agent": "pm-agent",
                        },
                    ],
                },
                actor_id="admin",
            )
            result = activate_runtime_settings_page_admin(
                runtime,
                _auth(),
                {
                    "runtime_settings_draft_id": settings["draft"]["id"],
                    "runtime_settings_expected_revision": settings["draft"]["revision"],
                    "routing_draft_id": routing["id"],
                    "routing_expected_revision": routing["revision"],
                },
            )
            self.assertTrue(result["reload_ok"])
            self.assertIn("signal_collision", {issue["code"] for issue in result["issues"]})
            self.assertEqual(
                runtime.config.program_defaults["repo_picker"]["repos"][0]["id"],
                "org/checkout-service",
            )
            self.assertEqual(
                [rule.id for rule in runtime.config.routing.intents],
                ["governed_prd", "clone_prd"],
            )


if __name__ == "__main__":
    unittest.main()


class StaleDraftTests(unittest.TestCase):
    """A draft saved before the latest activation is not offered and cannot be activated."""

    def tearDown(self):
        settings_store_mod._STORE = None

    def test_a_draft_older_than_the_active_settings_is_set_aside(self):
        from ts_harness.gateway.runtime_settings_bff import current_draft

        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeSettingsStore(Path(tmp) / "settings")
            settings_store_mod._STORE = store
            runtime = _runtime()
            empty = {"definition_type": "runtime_settings", "definition_version": "1.0.0"}
            stale = create_runtime_settings_draft_admin(runtime, _auth(), {"definition": empty})["draft"]
            newer = create_runtime_settings_draft_admin(runtime, _auth(), {
                "definition": {**empty, "definition_version": "1.0.1",
                               "program_defaults": {"jira_filing": {"allowed_projects": ["PROJ"]}}}})["draft"]
            activate_runtime_settings_draft_admin(runtime, _auth(), newer["id"], {"expected_revision": newer["revision"]})
            self.assertIsNone(current_draft(store))
            payload = get_runtime_settings_admin(runtime)
            self.assertIsNone(payload["draft"])
            self.assertEqual(payload["definition"]["program_defaults"]["jira_filing"]["allowed_projects"], ["PROJ"])
            with self.assertRaisesRegex(ValueError, "saved before the current settings were activated"):
                activate_runtime_settings_draft_admin(runtime, _auth(), stale["id"], {"expected_revision": stale["revision"]})


class ActivateVersionTests(unittest.TestCase):
    """A draft copied from the active settings keeps their version; activating it takes the next one."""

    def tearDown(self):
        settings_store_mod._STORE = None

    def test_activating_a_copy_of_the_active_version_bumps_the_patch(self):
        from ts_harness.gateway.runtime_settings_bff import activate_runtime_settings_page_admin

        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeSettingsStore(Path(tmp) / "settings")
            settings_store_mod._STORE = store
            runtime = _runtime()
            base = {"definition_type": "runtime_settings", "definition_version": "1.0.1",
                    "program_defaults": {"jira_filing": {"allowed_projects": ["OPS"]}}}
            first = create_runtime_settings_draft_admin(runtime, _auth(), {"definition": base})["draft"]
            activate_runtime_settings_draft_admin(runtime, _auth(), first["id"], {"expected_revision": first["revision"]})
            copy = create_runtime_settings_draft_admin(runtime, _auth(), {"definition": {
                **base, "program_defaults": {"jira_filing": {"allowed_projects": ["OPS", "PAY"]}}}})["draft"]
            result = activate_runtime_settings_page_admin(runtime, _auth(), {
                "runtime_settings_draft_id": copy["id"], "runtime_settings_expected_revision": copy["revision"]})
            self.assertTrue(result["reload_ok"])
            active = store.get_active("runtime_settings")
            self.assertEqual(active["definition_version"], "1.0.2")
            self.assertEqual(active["program_defaults"]["jira_filing"]["allowed_projects"], ["OPS", "PAY"])
            # The single-draft route does the same.
            again = create_runtime_settings_draft_admin(runtime, _auth(), {"definition": {
                **base, "definition_version": "1.0.2", "program_defaults": {"jira_filing": {"allowed_projects": ["PAY"]}}}})["draft"]
            activate_runtime_settings_draft_admin(runtime, _auth(), again["id"], {"expected_revision": again["revision"]})
            self.assertEqual(store.get_active("runtime_settings")["definition_version"], "1.0.3")
