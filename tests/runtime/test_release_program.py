"""Release on the program graph: environments, promote gates, triggers, deployment waits, rollback."""

from __future__ import annotations

import importlib.util
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

from ts_harness.config.runtime_settings import _canonicalize_program_defaults
from ts_harness.gateway.github_webhook import parse_github_event, wait_matches_event
from ts_harness.integrations.github_client import GitHubError
from ts_harness.workflows import release
from ts_harness.workflows.hooks.wait_external import _block_when_match
from ts_harness.workflows.program_adapters import build_default_adapter_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40
OLD_SHA = "b" * 40
OLDER_SHA = "c" * 40
PR_URL = "https://github.com/Acme/Shop/pull/7"


def validate_program_defaults(raw: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    issues: list[dict[str, str]] = []
    return _canonicalize_program_defaults(raw, path="$.program_defaults", issues=issues), issues


class FakeMemory:
    def __init__(self, tasks: list[dict[str, Any]], *, program_id: str | None = "prog-1", releases: dict | None = None):
        self.tasks = tasks
        self.workflow = {"id": "wf-1", "program_id": program_id, "tenant_id": "acme"}
        self.releases = releases or {}
        self.decisions: list[dict[str, Any]] = []
        self.patched: dict[str, dict[str, Any]] = {}
        self.recorded: list[dict[str, Any]] = []
        self.refs: list[dict[str, Any]] = []
        self.last_success_calls: list[dict[str, Any]] = []

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        return {"workflow": self.workflow, "tasks": self.tasks}

    def patch_task_input(self, task_id: str, input_data: dict[str, Any], *, actor: str = "") -> dict[str, Any]:
        self.patched[task_id] = input_data
        return {"status": "ok"}

    def last_successful_release(self, *, repo, environment, tenant_id="default", exclude_sha=None):
        self.last_success_calls.append({"repo": repo, "environment": environment, "exclude_sha": exclude_sha})
        for row in self.releases.get(environment, []):
            if row["sha"] != exclude_sha:
                return {"release": row}
        return {"release": None}

    def list_decisions(self, *, workflow_id=None, kind=None, program_id=None):
        return {"decisions": [d for d in self.decisions if d.get("kind") == kind]}

    def record_release(self, program_id: str, **fields: Any) -> dict[str, Any]:
        self.recorded.append({"program_id": program_id, **fields})
        return {"id": fields.get("id") or "rel-1", "status": fields.get("status")}

    def record_program_external_ref(self, program_id: str, **fields: Any) -> dict[str, Any]:
        self.refs.append({"program_id": program_id, **fields})
        return {"status": "ok"}


def _task(step: str, *, status: str = "pending", output: dict | None = None, input: dict | None = None) -> dict[str, Any]:
    return {
        "id": f"t-{step}",
        "workflow_id": "wf-1",
        "tenant_id": "acme",
        "step_name": step,
        "status": status,
        "output": output or {},
        "input": input or {},
    }


def _plan_tasks(intake: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _task("plan_release", status="running", input=intake),
        _task("approve_promote_staging", input={"_approval_policy": {"decision_kind": "promote"}}),
        _task("approve_promote_production", input={"_approval_policy": {"required_roles": ["release_manager"]}}),
    ]


PLAN_CONFIG = {"hook": "plan_release", "gates": {"staging": "approve_promote_staging", "production": "approve_promote_production"}}

CONFIGURED = {
    "release": {
        "environments": {
            "acme/shop": [
                {"name": "uat", "trigger": {"kind": "workflow_dispatch", "workflow": "deploy.yml", "ref_input": "sha"}, "required_roles": ["qa_lead"], "requires": None},
                {"name": "prod", "trigger": {"kind": "deployment"}, "required_roles": ["cab"], "requires": "uat"},
            ]
        }
    }
}


class EnvironmentTests(unittest.TestCase):
    def test_defaults_are_staging_then_production(self):
        envs = release.environments_for("acme/other", CONFIGURED)
        self.assertEqual([env["name"] for env in envs], ["staging", "production"])
        self.assertEqual(envs[1]["requires"], "staging")

    def test_configured_repo_lookup_is_case_insensitive(self):
        self.assertEqual([env["name"] for env in release.environments_for("Acme/Shop", CONFIGURED)], ["uat", "prod"])

    def test_settings_require_two_ordered_environments(self):
        _, issues = validate_program_defaults(
            {"release": {"environments": {"acme/shop": [{"name": "prod"}]}}}
        )
        self.assertTrue(any("two environments" in issue["message"] for issue in issues))
        _, issues = validate_program_defaults(
            {"release": {"environments": {"acme/shop": [{"name": "prod", "requires": "uat"}, {"name": "uat"}]}}}
        )
        self.assertTrue(any("earlier environment" in issue["message"] for issue in issues))

    def test_settings_canonicalize_workflow_dispatch(self):
        defaults, issues = validate_program_defaults(
            {
                "release": {
                    "environments": {
                        "Acme/Shop": [
                            {"name": "UAT", "trigger": {"kind": "workflow_dispatch", "workflow": "deploy.yml"}},
                            {"name": "prod", "requires": "uat", "required_roles": ["CAB"]},
                        ]
                    }
                }
            }
        )
        self.assertEqual(issues, [])
        envs = defaults["release"]["environments"]["acme/shop"]
        self.assertEqual(envs[0]["trigger"], {"kind": "workflow_dispatch", "workflow": "deploy.yml", "ref_input": "sha"})
        self.assertEqual(envs[1]["required_roles"], ["cab"])

    def test_environment_required_permission_is_copied_onto_the_promote_gate(self):
        defaults, issues = validate_program_defaults(
            {
                "release": {
                    "environments": {
                        "acme/shop": [
                            {"name": "staging", "required_permission": "approve_promote_staging"},
                            {"name": "prod", "requires": "staging", "required_permission": "Approve_Promote_Production"},
                        ]
                    }
                }
            }
        )
        self.assertEqual(issues, [])
        self.assertEqual(defaults["release"]["environments"]["acme/shop"][1]["required_permission"], "approve_promote_production")

        class _Memory:
            def __init__(self):
                self.patched = None

            def patch_task_input(self, task_id, data, actor):
                del task_id, actor
                self.patched = data

        memory = _Memory()
        release._patch_gate_policy(
            memory,
            {"id": "g", "input": {"_approval_policy": {"required_roles": ["approver"]}}},
            slot={
                "required_roles": [],
                "required_permission": "approve_promote_production",
                "environment": "prod",
                "rollback_sha": None,
            },
            repo="acme/shop",
            sha="a" * 40,
            actor="tester",
        )
        self.assertEqual(
            memory.patched["_approval_policy"]["required_permission"],
            "approve_promote_production",
        )


class PlanReleaseTests(unittest.TestCase):
    def test_plan_pins_sha_environments_and_gate_policies(self):
        memory = FakeMemory(
            _plan_tasks({"merge_sha": SHA, "pr_urls": [PR_URL], "base_branch": "dev"}),
            releases={"prod": [{"sha": OLD_SHA}]},
        )
        result = release.run_system_step("plan_release", memory.tasks[0], PLAN_CONFIG, memory=memory)
        self.assertEqual(result["status"], "succeeded", result)
        with mock.patch.object(release, "_live_program_defaults", return_value=CONFIGURED):
            output = release.plan_release(memory.tasks[0], PLAN_CONFIG, memory=memory)
        self.assertEqual(output["repo"], "acme/shop")
        self.assertEqual(output["sha"], SHA)
        self.assertEqual(output["environments"], ["uat", "prod"])
        self.assertEqual(output["slots"]["production"]["rollback_sha"], OLD_SHA)
        self.assertIsNone(output["slots"]["staging"]["rollback_sha"])
        self.assertIn({"repo": "acme/shop", "environment": "prod", "exclude_sha": SHA}, memory.last_success_calls)

        prod_policy = memory.patched["t-approve_promote_production"]["_approval_policy"]
        self.assertEqual(prod_policy["required_roles"], ["cab", "release_manager"])
        self.assertEqual(prod_policy["decision_kind"], "promote")
        self.assertTrue(prod_policy["require_dependencies_succeeded"])
        self.assertEqual(prod_policy["details"]["environment"], "prod")
        self.assertEqual(prod_policy["details"]["sha"], SHA)
        staging_policy = memory.patched["t-approve_promote_staging"]["_approval_policy"]
        self.assertEqual(staging_policy["required_roles"], ["qa_lead"])

    def test_plan_without_merge_sha_blocks(self):
        memory = FakeMemory(_plan_tasks({"pr_urls": [PR_URL]}))
        result = release.run_system_step("plan_release", memory.tasks[0], PLAN_CONFIG, memory=memory)
        self.assertEqual(result, {"status": "blocked", "reason": "release needs merge_sha"})

    def test_plan_without_repo_blocks(self):
        memory = FakeMemory(_plan_tasks({"merge_sha": SHA}))
        result = release.run_system_step("plan_release", memory.tasks[0], PLAN_CONFIG, memory=memory)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("repo_url", result["reason"])

    def test_rollback_targets_release_before_current(self):
        tasks = [
            _task("plan_release", status="running", input={"repo_url": "https://github.com/acme/shop", "rollback_environment": "production"}),
            _task("approve_rollback"),
        ]
        memory = FakeMemory(tasks, releases={"production": [{"sha": OLD_SHA}, {"sha": OLDER_SHA}]})
        output = release.plan_release(
            tasks[0], {"mode": "rollback", "gates": {"target": "approve_rollback"}}, memory=memory, program_defaults={}
        )
        self.assertEqual(output["sha"], OLDER_SHA)
        self.assertEqual(output["slots"]["target"]["environment"], "production")
        self.assertEqual(output["slots"]["target"]["rollback_sha"], OLD_SHA)
        self.assertEqual(memory.patched["t-approve_rollback"]["_approval_policy"]["details"]["sha"], OLDER_SHA)

    def test_plan_refuses_repo_outside_tenant_binding(self):
        tasks = [
            _task("plan_release", status="running", input={"repo_url": "https://github.com/evil/app", "merge_sha": SHA}),
            _task("approve_promote_staging"),
        ]
        memory = FakeMemory(tasks)
        with self.assertRaisesRegex(release.ReleaseBlocked, "outside the tenant GitHub org acme"):
            release.plan_release(tasks[0], PLAN_CONFIG, memory=memory, program_defaults={"github": {"org": "acme"}})
        self.assertEqual(memory.patched, {})

    def test_rollback_without_history_blocks(self):
        tasks = [
            _task("plan_release", status="running", input={"repo_url": "acme/shop", "rollback_environment": "production"}),
            _task("approve_rollback"),
        ]
        memory = FakeMemory(tasks, releases={"production": [{"sha": OLD_SHA}]})
        with self.assertRaisesRegex(release.ReleaseBlocked, "no earlier successful release"):
            release.plan_release(tasks[0], {"mode": "rollback", "gates": {"target": "approve_rollback"}}, memory=memory, program_defaults={})


def _plan_output(trigger: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": "promote",
        "repo": "acme/shop",
        "sha": SHA,
        "base_branch": "dev",
        "slots": {
            "staging": {"environment": "uat", "trigger": trigger, "required_roles": [], "rollback_sha": OLD_SHA},
        },
    }


TRIGGER_CONFIG = {"hook": "trigger_deploy", "slot": "staging", "gate": "approve_promote_staging"}


def _trigger_memory(trigger: dict[str, Any], *, approved: bool = True) -> FakeMemory:
    tasks = [
        _task("plan_release", status="succeeded", output=_plan_output(trigger)),
        _task("approve_promote_staging", status="succeeded"),
        _task("trigger_deploy_staging", status="running"),
    ]
    memory = FakeMemory(tasks)
    if approved:
        memory.decisions.append(
            {"id": str(uuid.uuid4()), "kind": "promote", "decision": "approved", "task_id": "t-approve_promote_staging", "actor_email": "cab@acme.com"}
        )
    return memory


class TriggerDeployTests(unittest.TestCase):
    def test_deployment_trigger_creates_deployment_for_sha(self):
        memory = _trigger_memory({"kind": "deployment"})
        github = mock.MagicMock()
        github.create_deployment.return_value = {"id": 991}
        output = release.trigger_deploy(memory.tasks[2], TRIGGER_CONFIG, memory=memory, github=github)
        kwargs = github.create_deployment.call_args.kwargs
        self.assertEqual(github.create_deployment.call_args.args, ("acme", "shop"))
        self.assertEqual((kwargs["ref"], kwargs["environment"]), (SHA, "uat"))
        self.assertEqual(kwargs["payload"]["release_id"], "rel-1")
        self.assertEqual(output["deployment_id"], 991)
        self.assertEqual(output["promoted_by"], "cab@acme.com")
        first = memory.recorded[0]
        self.assertEqual((first["status"], first["environment"], first["sha"], first["rollback_sha"]), ("in_progress", "uat", SHA, OLD_SHA))
        self.assertEqual(memory.recorded[1]["deployment_ref"], "991")
        self.assertEqual(memory.refs[0]["kind"], "deployment")
        self.assertEqual(memory.refs[0]["ref"], f"acme/shop@{SHA}:uat")

    def test_workflow_dispatch_trigger_passes_sha_input_on_base_branch(self):
        memory = _trigger_memory({"kind": "workflow_dispatch", "workflow": "deploy.yml", "ref_input": "release_sha"})
        github = mock.MagicMock()
        output = release.trigger_deploy(memory.tasks[2], TRIGGER_CONFIG, memory=memory, github=github)
        github.dispatch_workflow.assert_called_once_with(
            "acme", "shop", "deploy.yml", ref="dev", inputs={"release_sha": SHA, "environment": "uat"}
        )
        github.create_deployment.assert_not_called()
        self.assertEqual(output["deployment_ref"], "workflow_dispatch:deploy.yml@dev")

    def test_missing_promote_decision_blocks_without_triggering(self):
        memory = _trigger_memory({"kind": "deployment"}, approved=False)
        github = mock.MagicMock()
        with self.assertRaisesRegex(release.ReleaseBlocked, "no approved promote decision"):
            release.trigger_deploy(memory.tasks[2], TRIGGER_CONFIG, memory=memory, github=github)
        github.create_deployment.assert_not_called()
        self.assertEqual(memory.recorded, [])

    def test_github_error_marks_release_failed(self):
        memory = _trigger_memory({"kind": "deployment"})
        github = mock.MagicMock()
        github.create_deployment.side_effect = GitHubError(403, "forbidden")
        with self.assertRaisesRegex(release.ReleaseBlocked, "deploy trigger for uat failed"):
            release.trigger_deploy(memory.tasks[2], TRIGGER_CONFIG, memory=memory, github=github)
        self.assertEqual(memory.recorded[-1], {"program_id": "prog-1", "id": "rel-1", "status": "failed", "actor": "workflow-runner"})

    def test_release_requires_program(self):
        memory = _trigger_memory({"kind": "deployment"})
        memory.workflow["program_id"] = None
        result = release.run_system_step("trigger_deploy", memory.tasks[2], TRIGGER_CONFIG, memory=memory)
        self.assertEqual(result["status"], "blocked")


TRIGGERED = {"release_id": "rel-1", "repo": "acme/shop", "sha": SHA, "environment": "uat", "deployment_id": 991, "rollback_sha": OLD_SHA}
RECORD_CONFIG = {"hook": "record_release", "trigger_step": "trigger_deploy_staging", "wait_step": "await_deployment_staging"}


class RecordReleaseTests(unittest.TestCase):
    def _memory(self, state: str) -> FakeMemory:
        return FakeMemory(
            [
                _task("trigger_deploy_staging", status="succeeded", output=TRIGGERED),
                _task("await_deployment_staging", status="succeeded", output={"state": state, "target_url": "https://ci/run/1", "deployment_id": 991}),
                _task("record_release_staging", status="running"),
            ]
        )

    def test_success_records_evidence(self):
        memory = self._memory("success")
        output = release.record_release(memory.tasks[2], RECORD_CONFIG, memory=memory)
        self.assertEqual(output["status"], "succeeded")
        self.assertEqual(memory.recorded[0]["status"], "succeeded")
        self.assertEqual(memory.recorded[0]["evidence_url"], "https://ci/run/1")
        self.assertEqual(memory.refs[0]["state"], "succeeded")

    def test_failure_records_and_blocks_with_rollback_guidance(self):
        memory = self._memory("failure")
        result = release.run_system_step("record_release", memory.tasks[2], RECORD_CONFIG, memory=memory)
        self.assertEqual(result["status"], "blocked")
        self.assertIn(f"roll back to {OLD_SHA}", result["reason"])
        self.assertEqual(memory.recorded[0]["status"], "failed")


class DeploymentWaitTests(unittest.TestCase):
    def _memory(self) -> FakeMemory:
        return FakeMemory(
            [
                _task("trigger_deploy_staging", status="succeeded", output=TRIGGERED),
                _task("await_deployment_staging", status="blocked"),
            ]
        )

    def test_wait_pins_sha_and_environment_and_matches_webhook(self):
        memory = self._memory()
        github = mock.MagicMock()
        github.deployment_statuses.return_value = [{"id": 1, "state": "in_progress"}]
        prepared = release.prepare_deployment_wait(
            memory.tasks[1], {"deployment": {"trigger_step": "trigger_deploy_staging"}}, memory=memory, github=github
        )
        self.assertNotIn("ready", prepared)
        pinned = memory.patched["t-await_deployment_staging"]
        self.assertEqual(pinned["_wait_match"], {"sha": SHA, "environment": "uat"})
        wait = {"match": pinned["_wait_match"], "options": pinned["_wait_options"]}

        def event(sha: str, environment: str) -> dict[str, Any]:
            return parse_github_event(
                {
                    "deployment_status": {"state": "success", "environment": environment},
                    "deployment": {"id": 991, "sha": sha, "environment": environment},
                    "repository": {"full_name": "Acme/Shop"},
                },
                "deployment_status",
            )

        self.assertTrue(wait_matches_event(wait, event(SHA, "uat")))
        self.assertFalse(wait_matches_event(wait, event(SHA, "prod")))
        self.assertFalse(wait_matches_event(wait, event(OLD_SHA, "uat")))

    def test_already_terminal_deployment_is_ready(self):
        memory = self._memory()
        github = mock.MagicMock()
        github.deployment_statuses.return_value = [
            {"id": 1, "state": "in_progress"},
            {"id": 2, "state": "success", "target_url": "https://ci/run/2"},
        ]
        prepared = release.prepare_deployment_wait(
            memory.tasks[1], {"deployment": {"trigger_step": "trigger_deploy_staging"}}, memory=memory, github=github
        )
        self.assertEqual(prepared["ready"]["state"], "success")
        self.assertEqual(prepared["ready"]["target_url"], "https://ci/run/2")

    def test_workflow_dispatch_wait_finds_deployment_by_sha(self):
        github = mock.MagicMock()
        github.list_deployments.return_value = [{"id": 5}, {"id": 9}]
        github.deployment_statuses.return_value = [{"id": 3, "state": "failure"}]
        state = release.deployment_state(github, repo="acme/shop", sha=SHA, environment="uat")
        github.list_deployments.assert_called_once_with("acme", "shop", sha=SHA, environment="uat")
        github.deployment_statuses.assert_called_once_with("acme", "shop", 9)
        self.assertEqual(state["state"], "failure")


class ShaChecksWaitTests(unittest.TestCase):
    def test_ci_wait_pins_merged_sha_and_blocks_on_failure(self):
        tasks = [
            _task("plan_release", status="succeeded", output={"sha": SHA, "repo": "acme/shop"}),
            _task("await_ci_on_sha", status="blocked"),
        ]
        memory = FakeMemory(tasks)
        github = mock.MagicMock()
        github.check_summary.return_value = {"state": "failure", "checks": {"ci": "failure"}, "failed": ["ci"], "pending": []}
        prepared = release.prepare_sha_checks_wait(
            tasks[1],
            {"sha_checks": {"sha": "$prior.plan_release.output.sha", "repo": "$prior.plan_release.output.repo"}},
            memory=memory,
            github=github,
            program_defaults={"merge_readiness": {"required_checks": {"acme/shop": ["ci"]}}},
        )
        self.assertEqual(memory.patched["t-await_ci_on_sha"]["_wait_match"], {"head_sha": SHA})
        github.check_summary.assert_called_once_with("acme", "shop", SHA, required=["ci"])
        self.assertEqual(prepared["ready"]["checks_state"], "failure")
        self.assertEqual(prepared["ready"]["failed_checks"], ["acme/shop:ci"])
        self.assertEqual(_block_when_match({"checks_state": ["failure"]}, prepared["ready"]), "checks_state=failure")
        self.assertEqual(_block_when_match({"checks_state": ["failure"]}, {"checks_state": "success"}), "")


class AdapterTests(unittest.TestCase):
    def test_deploy_inputs_carry_release_artifacts(self):
        registry = build_default_adapter_registry()
        intake = registry.build_inputs(
            "extract_deploy_outputs",
            summary="ship",
            artifacts={"merge_sha": SHA, "pr_urls": [PR_URL], "default_branch": "dev", "repo_url": "https://github.com/acme/shop"},
        )
        self.assertEqual(intake["merge_sha"], SHA)
        self.assertEqual(intake["pr_urls"], [PR_URL])
        self.assertEqual(intake["base_branch"], "dev")

    def test_implement_outputs_include_merge_sha(self):
        registry = build_default_adapter_registry()
        patch = registry.extract_outputs(
            "extract_implement_outputs",
            workflow_type="implement_delivery",
            tasks=[{"status": "succeeded", "output": {"pr_urls": [PR_URL], "merge_sha": SHA}}],
            artifacts={},
        )
        self.assertEqual(patch["merge_sha"], SHA)


def _software_delivery_snapshot() -> dict[str, Any]:
    from ts_harness.repo_paths import ensure_scripts_importable, programs_dir

    ensure_scripts_importable()
    import program_definitions

    return program_definitions.program_to_payload(
        program_definitions.load_program(programs_dir() / "software_delivery.json")
    )


class ImplementToReleaseTests(unittest.TestCase):
    def _ctx(self, client: mock.MagicMock):
        from ts_harness.workflows.materialize import load_skeleton
        from ts_harness.workflows.registry import HookContext

        skeleton = load_skeleton("implement_work_orders")
        return HookContext(
            workflow_id="wf-impl",
            workflow_type="implement_work_orders",
            tenant_id="acme",
            task={"id": "t-final"},
            stage=skeleton.stages[-1],
            skeleton=skeleton,
            actor="workflow-runner",
            memory_client=client,
            extras={},
        )

    def _client(self, children: list[dict[str, Any]]) -> mock.MagicMock:
        client = mock.MagicMock()
        client.get_workflow.return_value = {
            "workflow": {"id": "wf-impl", "program_id": "prog-root", "program_phase": "implement", "summary": "ship"},
            "tasks": [{"status": "succeeded", "output": {"work_order_id": "WO-1", "merge_sha": SHA}}],
        }
        client.get_program.return_value = {
            "program": {
                "id": "prog-root",
                "program_definition_snapshot": _software_delivery_snapshot(),
                "metadata": {"artifacts": {"work_order_ids": ["WO-1"], "completed_work_order_ids": ["WO-1"], "merge_sha": SHA}},
            }
        }
        client.get_program_status.return_value = {"children": children}
        return client

    def _run(self, client: mock.MagicMock):
        from ts_harness.workflows.hooks import complete_program_phase

        service = mock.MagicMock()
        service.on_workflow_terminal.return_value = {}
        service.advance_program.return_value = {"workflow_id": "wf-release"}
        with mock.patch.object(complete_program_phase, "get_delivery_program_service", return_value=service), mock.patch(
            "ts_harness.workflows.hooks.notify_bound_channel.handle", return_value={"status": "notified"}
        ), mock.patch.object(complete_program_phase, "publish_program_brief"):
            return complete_program_phase.handle(self._ctx(client)), service

    def test_delivered_work_orders_advance_to_release(self):
        result, service = self._run(self._client([{"id": "child-1", "status": "succeeded"}]))
        self.assertEqual(result["status"], "phase_completed_auto_advanced")
        self.assertEqual(service.advance_program.call_args.kwargs["target_phase"], "release")

    def test_release_waits_for_unfinished_child_programs(self):
        client = self._client([{"id": "child-1", "status": "succeeded"}, {"id": "child-2", "status": "blocked"}])
        result, service = self._run(client)
        self.assertEqual(result["status"], "phase_waiting_on_children")
        self.assertEqual(result["waiting_on"], ["child-2"])
        service.advance_program.assert_not_called()
        pending = [
            call.args[1]["pending_auto_advance"]
            for call in client.patch_program_artifacts.call_args_list
            if "pending_auto_advance" in call.args[1]
        ]
        self.assertEqual(pending[0]["target"], "release")

    def test_last_child_finishing_runs_parent_release(self):
        from ts_harness.workflows.hooks import complete_program_phase

        client = mock.MagicMock()
        client.get_program.return_value = {
            "program": {
                "id": "prog-root",
                "tenant_id": "acme",
                "metadata": {"artifacts": {"pending_auto_advance": {"target": "release", "summary": "ship"}}},
            }
        }
        client.get_program_status.return_value = {"children": [{"id": "child-2", "status": "running"}]}
        service = mock.MagicMock()
        ctx = self._ctx(client)
        complete_program_phase._resume_parent_pending_advance(
            ctx, service, {"id": "child-2", "parent_program_id": "prog-root"}
        )
        self.assertEqual(service.advance_program.call_args.kwargs["program_id"], "prog-root")
        self.assertEqual(service.advance_program.call_args.kwargs["target_phase"], "release")

        client.get_program_status.return_value = {"children": [{"id": "child-3", "status": "running"}]}
        service.reset_mock()
        complete_program_phase._resume_parent_pending_advance(
            ctx, service, {"id": "child-2", "parent_program_id": "prog-root"}
        )
        service.advance_program.assert_not_called()


def load_gateway():
    spec = importlib.util.spec_from_file_location("memory_gateway_release", REPO_ROOT / "scripts" / "memory-gateway.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Cursor:
    def __init__(self, routes: list[tuple[str, Any]]):
        self.routes = routes
        self.calls: list[tuple[str, Any]] = []
        self._last: Any = None
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self._last = next((result for fragment, result in self.routes if fragment in sql), None)

    def fetchone(self):
        return self._last[0] if isinstance(self._last, list) else self._last

    def fetchall(self):
        return self._last if isinstance(self._last, list) else ([self._last] if self._last else [])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        pass


class PromoteGateTests(unittest.TestCase):
    def setUp(self):
        self.gateway = load_gateway()
        self.dep = str(uuid.uuid4())

    def _gate(self, policy: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "pending",
            "approval_required": True,
            "step_name": "approve_promote_production",
            "input": {"_approval_policy": policy},
            "workflow_id": str(uuid.uuid4()),
            "tenant_id": "acme",
            "depends_on": [self.dep],
            "expires_at": None,
        }

    def _approve(self, policy: dict[str, Any], dep_status: str, roles: list[str]):
        cursor = Cursor(
            [
                ("FROM workflow_tasks\n            WHERE id", self._gate(policy)),
                ("SELECT step_name, status FROM workflow_tasks", [{"step_name": "record_release_staging", "status": dep_status}]),
            ]
        )
        body = {"id": str(uuid.uuid4()), "actor": "slack:U1", "actor_roles": roles}
        return self.gateway.approve_workflow_task(Conn(cursor), body, True), cursor

    def test_promote_before_staging_recorded_is_refused(self):
        policy = {"decision_kind": "promote", "require_dependencies_succeeded": True}
        with self.assertRaisesRegex(ValueError, "waiting on: record_release_staging"):
            self._approve(policy, "pending", [])

    def test_role_without_environment_role_is_denied(self):
        policy = {"decision_kind": "promote", "require_dependencies_succeeded": True, "required_roles": ["cab"]}
        with self.assertRaisesRegex(ValueError, "requires one of roles: cab"):
            self._approve(policy, "succeeded", ["developer"])

    def test_approved_promote_records_promote_decision_with_details(self):
        policy = {
            "decision_kind": "promote",
            "require_dependencies_succeeded": True,
            "required_roles": ["cab"],
            "details": {"environment": "prod", "sha": SHA},
        }
        result, cursor = self._approve(policy, "succeeded", ["cab"])
        self.assertEqual(result["status"], "succeeded")
        decisions = [params for sql, params in cursor.calls if "INSERT INTO workflow_decisions" in sql]
        self.assertEqual(decisions[-1][5:7], ("promote", "approved"))
        self.assertIn("prod", str(decisions[-1]))


if __name__ == "__main__":
    unittest.main()
