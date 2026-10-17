import os
import tempfile
import unittest
from pathlib import Path

from ts_harness.agent.sessions import SessionStore
from ts_harness.audit.writer import AuditWriter
from ts_harness.config.schema import load_runtime_config
from ts_harness.gateway.identity import IdentityConfig, OidcIdentityProvider, build_identity_provider
from ts_harness.policy.engine import PolicyEngine
from ts_harness.sandbox.docker_env import DockerExecutionEnvironment
from ts_harness.secrets.resolver import SecretResolver


REPO_ROOT = Path(__file__).resolve().parents[2]


class EnterpriseHardeningTests(unittest.TestCase):
    def test_tenant_audit_export_isolated(self):
        audit = AuditWriter()
        audit.append(tenant_id="tenant-a", action_type="test", actor_id="u1")
        audit.append(tenant_id="tenant-b", action_type="test", actor_id="u2")
        export_a = audit.export(tenant_id="tenant-a")
        self.assertEqual(len(export_a), 1)
        self.assertEqual(export_a[0]["tenant_id"], "tenant-a")

    def test_audit_writer_persists_append_only_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            audit = AuditWriter(path=path)
            audit.append(tenant_id="tenant-a", action_type="secret_rotation", actor_id="admin")
            audit.append(tenant_id="tenant-a", action_type="tool_call", actor_id="agent")

            reloaded = AuditWriter(path=path)
            export = reloaded.export(tenant_id="tenant-a")

            self.assertEqual(len(export), 2)
            self.assertEqual(path.read_text().count("\n"), 2)
            self.assertEqual(export[0]["sequence"], 1)
            self.assertEqual(export[1]["sequence"], 2)

    def test_secret_values_redacted(self):
        resolver = SecretResolver()
        resolver.resolve("gateway-token", tenant_id="default")
        os.environ["GATEWAY_TOKEN"] = "super-secret-token-value"
        resolver._cache["ts-harness/default/GATEWAY_TOKEN"] = "super-secret-token-value"
        redacted = resolver.redact("leaked super-secret-token-value here")
        self.assertNotIn("super-secret-token-value", redacted)

    def test_secret_resolver_tracks_rotation_metadata_and_uses_provider(self):
        calls = []

        def provider(ref):
            calls.append(ref)
            return "rotated-secret"

        resolver = SecretResolver(namespace="ts-harness", provider=provider)
        value = resolver.resolve("gateway-token", tenant_id="tenant-a")
        resolver.mark_rotated("gateway-token", tenant_id="tenant-a", version="v2")

        self.assertEqual(value, "rotated-secret")
        self.assertEqual(calls, ["ts-harness/tenant-a/gateway-token"])
        health = resolver.health()
        self.assertEqual(health["rotations"]["ts-harness/tenant-a/gateway-token"]["version"], "v2")

    def test_budget_hard_stop(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        engine = PolicyEngine(config)
        engine.evaluate_budget(tenant_id="default", cost_usd=60)
        result = engine.evaluate_budget(tenant_id="default", cost_usd=50)
        self.assertEqual(result.decision, "deny")

    def test_secret_reload_clears_cache_and_refetches(self):
        calls = []

        def provider(ref):
            calls.append(ref)
            return "rotated-secret"

        resolver = SecretResolver(namespace="ts-harness", provider=provider)
        value = resolver.resolve("gateway-token", tenant_id="tenant-a")
        resolver.mark_rotated("gateway-token", tenant_id="tenant-a", version="v2")
        resolver.reload("gateway-token", tenant_id="tenant-a")
        value2 = resolver.resolve("gateway-token", tenant_id="tenant-a")

        self.assertEqual(value, "rotated-secret")
        self.assertEqual(value2, "rotated-secret")
        self.assertEqual(len(calls), 2)

    def test_rbac_denies_missing_role(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        engine = PolicyEngine(config)
        result = engine.evaluate_identity({"roles": ["member"]}, ["admin"])
        self.assertEqual(result.decision, "deny")

    def test_audit_export_filters_by_time_window(self):
        audit = AuditWriter()
        audit.append(tenant_id="tenant-a", action_type="early", actor_id="u1")
        audit._events[0].timestamp = "2026-01-01T00:00:00Z"
        audit.append(tenant_id="tenant-a", action_type="late", actor_id="u1")
        audit._events[1].timestamp = "2026-06-01T00:00:00Z"
        export = audit.export(tenant_id="tenant-a", since="2026-02-01T00:00:00Z")
        self.assertEqual(len(export), 1)
        self.assertEqual(export[0]["action_type"], "late")

    def test_session_store_isolates_tenants(self):
        store = SessionStore()
        store.get_or_create(tenant_id="tenant-a", source="workflow", agent_id="orchestrator")
        store.get_or_create(tenant_id="tenant-b", source="workflow", agent_id="orchestrator")
        self.assertEqual(len(store.list_for_tenant("tenant-a")), 1)
        self.assertEqual(store.list_for_tenant("tenant-a")[0].tenant_id, "tenant-a")

    def test_sandbox_docker_command_includes_tenant_label(self):
        captured: dict[str, list[str]] = {}

        def runner(cmd, **kwargs):
            captured["cmd"] = cmd

            class Result:
                returncode = 0
                stdout = ""
                stderr = ""

            return Result()

        env = DockerExecutionEnvironment(enabled=True, runner=runner)
        env.run("echo ok", workspace="/workspace/dev-agent", tenant_id="tenant-b")
        self.assertIn("ts-harness.tenant=tenant-b", captured["cmd"])

    def test_oidc_identity_provider_rejects_non_jwt_token(self):
        """OidcIdentityProvider now performs real JWT validation, not stub checks."""
        provider = build_identity_provider(IdentityConfig(provider="oidc", issuer="https://issuer.example"))
        self.assertIsInstance(provider, OidcIdentityProvider)
        with self.assertRaises((ValueError, Exception)):
            provider.validate_token("valid-token-12345678")


if __name__ == "__main__":
    unittest.main()
