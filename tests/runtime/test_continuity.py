"""Tests for program continuity bag + memory snapshots (Phase 3)."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from ts_harness.workflows.continuity import (
    CONTINUITY_INDEX_KEY,
    REDACTED,
    ContinuityRef,
    read_handoff_context,
    redact_sensitive,
    write_stage_snapshot,
)
from ts_harness.workflows.delivery_program import DeliveryProgramService


REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeMemory:
    """Minimal in-memory stand-in for MemoryClient program + memory ops."""

    def __init__(self) -> None:
        self.programs: dict[str, dict] = {}
        self.remembered: list[dict] = []
        self.workflow_payload: dict | None = None
        self.fail_remember = False
        self._counter = 0

    # --- program artifact bag ---
    def create_program(self, program_id: str, artifacts: dict | None = None) -> None:
        self.programs[program_id] = dict(artifacts or {})

    def get_program(self, program_id: str) -> dict:
        artifacts = self.programs.get(program_id, {})
        return {"program": {"metadata": {"artifacts": copy.deepcopy(artifacts)}}}

    def patch_program_artifacts(self, program_id, artifacts, *, merge=True, actor="orchestrator"):
        current = self.programs.setdefault(program_id, {})
        if merge:
            current.update(copy.deepcopy(artifacts))
        else:
            self.programs[program_id] = copy.deepcopy(artifacts)
        return {"ok": True}

    # --- semantic memory ---
    def remember(self, *, tenant_id, agent_id, content, memory_type="semantic", tags=None):
        if self.fail_remember:
            raise RuntimeError("memory-api unavailable")
        self._counter += 1
        mem_id = f"mem-{self._counter}"
        self.remembered.append(
            {"id": mem_id, "tenant_id": tenant_id, "content": content, "tags": list(tags or [])}
        )
        return {"id": mem_id}

    def recall(self, *, tenant_id, agent_id, query, limit=5):
        return [
            {"id": r["id"], "content": r["content"], "tags": r["tags"]}
            for r in self.remembered
        ]

    # --- workflow (for on_workflow_terminal) ---
    def get_workflow(self, workflow_id: str) -> dict:
        return self.workflow_payload or {"workflow": {}, "tasks": []}


class RedactionTests(unittest.TestCase):
    def test_redact_sensitive_masks_credential_keys(self):
        payload = {
            "name": "ok",
            "api_key": "abc",
            "nested": {"token": "xyz", "keep": 1, "PASSWORD": "p"},
            "list": [{"authorization": "Bearer z"}, {"safe": True}],
        }
        redacted = redact_sensitive(payload)
        self.assertEqual(redacted["name"], "ok")
        self.assertEqual(redacted["api_key"], REDACTED)
        self.assertEqual(redacted["nested"]["token"], REDACTED)
        self.assertEqual(redacted["nested"]["keep"], 1)
        self.assertEqual(redacted["nested"]["PASSWORD"], REDACTED)
        self.assertEqual(redacted["list"][0]["authorization"], REDACTED)
        self.assertEqual(redacted["list"][1]["safe"], True)

    def test_secret_redaction_before_persistence(self):
        mem = FakeMemory()
        mem.create_program("prog-1")
        write_stage_snapshot(
            tenant_id="t1",
            workflow_id="wf-1",
            program_id="prog-1",
            stage_name="implement",
            output={"pr_url": "https://x/pr/1", "config": {"secret": "s", "api_key": "k"}},
            memory_client=mem,
        )
        bag = mem.programs["prog-1"]
        self.assertEqual(bag["config"]["secret"], REDACTED)
        self.assertEqual(bag["config"]["api_key"], REDACTED)
        # Persisted memory content must also be redacted.
        content = json.loads(mem.remembered[0]["content"])
        self.assertEqual(content["output"]["config"]["secret"], REDACTED)


class RoundTripTests(unittest.TestCase):
    def test_write_read_version_hash_roundtrip(self):
        mem = FakeMemory()
        mem.create_program("prog-1")
        output = {"approved_prd": {"title": "X"}, "pr_url": "https://x/pr/1"}

        ref1 = write_stage_snapshot(
            tenant_id="t1",
            workflow_id="wf-1",
            program_id="prog-1",
            stage_name="prd",
            output=output,
            memory_client=mem,
        )
        self.assertIsInstance(ref1, ContinuityRef)
        self.assertEqual(ref1.version, 1)
        self.assertTrue(ref1.content_hash)
        self.assertEqual(ref1.memory_id, "mem-1")
        self.assertIn("approved_prd", ref1.artifact_keys)
        self.assertIn("pr_url", ref1.artifact_keys)
        self.assertNotIn(CONTINUITY_INDEX_KEY, ref1.artifact_keys)

        context = read_handoff_context(
            tenant_id="t1",
            refs=[ref1],
            required_keys=["approved_prd", "pr_url"],
            memory_client=mem,
        )
        self.assertEqual(context["approved_prd"], {"title": "X"})
        self.assertEqual(context["pr_url"], "https://x/pr/1")

    def test_idempotent_same_content_same_version(self):
        mem = FakeMemory()
        mem.create_program("prog-1")
        output = {"approved_prd": {"title": "X"}}

        ref1 = write_stage_snapshot(
            tenant_id="t1",
            workflow_id="wf-1",
            program_id="prog-1",
            stage_name="prd",
            output=output,
            memory_client=mem,
        )
        ref2 = write_stage_snapshot(
            tenant_id="t1",
            workflow_id="wf-1",
            program_id="prog-1",
            stage_name="prd",
            output=dict(output),
            memory_client=mem,
        )
        self.assertEqual(ref1.version, ref2.version)
        self.assertEqual(ref1.content_hash, ref2.content_hash)
        # No duplicate success snapshot for the same content.
        self.assertEqual(len(mem.remembered), 1)

    def test_version_increments_on_changed_content(self):
        mem = FakeMemory()
        mem.create_program("prog-1")
        ref1 = write_stage_snapshot(
            tenant_id="t1",
            workflow_id="wf-1",
            program_id="prog-1",
            stage_name="prd",
            output={"approved_prd": {"title": "X"}},
            memory_client=mem,
        )
        ref2 = write_stage_snapshot(
            tenant_id="t1",
            workflow_id="wf-1",
            program_id="prog-1",
            stage_name="prd",
            output={"approved_prd": {"title": "Y"}},
            memory_client=mem,
        )
        self.assertEqual(ref1.version, 1)
        self.assertEqual(ref2.version, 2)
        self.assertEqual(len(mem.remembered), 2)

    def test_session_keyed_snapshot_versioning(self):
        mem = FakeMemory()
        ref1 = write_stage_snapshot(
            tenant_id="t-sess",
            workflow_id="wf-adhoc",
            program_id=None,
            stage_name="delegated_task",
            output={"summary": "first"},
            memory_client=mem,
            session_id="child-session-xyz",
        )
        ref2 = write_stage_snapshot(
            tenant_id="t-sess",
            workflow_id="wf-adhoc",
            program_id=None,
            stage_name="delegated_task",
            output={"summary": "second"},
            memory_client=mem,
            session_id="child-session-xyz",
        )
        self.assertIsNone(ref1.program_id)
        self.assertEqual(ref1.version, 1)
        self.assertEqual(ref2.version, 2)


class TenantScopeTests(unittest.TestCase):
    def test_cross_tenant_ref_rejected_on_read(self):
        mem = FakeMemory()
        foreign_ref = ContinuityRef(
            tenant_id="other-tenant",
            workflow_id="wf-1",
            program_id="prog-1",
            stage_name="prd",
            version=1,
            content_hash="deadbeef",
            artifact_keys=["approved_prd"],
            memory_id="mem-1",
        )
        with self.assertRaises(ValueError):
            read_handoff_context(
                tenant_id="t1",
                refs=[foreign_ref],
                required_keys=["approved_prd"],
                memory_client=mem,
            )


class FailClosedTests(unittest.TestCase):
    def test_memory_write_failure_fails_closed_for_program_path(self):
        mem = FakeMemory()
        mem.create_program("prog-1")
        mem.fail_remember = True
        with self.assertRaises(Exception):
            write_stage_snapshot(
                tenant_id="t1",
                workflow_id="wf-1",
                program_id="prog-1",
                stage_name="prd",
                output={"approved_prd": {"title": "X"}},
                memory_client=mem,
            )
        # Canonical bag must not be mutated when the snapshot fails closed.
        self.assertNotIn("approved_prd", mem.programs["prog-1"])
        self.assertNotIn(CONTINUITY_INDEX_KEY, mem.programs["prog-1"])

    def test_session_path_tolerates_memory_failure(self):
        mem = FakeMemory()
        mem.fail_remember = True
        ref = write_stage_snapshot(
            tenant_id="t1",
            workflow_id="wf-1",
            program_id=None,
            stage_name="delegated_task",
            output={"summary": "ok"},
            memory_client=mem,
            session_id="sess-1",
        )
        self.assertIsNone(ref.memory_id)
        self.assertEqual(ref.version, 1)


class DeliveryProgramRegressionTests(unittest.TestCase):
    def test_on_workflow_terminal_still_patches_required_keys(self):
        mem = FakeMemory()
        mem.create_program("prog-x")
        mem.workflow_payload = {
            "workflow": {
                "id": "wf-prd",
                "workflow_type": "prd_request",
                "program_id": "prog-x",
                "program_phase": "prd",
                "tenant_id": "default",
            },
            "tasks": [
                {
                    "status": "succeeded",
                    "step_name": "draft_prd",
                    "output": {"approved_prd": {"title": "PRD", "summary": "s"}},
                },
            ],
        }
        service = DeliveryProgramService(client=mem)
        result = service.on_workflow_terminal("wf-prd")

        self.assertIsNotNone(result)
        self.assertIn("approved_prd", result["artifacts_patched"])
        # Materialization reads program artifacts; the required key must survive
        # byte-equivalently through the continuity write.
        bag = mem.programs["prog-x"]
        self.assertEqual(bag["approved_prd"], {"title": "PRD", "summary": "s"})
        # A continuity index + memory snapshot were recorded.
        self.assertIn(CONTINUITY_INDEX_KEY, bag)
        self.assertTrue(mem.remembered)

    def test_read_handoff_context_recovers_required_keys(self):
        mem = FakeMemory()
        mem.create_program("prog-x")
        ref = write_stage_snapshot(
            tenant_id="default",
            workflow_id="wf-prd",
            program_id="prog-x",
            stage_name="prd",
            output={"approved_prd": {"title": "PRD"}, "pr_url": "https://x/pr/9"},
            memory_client=mem,
        )
        context = read_handoff_context(
            tenant_id="default",
            refs=[ref.to_dict()],
            required_keys=["approved_prd", "pr_url", "missing_key"],
            memory_client=mem,
        )
        self.assertEqual(context["approved_prd"], {"title": "PRD"})
        self.assertEqual(context["pr_url"], "https://x/pr/9")
        self.assertNotIn("missing_key", context)


class DelegationContinuityTests(unittest.TestCase):
    def test_delegate_returns_continuity_refs(self):
        import json as _json

        from ts_harness.agent.loop import EnterpriseAgent
        from ts_harness.agent.provider import FakeProvider
        from ts_harness.config.schema import load_runtime_config
        from ts_harness.tools.delegation import configure_delegation, handle_delegate_task
        from ts_harness.tools.policy import ToolPolicyContext
        from ts_harness.tools.registry import build_default_registry

        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": _json.dumps(
                                    {"summary": "child summary", "artifact": {"pr_url": "https://x/pr/1"}}
                                ),
                            }
                        }
                    ]
                }
            ]
        )
        agent = EnterpriseAgent(config, build_default_registry(), provider=provider)
        configure_delegation(config, agent)
        ctx = ToolPolicyContext(
            tenant_id="tenant-continuity", agent_id="orchestrator", source="workflow"
        )
        result = handle_delegate_task(
            {"agent_id": "dev-agent", "task": "implement feature"},
            ctx,
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["summary"], "child summary")
        self.assertIn("continuity_refs", result)
        self.assertIsInstance(result["continuity_refs"], list)
        self.assertEqual(len(result["continuity_refs"]), 1)
        ref = result["continuity_refs"][0]
        self.assertEqual(ref["stage_name"], "delegated_task")
        self.assertEqual(ref["tenant_id"], "tenant-continuity")
        self.assertIn("content_hash", ref)
        self.assertIsInstance(result["warnings"], list)
        # Full child output is not dumped into the parent-facing result.
        self.assertNotIn("artifact", result)


if __name__ == "__main__":
    unittest.main()
