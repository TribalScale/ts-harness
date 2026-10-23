# flake8: noqa
import importlib.util
import io
import json
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "scripts" / "workflow-runner.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("workflow_runner", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeMemoryClient:
    def __init__(self, ready=None):
        self.ready = ready or []
        self.claims = []
        self.completed = []
        self.failed = []
        self.blocked = []
        self.blocked_outputs = []
        self.runtime_records = []
        self.plan_proposals = []
        self.retried = []
        self.stale = []
        self.waits = []
        self.created_workflows = []

    def list_stale_tasks(self):
        return self.stale

    def list_ready_tasks(self, limit):
        return self.ready[:limit]

    def claim_task(self, task, locked_by, lease_seconds):
        self.claims.append((task, locked_by, lease_seconds))
        return True

    def record_runtime_task(self, task_id, runtime_task_id):
        self.runtime_records.append((task_id, runtime_task_id))

    def complete_task(self, task_id, output):
        self.completed.append((task_id, output))

    def fail_task(self, task_id, error):
        self.failed.append((task_id, error))

    def block_task(self, task_id, error, output=None):
        self.blocked.append((task_id, error))
        self.blocked_outputs.append((task_id, error, output))

    def set_task_wait(self, task_id, wait_token, expected_event_type, **deadlines):
        self.waits.append((task_id, wait_token, expected_event_type))

    def propose_plan(self, workflow_id, parent_task_id, proposed_tasks):
        self.plan_proposals.append((workflow_id, parent_task_id, proposed_tasks))
        return {"plan_id": "plan-1"}

    def acquire_leader(self, runner_id, ttl_seconds):
        return True

    def create_workflow(self, workflow_id, workflow_type, tenant_id, definition_version=None):
        self.created_workflows.append(
            (workflow_id, workflow_type, tenant_id, definition_version)
        )
        self._workflow_records = getattr(self, "_workflow_records", {})
        self._workflow_records[workflow_id] = {
            "workflow": {
                "id": workflow_id,
                "workflow_type": workflow_type,
                "definition_version": definition_version,
            },
            "tasks": [],
        }
        return {"id": workflow_id, "workflow_type": workflow_type}

    def get_workflow_record(self, workflow_id):
        records = getattr(self, "_workflow_records", {})
        if workflow_id in records:
            return records[workflow_id]
        return {"workflow": {"id": workflow_id}, "tasks": []}

    def retry_task(self, task_id, reason):
        self.retried.append((task_id, reason))


class FakeAdapter:
    def __init__(self, result):
        self.result = result
        self.payloads = []

    def dispatch(self, payload):
        self.payloads.append(payload)
        return self.result


class WorkflowRunnerTests(unittest.TestCase):
    def setUp(self):
        # Keep unit tests off the real hook engine unless a test opts in.
        self._hook_engine_patch = patch.dict(
            "os.environ",
            {"TS_HARNESS_HOOK_ENGINE": "false"},
        )
        self._hook_engine_patch.start()
        self.addCleanup(self._hook_engine_patch.stop)

    def test_runner_claims_before_dispatch_and_records_success(self):
        runner_module = load_runner()
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "prd_request",
            "step_name": "draft_prd",
            "agent_id": "pm-agent",
            "input": {"topic": "tutorial"},
            "required_artifacts": ["prd"],
            "skill_hints": ["draft-prd"],
        }
        memory = FakeMemoryClient(ready=[task])
        adapter = FakeAdapter(
            {"status": "succeeded", "output": {"summary": "done"}, "runtime_task_id": "rt-1"}
        )
        workflow_runner = runner_module.WorkflowRunner(memory, adapter, runner_id="runner-1")

        workflow_runner.tick()

        self.assertEqual(len(memory.claims), 1)
        self.assertEqual(adapter.payloads[0]["workflow_type"], "prd_request")
        self.assertEqual(adapter.payloads[0]["task_id"], "task-1")
        self.assertEqual(memory.runtime_records, [("task-1", "rt-1")])
        self.assertEqual(memory.completed, [("task-1", {"summary": "done"})])

    def test_dispatch_payload_carries_stage_tool_allow_only_when_set(self):
        runner_module = load_runner()
        wd = runner_module.workflow_definitions

        def dispatched(tool_allow):
            stage = {"name": "publish", "kind": "task", "agent_id": "pm-agent"}
            if tool_allow is not None:
                stage["tool_allow"] = tool_allow
            skeleton = wd.load_skeleton_payload(
                {"workflow_type": "campaign", "definition_version": "1.0.0", "stages": [stage]}
            )
            task = {
                "id": "task-1",
                "workflow_id": "workflow-1",
                "workflow_type": "campaign",
                "step_name": "publish",
                "agent_id": "pm-agent",
                "input": {},
            }
            adapter = FakeAdapter({"status": "succeeded", "output": {"summary": "done"}})
            runner_module.WorkflowRunner(
                FakeMemoryClient(ready=[task]),
                adapter,
                runner_id="runner-1",
                skeletons={"campaign": skeleton},
            ).tick()
            return adapter.payloads[0]

        self.assertNotIn("tool_allow", dispatched(None))
        self.assertEqual(
            dispatched(["google_workspace_docs_append"])["tool_allow"],
            ["google_workspace_docs_append"],
        )

    def test_memory_api_client_sends_bearer_token_when_configured(self):
        runner_module = load_runner()
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"tasks":[]}'

        def fake_urlopen(request, timeout):
            captured["authorization"] = request.headers.get("Authorization")
            return FakeResponse()

        with patch.object(runner_module.urllib.request, "urlopen", fake_urlopen):
            client = runner_module.MemoryApiClient("http://memory", token="memory-token")
            client.list_ready_tasks(1)

        self.assertEqual(captured["authorization"], "Bearer memory-token")

    def test_memory_api_client_create_workflow_sends_definition_version(self):
        runner_module = load_runner()
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"id":"workflow-1"}'

        def fake_urlopen(request, timeout):
            captured["body"] = json.loads(request.data.decode())
            return FakeResponse()

        with patch.object(runner_module.urllib.request, "urlopen", fake_urlopen):
            client = runner_module.MemoryApiClient("http://memory", token="")
            client.create_workflow(
                "workflow-1",
                "custom_flow",
                "tenant-1",
                definition_version="managed-2",
            )

        self.assertEqual(captured["body"]["definition_version"], "managed-2")

    def test_runner_creates_workflow_record_with_task_definition_version(self):
        runner_module = load_runner()
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "custom_flow",
            "definition_version": "managed-2",
            "step_name": "build",
            "agent_id": "agent",
            "tenant_id": "tenant-1",
            "input": {},
        }
        memory = FakeMemoryClient(ready=[task])
        adapter = FakeAdapter({"status": "succeeded", "output": {"ok": True}})
        workflow_runner = runner_module.WorkflowRunner(
            memory,
            adapter,
            runner_id="runner-1",
        )

        workflow_runner.tick()

        self.assertEqual(
            memory.created_workflows,
            [("workflow-1", "custom_flow", "tenant-1", "managed-2")],
        )

    def test_http_request_retries_urlerror_then_succeeds(self):
        runner_module = load_runner()
        attempts = {"n": 0}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"ok":true}'

        def fake_urlopen(request, timeout):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise runner_module.urllib.error.URLError("Temporary failure in name resolution")
            return FakeResponse()

        with patch.object(runner_module.urllib.request, "urlopen", fake_urlopen), patch.object(
            runner_module.time, "sleep", lambda *_: None
        ):
            raw = runner_module._http_request_with_retries(
                "GET",
                "http://memory/health",
                headers={},
                body=None,
                timeout=5,
            )

        self.assertEqual(json.loads(raw), {"ok": True})
        self.assertEqual(attempts["n"], 3)

    def test_list_ready_tasks_soft_fails_after_retries(self):
        runner_module = load_runner()

        def fake_urlopen(request, timeout):
            raise runner_module.urllib.error.URLError("Temporary failure in name resolution")

        output = io.StringIO()
        with patch.object(runner_module.urllib.request, "urlopen", fake_urlopen), patch.object(
            runner_module.time, "sleep", lambda *_: None
        ), redirect_stdout(output):
            client = runner_module.MemoryApiClient("http://memory", token="")
            tasks = client.list_ready_tasks(5)

        self.assertEqual(tasks, [])
        self.assertIn("memory_api_unavailable", output.getvalue())

    def test_runner_proposes_plan_for_expand_stage_result(self):
        runner_module = load_runner()
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "prd_request",
            "step_name": "delivery_planning",
            "agent_id": "pm-agent",
            "input": {},
            "attempt": 1,
            "max_retries": 2,
        }
        proposed_tasks = [{"step_name": "write_story", "agent_id": "pm-agent", "input": {}}]
        memory = FakeMemoryClient(ready=[task])
        adapter = FakeAdapter(
            {"status": "succeeded", "output": {}, "proposed_tasks": proposed_tasks}
        )
        skeleton = runner_module.workflow_definitions.WorkflowSkeleton(
            workflow_type="prd_request",
            definition_version="1.0.0",
            stages=[
                runner_module.workflow_definitions.Stage(
                    name="delivery_planning",
                    kind="expand",
                    agent_id="pm-agent",
                    allowed_agents=["pm-agent"],
                    allowed_external_effects=[],
                    depends_on=[],
                    required_artifacts=[],
                    skill_hints=["delivery-planning"],
                    retry={"max_retries": 2, "timeout_seconds": 1800},
                    plan_approval_required=True,
                )
            ],
        )
        workflow_runner = runner_module.WorkflowRunner(
            memory,
            adapter,
            runner_id="runner-1",
            skeletons={"prd_request": skeleton},
        )

        workflow_runner.tick()

        self.assertEqual(memory.plan_proposals[0][0:2], ("workflow-1", "task-1"))
        self.assertEqual(memory.plan_proposals[0][2][0]["step_name"], "write_story")
        self.assertEqual(memory.plan_proposals[0][2][0]["max_retries"], 2)
        # plan_approval_required=True: task is blocked awaiting human approval,
        # NOT completed immediately.
        self.assertEqual(len(memory.completed), 0)
        self.assertEqual(len(memory.blocked), 1)
        self.assertIn("awaiting_plan_approval", memory.blocked[0][1])
        self.assertIn("plan-1", memory.blocked[0][1])

    def test_runner_uses_persisted_version_for_wait_stage_after_reload(self):
        runner_module = load_runner()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / "history" / "custom_flow"
            history.mkdir(parents=True)

            def skeleton_payload(version, wait_external):
                stage = {
                    "name": "await_external",
                    "kind": "task",
                    "agent_id": "orchestrator",
                }
                if wait_external:
                    stage["wait_external"] = {"event_type": wait_external}
                return {
                    "workflow_type": "custom_flow",
                    "definition_version": version,
                    "stages": [stage],
                }

            v1 = skeleton_payload("v1", "v1.event")
            v2 = skeleton_payload("v2", None)
            (history / "v1-rev.json").write_text(json.dumps(v1), encoding="utf-8")
            active = runner_module.workflow_definitions.load_skeleton_payload(v2)
            task = {
                "id": "task-1",
                "workflow_id": "workflow-1",
                "workflow_type": "custom_flow",
                "definition_version": "v1",
                "step_name": "await_external",
                "agent_id": "orchestrator",
                "input": {},
            }
            memory = FakeMemoryClient(ready=[task])
            adapter = FakeAdapter({"status": "succeeded", "output": {"bad": True}})
            workflow_runner = runner_module.WorkflowRunner(
                memory,
                adapter,
                runner_id="runner-1",
                skeletons={"custom_flow": active},
            )

            with patch.dict(
                "os.environ",
                {"TS_HARNESS_WORKFLOW_DEFINITION_STORE": str(root)},
            ):
                workflow_runner.tick()

        self.assertEqual(adapter.payloads, [])
        self.assertEqual(memory.waits[0][2], "v1.event")
        self.assertEqual(memory.blocked, [("task-1", "awaiting_external:v1.event")])

    def test_runner_uses_persisted_version_for_contract_fallback(self):
        runner_module = load_runner()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / "history" / "custom_flow"
            history.mkdir(parents=True)

            def skeleton_payload(version, property_name):
                return {
                    "workflow_type": "custom_flow",
                    "definition_version": version,
                    "contracts": {
                        "result": {
                            "output_schema": {
                                "type": "object",
                                "properties": {
                                    property_name: {"type": "string"},
                                },
                            }
                        }
                    },
                    "stages": [
                        {
                            "name": "build",
                            "kind": "task",
                            "agent_id": "agent",
                            "contract_ref": "result",
                        }
                    ],
                }

            v1 = skeleton_payload("v1", "v1_field")
            v2 = skeleton_payload("v2", "v2_field")
            (history / "v1-rev.json").write_text(json.dumps(v1), encoding="utf-8")
            active = runner_module.workflow_definitions.load_skeleton_payload(v2)
            task = {
                "id": "task-1",
                "workflow_id": "workflow-1",
                "workflow_type": "custom_flow",
                "definition_version": "v1",
                "step_name": "build",
                "agent_id": "agent",
                "input": {},
            }
            memory = FakeMemoryClient(ready=[task])
            adapter = FakeAdapter({"status": "failed", "error": "stop"})
            workflow_runner = runner_module.WorkflowRunner(
                memory,
                adapter,
                runner_id="runner-1",
                skeletons={"custom_flow": active},
            )

            with patch.dict(
                "os.environ",
                {"TS_HARNESS_WORKFLOW_DEFINITION_STORE": str(root)},
            ), patch(
                "ts_harness.workflows.engine.hook_engine_enabled",
                return_value=False,
            ):
                workflow_runner.tick()

        contract = adapter.payloads[0]["contract"]
        properties = contract["output_schema"]["properties"]
        self.assertIn("v1_field", properties)
        self.assertNotIn("v2_field", properties)

    def test_runner_uses_persisted_version_for_expand_validation(self):
        runner_module = load_runner()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / "history" / "custom_flow"
            history.mkdir(parents=True)

            def skeleton_payload(version, allowed_agent):
                return {
                    "workflow_type": "custom_flow",
                    "definition_version": version,
                    "stages": [
                        {
                            "name": "plan",
                            "kind": "expand",
                            "agent_id": "planner",
                            "allowed_agents": [allowed_agent],
                        }
                    ],
                }

            v1 = skeleton_payload("v1", "pm-agent")
            v2 = skeleton_payload("v2", "ops-agent")
            (history / "v1-rev.json").write_text(json.dumps(v1), encoding="utf-8")
            active = runner_module.workflow_definitions.load_skeleton_payload(v2)
            task = {
                "id": "task-1",
                "workflow_id": "workflow-1",
                "workflow_type": "custom_flow",
                "definition_version": "v1",
                "step_name": "plan",
                "agent_id": "planner",
                "input": {},
            }
            memory = FakeMemoryClient(ready=[task])
            adapter = FakeAdapter(
                {
                    "status": "succeeded",
                    "output": {},
                    "proposed_tasks": [
                        {
                            "step_name": "write_story",
                            "agent_id": "pm-agent",
                            "input": {},
                        }
                    ],
                }
            )
            workflow_runner = runner_module.WorkflowRunner(
                memory,
                adapter,
                runner_id="runner-1",
                skeletons={"custom_flow": active},
            )

            with patch.dict(
                "os.environ",
                {"TS_HARNESS_WORKFLOW_DEFINITION_STORE": str(root)},
            ), patch(
                "ts_harness.workflows.engine.hook_engine_enabled",
                return_value=False,
            ):
                workflow_runner.tick()

        self.assertEqual(len(memory.plan_proposals), 1)
        self.assertEqual(memory.plan_proposals[0][2][0]["agent_id"], "pm-agent")

    def test_runner_blocks_wait_stage_when_wait_hook_fails(self):
        runner_module = load_runner()
        skeletons = runner_module.load_workflow_skeletons(REPO_ROOT / "config" / "workflows")
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "prd_request",
            "step_name": "await_intake_answers",
            "agent_id": "orchestrator",
            "input": {},
        }
        class FailingWaitMemoryClient(FakeMemoryClient):
            def set_task_wait(self, task_id, wait_token, expected_event_type, **deadlines):
                raise RuntimeError("memory api unavailable")

        memory = FailingWaitMemoryClient(ready=[task])
        adapter = FakeAdapter({"status": "succeeded", "output": {"summary": "should not run"}})
        workflow_runner = runner_module.WorkflowRunner(
            memory,
            adapter,
            runner_id="runner-1",
            skeletons={"prd_request": skeletons["prd_request"]},
        )
        workflow_runner.tick()

        self.assertEqual(adapter.payloads, [])
        self.assertEqual(len(memory.blocked), 1)
        self.assertEqual(memory.blocked[0][0], "task-1")
        self.assertIn("wait_external setup failed", memory.blocked[0][1])

    def test_runner_sets_external_wait_without_dispatching(self):
        runner_module = load_runner()
        skeletons = runner_module.load_workflow_skeletons(REPO_ROOT / "config" / "workflows")
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "prd_request",
            "step_name": "await_intake_answers",
            "agent_id": "orchestrator",
            "input": {},
        }
        memory = FakeMemoryClient(ready=[task])
        adapter = FakeAdapter({"status": "succeeded", "output": {"summary": "should not run"}})
        workflow_runner = runner_module.WorkflowRunner(
            memory,
            adapter,
            runner_id="runner-1",
            skeletons={"prd_request": skeletons["prd_request"]},
        )

        workflow_runner.tick()

        self.assertEqual(adapter.payloads, [])
        self.assertEqual(len(memory.waits), 1)
        self.assertEqual(memory.waits[0][0], "task-1")
        self.assertEqual(memory.waits[0][2], "channel.intake_response")
        self.assertEqual(
            memory.blocked,
            [("task-1", "awaiting_external:channel.intake_response")],
        )

    def test_runner_prepares_pr_checks_wait_and_resumes_when_green(self):
        runner_module = load_runner()
        skeletons = runner_module.load_workflow_skeletons(REPO_ROOT / "config" / "workflows")
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "deliver_work_order",
            "step_name": "await_pr_checks",
            "agent_id": "orchestrator",
            "tenant_id": "acme",
            "input": {},
        }
        memory = FakeMemoryClient(ready=[task])
        adapter = FakeAdapter({"status": "succeeded", "output": {}})
        workflow_runner = runner_module.WorkflowRunner(
            memory,
            adapter,
            runner_id="runner-1",
            skeletons={"deliver_work_order": skeletons["deliver_work_order"]},
        )
        engine = MagicMock()
        ready = {"head_sha": "h1", "checks_state": "success"}
        with (
            patch.object(runner_module, "_workflow_engine", return_value=engine),
            patch(
                "ts_harness.workflows.merge_readiness.prepare_checks_wait",
                return_value={"match": {"head_sha": "h1"}, "summary": {"state": "success"}, "options": {}, "ready": ready},
            ) as prepare,
        ):
            workflow_runner.tick()

        self.assertEqual(adapter.payloads, [])
        self.assertEqual(memory.waits[0][2], "github.pr_checks")
        prepare.assert_called_once()
        self.assertEqual(prepare.call_args.args[1]["event_type"], "github.pr_checks")
        kwargs = engine.resume_external.call_args.kwargs
        self.assertEqual((kwargs["event_type"], kwargs["payload"], kwargs["tenant_id"]), ("github.pr_checks", ready, "acme"))

    def _release_runner(self, step_name):
        runner_module = load_runner()
        skeletons = runner_module.load_workflow_skeletons(REPO_ROOT / "config" / "workflows")
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "deploy_release",
            "step_name": step_name,
            "agent_id": "orchestrator",
            "tenant_id": "acme",
            "input": {},
        }
        memory = FakeMemoryClient(ready=[task])
        adapter = FakeAdapter({"status": "succeeded", "output": {}})
        workflow_runner = runner_module.WorkflowRunner(
            memory,
            adapter,
            runner_id="runner-1",
            skeletons={"deploy_release": skeletons["deploy_release"]},
        )
        return runner_module, workflow_runner, memory, adapter

    def test_runner_runs_release_system_steps_without_agent(self):
        runner_module, workflow_runner, memory, adapter = self._release_runner("trigger_deploy_staging")
        engine = MagicMock()
        with (
            patch.object(runner_module, "_workflow_engine", return_value=engine),
            patch(
                "ts_harness.workflows.system_steps.run_system_step",
                return_value={"status": "blocked", "reason": "no approved promote decision"},
            ) as step,
        ):
            workflow_runner.tick()

        self.assertEqual(adapter.payloads, [])
        self.assertEqual(step.call_args.args[0], "trigger_deploy")
        self.assertEqual(step.call_args.args[2]["slot"], "staging")
        self.assertEqual(memory.blocked, [("task-1", "barrier_blocked: no approved promote decision")])

    def test_blocked_system_step_reaches_engine_on_block(self):
        runner_module, workflow_runner, memory, _ = self._release_runner("trigger_deploy_staging")
        engine = MagicMock()
        with (
            patch.object(runner_module, "_workflow_engine", return_value=engine),
            patch("ts_harness.workflows.engine.hook_engine_enabled", return_value=True),
            patch(
                "ts_harness.workflows.system_steps.run_system_step",
                return_value={"status": "blocked", "reason": "stale_base: x", "output": {"stale": [1]}},
            ),
        ):
            workflow_runner.tick()

        self.assertEqual(memory.blocked_outputs, [("task-1", "barrier_blocked: stale_base: x", {"stale": [1]})])
        args, kwargs = engine.on_task_transition.call_args
        self.assertEqual(args[1], "blocked")
        self.assertEqual(kwargs["error"], "barrier_blocked: stale_base: x")
        self.assertEqual(kwargs["dispatch_result"], {"output": {"stale": [1]}})

    def _accepted_runner(self, step_name, agent_id):
        runner_module = load_runner()
        skeletons = runner_module.load_workflow_skeletons(REPO_ROOT / "config" / "workflows")
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "deliver_work_order",
            "step_name": step_name,
            "agent_id": agent_id,
            "input": {"_human_accept": {"actor": "alice", "at": "t", "output": {"verdict": "block", "prs": []}}},
        }
        memory = FakeMemoryClient(ready=[task])
        adapter = FakeAdapter({"status": "succeeded", "output": {}})
        workflow_runner = runner_module.WorkflowRunner(
            memory,
            adapter,
            runner_id="runner-1",
            skeletons={"deliver_work_order": skeletons["deliver_work_order"]},
        )
        return runner_module, workflow_runner, memory, adapter

    def test_accepted_step_completes_with_its_output_without_running(self):
        for step_name, agent_id in (("finalize_code_review", "orchestrator"), ("qa_gate", "qa-agent")):
            with self.subTest(step=step_name):
                runner_module, workflow_runner, memory, adapter = self._accepted_runner(step_name, agent_id)
                with (
                    patch("ts_harness.workflows.system_steps.run_system_step") as step,
                    patch("ts_harness.workflows.code_review.publish_override_status", return_value=[]),
                ):
                    workflow_runner.tick()

                step.assert_not_called()
                self.assertEqual(adapter.payloads, [])
                [(task_id, output)] = memory.completed
                self.assertEqual(task_id, "task-1")
                self.assertEqual(output["human_override"]["actor"], "alice")
                self.assertEqual(output["human_override"]["step"], step_name)

    def test_qa_gate_publishes_pending_before_dispatch_and_the_verdict_after(self):
        runner_module = load_runner()
        skeletons = runner_module.load_workflow_skeletons(REPO_ROOT / "config" / "workflows")
        prs = [{"pr_url": "https://github.com/o/r/pull/19", "head_sha": "62bb0d4"}]
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "deliver_work_order",
            "step_name": "qa_gate",
            "agent_id": "qa-agent",
            "input": {"files": [{"filename": "a.js"}], "prs": prs},
        }
        result = {"status": "succeeded", "output": {"status": "success", "result": {"prs": []}}}
        calls = []

        class RecordingAdapter(FakeAdapter):
            def dispatch(self, payload):
                calls.append("dispatch")
                return super().dispatch(payload)

        memory = FakeMemoryClient(ready=[task])
        workflow_runner = runner_module.WorkflowRunner(
            memory,
            RecordingAdapter(result),
            runner_id="runner-1",
            skeletons={"deliver_work_order": skeletons["deliver_work_order"]},
        )
        with (
            patch("ts_harness.workflows.qa_status.qa_started", side_effect=lambda i: calls.append(("started", i["prs"])) or []),
            patch(
                "ts_harness.workflows.qa_status.qa_finished",
                side_effect=lambda i, r: calls.append(("finished", r["status"])) or [],
            ),
        ):
            workflow_runner.tick()

        self.assertEqual(calls, [("started", prs), "dispatch", ("finished", "succeeded")])

    def test_runner_prepares_deployment_wait(self):
        runner_module, workflow_runner, memory, _ = self._release_runner("await_deployment_staging")
        engine = MagicMock()
        ready = {"sha": "s1", "environment": "staging", "state": "success"}
        with (
            patch.object(runner_module, "_workflow_engine", return_value=engine),
            patch(
                "ts_harness.workflows.release.prepare_deployment_wait",
                return_value={"match": {"sha": "s1", "environment": "staging"}, "ready": ready},
            ) as prepare,
        ):
            workflow_runner.tick()

        self.assertEqual(memory.waits[0][2], "github.deployment_status")
        prepare.assert_called_once()
        kwargs = engine.resume_external.call_args.kwargs
        self.assertEqual((kwargs["event_type"], kwargs["payload"]), ("github.deployment_status", ready))

    def test_runner_blocks_when_artifact_presentation_fails(self):
        runner_module = load_runner()
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "prd_request",
            "step_name": "draft_prd",
            "agent_id": "pm-agent",
            "input": {},
        }
        memory = FakeMemoryClient(ready=[task])
        adapter = FakeAdapter(
            {
                "status": "succeeded",
                "output": {"prd_markdown": "# Draft"},
                "runtime_task_id": "rt-1",
            }
        )

        class FakeEngine:
            def reload_if_changed(self):
                return False

            def on_task_transition(self, task, event, dispatch_result=None):
                return {
                    "hook": "present_specialist_output",
                    "status": "error",
                    "error": "GATEWAY_TOKEN is required",
                }

            def after_task_succeeded(self, task, dispatch_result=None):
                raise AssertionError("should not run after blocked presentation")

        workflow_runner = runner_module.WorkflowRunner(
            memory,
            adapter,
            runner_id="runner-1",
        )
        with patch(
            "ts_harness.workflows.engine.hook_engine_enabled",
            return_value=True,
        ), patch(
            "ts_harness.workflows.engine.get_workflow_engine",
            return_value=FakeEngine(),
        ):
            workflow_runner.tick()

        self.assertEqual(memory.completed, [])
        self.assertEqual(memory.blocked, [("task-1", "GATEWAY_TOKEN is required")])

    def test_runner_completes_when_presentation_is_skipped(self):
        runner_module = load_runner()
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "deliver_work_order",
            "step_name": "deliver_work_order",
            "agent_id": "dev-agent",
            "input": {},
        }
        memory = FakeMemoryClient(ready=[task])
        adapter = FakeAdapter(
            {
                "status": "succeeded",
                "output": {"work_order_id": "WO-1", "pr_url": "https://github.com/o/r/pull/1"},
                "runtime_task_id": "rt-1",
            }
        )

        class FakeEngine:
            def reload_if_changed(self):
                return False

            def on_task_transition(self, task, event, dispatch_result=None):
                return {"hook": "present_specialist_output", "status": "skipped", "reason": "no bound channel"}

            def after_task_succeeded(self, task, dispatch_result=None):
                return None

        workflow_runner = runner_module.WorkflowRunner(memory, adapter, runner_id="runner-1")
        with patch(
            "ts_harness.workflows.engine.hook_engine_enabled",
            return_value=True,
        ), patch(
            "ts_harness.workflows.engine.get_workflow_engine",
            return_value=FakeEngine(),
        ):
            workflow_runner.tick()

        self.assertEqual(memory.blocked, [])
        self.assertEqual([task_id for task_id, _ in memory.completed], ["task-1"])

    def test_runner_calls_after_task_succeeded_after_complete_task(self):
        runner_module = load_runner()
        task = {
            "id": "task-prep",
            "workflow_id": "workflow-wo",
            "workflow_type": "work_orders_from_prd",
            "step_name": "prepare_work_order_delivery",
            "agent_id": "orchestrator",
            "tenant_id": "default",
            "status": "claimed",
            "input": {},
        }
        memory = FakeMemoryClient(ready=[task])
        adapter = FakeAdapter(
            {
                "status": "succeeded",
                "output": {
                    "planner_complete": True,
                    "work_orders_handoff": True,
                    "next_work_order_id": "WO-001",
                    "work_order_ids": ["WO-001"],
                },
                "runtime_task_id": "rt-prep",
            }
        )

        class FakeEngine:
            def __init__(self):
                self.transitions = []
                self.after_calls = []

            def reload_if_changed(self):
                return False

            def on_task_transition(self, task, event, dispatch_result=None):
                self.transitions.append((dict(task), event, dispatch_result))
                return None

            def after_task_succeeded(self, task, dispatch_result=None):
                self.after_calls.append((dict(task), dispatch_result))
                return {"status": "phase_completed_auto_advanced"}

        engine = FakeEngine()
        workflow_runner = runner_module.WorkflowRunner(
            memory,
            adapter,
            runner_id="runner-1",
        )
        output = io.StringIO()
        with redirect_stdout(output), patch(
            "ts_harness.workflows.engine.hook_engine_enabled",
            return_value=True,
        ), patch(
            "ts_harness.workflows.engine.get_workflow_engine",
            return_value=engine,
        ):
            workflow_runner.tick()

        self.assertEqual(len(memory.completed), 1)
        self.assertEqual(memory.completed[0][0], "task-prep")
        self.assertEqual(len(engine.transitions), 1)
        self.assertEqual(engine.transitions[0][0].get("status"), "claimed")
        self.assertEqual(len(engine.after_calls), 1)
        self.assertEqual(engine.after_calls[0][0].get("status"), "succeeded")
        self.assertEqual(engine.after_calls[0][0].get("output", {}).get("next_work_order_id"), "WO-001")
        events = [
            json.loads(line)["event"]
            for line in output.getvalue().splitlines()
            if line.strip()
        ]
        self.assertIn("completed", events)
        self.assertIn("program_phase_hook", events)

    def test_runner_reloads_global_engine_before_success_hooks(self):
        runner_module = load_runner()
        task = {
            "id": "task-prep",
            "workflow_id": "workflow-wo",
            "workflow_type": "work_orders_from_prd",
            "step_name": "prepare_work_order_delivery",
            "agent_id": "orchestrator",
            "tenant_id": "default",
            "status": "claimed",
            "input": {},
        }
        memory = FakeMemoryClient(ready=[task])
        adapter = FakeAdapter({"status": "succeeded", "output": {"ok": True}})

        class FakeEngine:
            def __init__(self):
                self.calls = []

            def reload_if_changed(self):
                self.calls.append("reload")
                return True

            def on_task_transition(self, task, event, dispatch_result=None):
                self.calls.append("on_task_transition")
                return None

            def after_task_succeeded(self, task, dispatch_result=None):
                self.calls.append("after_task_succeeded")
                return None

        engine = FakeEngine()
        workflow_runner = runner_module.WorkflowRunner(
            memory,
            adapter,
            runner_id="runner-1",
        )
        with patch(
            "ts_harness.workflows.engine.hook_engine_enabled",
            return_value=True,
        ), patch(
            "ts_harness.workflows.engine.get_workflow_engine",
            return_value=engine,
        ):
            workflow_runner.tick()

        self.assertEqual(
            engine.calls,
            [
                "reload",
                "on_task_transition",
                "reload",
                "after_task_succeeded",
            ],
        )

    def test_runner_logs_blocked_dispatch_result(self):
        runner_module = load_runner()
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "prd_request",
            "step_name": "draft_prd",
            "agent_id": "pm-agent",
            "input": {},
        }
        memory = FakeMemoryClient(ready=[task])
        adapter = FakeAdapter(
            {
                "status": "blocked",
                "error": "draft_prd output must include result.prd_markdown",
                "runtime_task_id": "rt-1",
            }
        )
        workflow_runner = runner_module.WorkflowRunner(
            memory,
            adapter,
            runner_id="runner-1",
        )

        output = io.StringIO()
        with redirect_stdout(output):
            workflow_runner.tick()

        events = [
            json.loads(line)["event"]
            for line in output.getvalue().splitlines()
            if line.strip()
        ]
        self.assertIn("blocked", events)
        self.assertEqual(
            memory.blocked,
            [("task-1", "draft_prd output must include result.prd_markdown")],
        )

    def test_terminal_block_hands_the_blocked_output_to_the_engine(self):
        runner_module = load_runner()
        task = {
            "id": "qa-1",
            "workflow_id": "workflow-1",
            "workflow_type": "deliver_work_order",
            "step_name": "qa_gate",
            "agent_id": "qa-agent",
            "input": {},
        }
        qa_output = {"status": "failed", "result": {"summary": "QA blocked", "prs": [{"defects": [{"id": "QA-001"}]}]}}
        memory = FakeMemoryClient(ready=[task])
        adapter = FakeAdapter(
            {"status": "blocked", "error": "stage contract outcome: blocked", "runtime_task_id": "rt-1", "output": qa_output}
        )
        workflow_runner = runner_module.WorkflowRunner(memory, adapter, runner_id="runner-1")

        with patch.dict("os.environ", {"TS_HARNESS_HOOK_ENGINE": "true"}), patch.object(
            runner_module, "_workflow_engine"
        ) as engine, patch.object(workflow_runner, "_publish_qa_status"), redirect_stdout(io.StringIO()):
            workflow_runner.tick()

        args, kwargs = engine.return_value.on_task_transition.call_args
        self.assertEqual(args[1], "blocked")
        self.assertEqual(kwargs["dispatch_result"]["output"], qa_output)
        self.assertEqual(kwargs["error"], "stage contract outcome: blocked")

    def test_runner_parks_tool_approval_block_without_retry_or_hooks(self):
        runner_module = load_runner()
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "prd_request",
            "step_name": "draft_prd",
            "agent_id": "pm-agent",
            "input": {},
            "attempt": 1,
            "max_retries": 3,
        }
        memory = FakeMemoryClient(ready=[task])
        adapter = FakeAdapter(
            {
                "status": "blocked",
                "error": "awaiting_tool_approval:ap-1",
                "runtime_task_id": "rt-1",
                "output": {"approval_id": "ap-1", "tool_name": "google_workspace_docs_append"},
            }
        )
        workflow_runner = runner_module.WorkflowRunner(memory, adapter, runner_id="runner-1")

        output = io.StringIO()
        with patch.dict("os.environ", {"TS_HARNESS_HOOK_ENGINE": "true"}), patch.object(
            runner_module, "_workflow_engine"
        ) as engine, redirect_stdout(output):
            workflow_runner.tick()

        events = [json.loads(line)["event"] for line in output.getvalue().splitlines() if line.strip()]
        self.assertIn("awaiting_tool_approval", events)
        self.assertEqual(memory.blocked, [("task-1", "awaiting_tool_approval:ap-1")])
        self.assertEqual(memory.retried, [])
        engine.return_value.on_task_transition.assert_not_called()
        notify = engine.return_value.notify_tool_approval_waiting
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0]["id"], "task-1")
        self.assertEqual(notify.call_args.kwargs, {"tool_name": "google_workspace_docs_append"})

    def test_tool_approval_notice_failure_still_parks(self):
        runner_module = load_runner()
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "prd_request",
            "step_name": "draft_prd",
            "agent_id": "pm-agent",
            "input": {},
            "attempt": 1,
            "max_retries": 3,
        }
        memory = FakeMemoryClient(ready=[task])
        adapter = FakeAdapter(
            {"status": "blocked", "error": "awaiting_tool_approval:ap-1", "runtime_task_id": "rt-1"}
        )
        workflow_runner = runner_module.WorkflowRunner(memory, adapter, runner_id="runner-1")

        output = io.StringIO()
        with patch.dict("os.environ", {"TS_HARNESS_HOOK_ENGINE": "true"}), patch.object(
            runner_module, "_workflow_engine"
        ) as engine, redirect_stdout(output):
            engine.return_value.notify_tool_approval_waiting.side_effect = RuntimeError("slack down")
            workflow_runner.tick()

        events = [json.loads(line)["event"] for line in output.getvalue().splitlines() if line.strip()]
        self.assertIn("tool_approval_notify_error", events)
        self.assertEqual(memory.blocked, [("task-1", "awaiting_tool_approval:ap-1")])
        self.assertEqual(memory.retried, [])

    def test_runner_persists_rejected_output_on_blocked_dispatch(self):
        runner_module = load_runner()
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "work_orders_from_prd",
            "step_name": "design_blueprints",
            "agent_id": "pm-agent",
            "input": {"stage": "foundry"},
            "attempt": 1,
            "max_retries": 2,
        }
        memory = FakeMemoryClient(ready=[task])
        rejected = {
            "summary": "partial foundry attempt",
            "artifacts": {"blueprints_markdown": "# notes\n\ntoo thin"},
        }
        error = (
            "work_orders_from_prd output must include artifacts with "
            "completed foundry content: missing foundry artifact markdown"
        )
        adapter = FakeAdapter(
            {
                "status": "blocked",
                "error": error,
                "output": rejected,
                "runtime_task_id": "rt-foundry-1",
            }
        )
        workflow_runner = runner_module.WorkflowRunner(
            memory,
            adapter,
            runner_id="runner-1",
        )
        workflow_runner.tick()

        self.assertEqual(len(memory.blocked_outputs), 1)
        task_id, blocked_error, output = memory.blocked_outputs[0]
        self.assertEqual(task_id, "task-1")
        self.assertEqual(blocked_error, error)
        self.assertIsInstance(output, dict)
        self.assertEqual(output.get("summary"), "partial foundry attempt")
        self.assertTrue(output.get("rejected"))
        self.assertEqual(output.get("validation_error"), error)
        self.assertIn("blueprints_markdown", (output.get("artifacts") or {}))

    def test_runner_logs_block_conflict_without_crashing(self):
        runner_module = load_runner()
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "prd_request",
            "step_name": "draft_prd",
            "agent_id": "pm-agent",
            "input": {},
        }

        class ConflictingBlockMemoryClient(FakeMemoryClient):
            def block_task(self, task_id, error, output=None):
                raise RuntimeError(
                    '{"error": "illegal workflow transition: succeeded -> blocked"}'
                )

            def get_workflow_record(self, workflow_id):
                return {
                    "workflow": {"id": workflow_id},
                    "tasks": [
                        {
                            **task,
                            "status": "succeeded",
                            "output": {
                                "result": {
                                    "prd_markdown": "# PRD\n\n## Overview\nDone."
                                }
                            },
                        }
                    ],
                }

        class FakeEngine:
            def __init__(self):
                self.transitions = []

            def reload_if_changed(self):
                return False

            def on_task_transition(self, task, event, dispatch_result=None):
                self.transitions.append((task, event, dispatch_result))
                return {"status": "presented"}

        memory = ConflictingBlockMemoryClient(ready=[task])
        adapter = FakeAdapter(
            {
                "status": "blocked",
                "error": "draft_prd output must include result.prd_markdown",
                "runtime_task_id": "rt-1",
            }
        )
        workflow_runner = runner_module.WorkflowRunner(
            memory,
            adapter,
            runner_id="runner-1",
        )
        engine = FakeEngine()

        output = io.StringIO()
        with redirect_stdout(output), patch(
            "ts_harness.workflows.engine.hook_engine_enabled",
            return_value=True,
        ), patch(
            "ts_harness.workflows.engine.get_workflow_engine",
            return_value=engine,
        ):
            workflow_runner.tick()

        events = [
            json.loads(line)["event"]
            for line in output.getvalue().splitlines()
            if line.strip()
        ]
        self.assertIn("blocked_transition_conflict", events)
        self.assertIn("succeeded_transition_recovered", events)
        self.assertEqual(engine.transitions[0][1], "succeeded")
        self.assertEqual(
            engine.transitions[0][2]["output"]["result"]["prd_markdown"],
            "# PRD\n\n## Overview\nDone.",
        )

    def test_runner_rejects_disallowed_expand_proposal(self):
        runner_module = load_runner()
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "prd_request",
            "step_name": "delivery_planning",
            "agent_id": "pm-agent",
            "input": {},
            "attempt": 0,
            "max_retries": 2,
        }
        memory = FakeMemoryClient(ready=[task])
        adapter = FakeAdapter(
            {
                "status": "succeeded",
                "output": {},
                "proposed_tasks": [
                    {"step_name": "deploy", "agent_id": "ops-agent", "input": {}}
                ],
            }
        )
        skeleton = runner_module.workflow_definitions.WorkflowSkeleton(
            workflow_type="prd_request",
            definition_version="1.0.0",
            stages=[
                runner_module.workflow_definitions.Stage(
                    name="delivery_planning",
                    kind="expand",
                    agent_id="pm-agent",
                    allowed_agents=["pm-agent"],
                    allowed_external_effects=[],
                    depends_on=[],
                    required_artifacts=[],
                    skill_hints=[],
                    retry={"max_retries": 2, "timeout_seconds": 1800},
                )
            ],
        )
        workflow_runner = runner_module.WorkflowRunner(
            memory, adapter, runner_id="runner-1", skeletons={"prd_request": skeleton}
        )

        workflow_runner.tick()

        self.assertEqual(memory.plan_proposals, [])
        self.assertEqual(memory.failed[0][0], "task-1")

    def test_runner_retries_failed_dispatch_when_budget_remains(self):
        runner_module = load_runner()
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "prd_request",
            "step_name": "draft_prd",
            "agent_id": "pm-agent",
            "attempt": 0,
            "max_retries": 2,
        }
        memory = FakeMemoryClient(ready=[task])
        adapter = FakeAdapter({"status": "failed", "error": "temporary"})
        workflow_runner = runner_module.WorkflowRunner(memory, adapter, runner_id="runner-1")

        workflow_runner.tick()

        self.assertEqual(memory.failed, [("task-1", "temporary")])
        self.assertEqual(memory.retried, [("task-1", "dispatch failed")])

    def test_gateway_adapter_maps_urlerror_to_transport_blocked(self):
        runner_module = load_runner()

        def fake_urlopen(request, timeout):
            raise runner_module.urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

        adapter = runner_module.GatewayHttpAdapter("http://runtime", "token")
        with patch.object(runner_module.urllib.request, "urlopen", fake_urlopen):
            result = adapter.dispatch({"task_id": "task-1"})

        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["error"].startswith(runner_module.TRANSPORT_ERROR_PREFIX))

    def test_gateway_adapter_uses_configured_dispatch_timeout(self):
        runner_module = load_runner()
        seen = {}

        def fake_urlopen(request, timeout):
            seen["timeout"] = timeout
            raise runner_module.urllib.error.URLError(TimeoutError("timed out"))

        adapter = runner_module.GatewayHttpAdapter(
            "http://runtime",
            "token",
            timeout_seconds=7200,
        )
        with patch.object(runner_module.urllib.request, "urlopen", fake_urlopen):
            result = adapter.dispatch({"task_id": "task-1"})

        self.assertEqual(seen["timeout"], 7200)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["error"].startswith(runner_module.TRANSPORT_ERROR_PREFIX))

    def test_runner_retries_transport_blocked_dispatch(self):
        runner_module = load_runner()
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "prd_request",
            "step_name": "draft_prd",
            "agent_id": "pm-agent",
            "attempt": 0,
            "max_retries": 2,
        }
        memory = FakeMemoryClient(ready=[task])
        adapter = FakeAdapter(
            {
                "status": "blocked",
                "error": f"{runner_module.TRANSPORT_ERROR_PREFIX} connection refused",
            }
        )
        workflow_runner = runner_module.WorkflowRunner(memory, adapter, runner_id="runner-1")

        workflow_runner.tick()

        self.assertEqual(len(memory.blocked), 1)
        self.assertEqual(memory.blocked[0][0], "task-1")
        self.assertTrue(memory.blocked[0][1].startswith(runner_module.TRANSPORT_ERROR_PREFIX))
        self.assertEqual(len(memory.retried), 1)
        self.assertEqual(memory.retried[0][0], "task-1")

    def test_runner_recovers_when_adapter_raises_after_claim(self):
        runner_module = load_runner()
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "prd_request",
            "step_name": "draft_prd",
            "agent_id": "pm-agent",
            "attempt": 0,
            "max_retries": 2,
        }

        class RaisingAdapter:
            def dispatch(self, payload):
                raise RuntimeError("unexpected adapter failure")

        memory = FakeMemoryClient(ready=[task])
        tick_path = Path(tempfile.mkdtemp()) / "workflow-runner.tick"
        workflow_runner = runner_module.WorkflowRunner(
            memory,
            RaisingAdapter(),
            runner_id="runner-1",
            tick_file=tick_path,
        )

        workflow_runner.tick()

        self.assertEqual(len(memory.claims), 1)
        self.assertEqual(len(memory.blocked), 1)
        self.assertEqual(memory.blocked[0][0], "task-1")
        self.assertIn("unexpected adapter failure", memory.blocked[0][1])
        self.assertEqual(len(memory.retried), 1)
        self.assertTrue(tick_path.exists())

    def test_runner_blocks_stale_tasks_when_configured(self):
        runner_module = load_runner()
        memory = FakeMemoryClient()
        memory.stale = [{"id": "task-1"}]
        adapter = FakeAdapter({"status": "succeeded", "output": {}})
        workflow_runner = runner_module.WorkflowRunner(
            memory, adapter, runner_id="runner-1", stale_policy="block"
        )

        workflow_runner.tick()

        self.assertEqual(memory.blocked, [("task-1", "task lease expired")])

    def test_runner_logs_json_lines_to_stdout(self):
        runner_module = load_runner()
        memory = FakeMemoryClient()
        adapter = FakeAdapter({"status": "succeeded", "output": {}})
        workflow_runner = runner_module.WorkflowRunner(memory, adapter, runner_id="runner-1")

        output = io.StringIO()
        with redirect_stdout(output):
            workflow_runner.tick()

        log_line = json.loads(output.getvalue().splitlines()[0])
        self.assertEqual(log_line["event"], "ticked")

    def test_compute_poll_backoff_grows_then_caps(self):
        runner_module = load_runner()
        self.assertEqual(runner_module.compute_poll_backoff(10, 0), 10)
        self.assertEqual(runner_module.compute_poll_backoff(10, 1), 20)
        self.assertEqual(runner_module.compute_poll_backoff(10, 2), 40)
        self.assertEqual(runner_module.compute_poll_backoff(10, 3), 60)
        self.assertEqual(runner_module.compute_poll_backoff(10, 8), 60)

    def test_runtime_not_ready_skips_claims(self):
        runner_module = load_runner()
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "prd_request",
            "step_name": "draft_prd",
            "agent_id": "pm-agent",
            "input": {},
        }
        memory = FakeMemoryClient(ready=[task])
        adapter = FakeAdapter({"status": "succeeded", "output": {}})
        workflow_runner = runner_module.WorkflowRunner(
            memory,
            adapter,
            runner_id="runner-1",
            runtime_url="http://runtime",
        )

        def fake_urlopen(request, timeout):
            raise runner_module.urllib.error.URLError("connection refused")

        with patch.object(runner_module.urllib.request, "urlopen", fake_urlopen):
            workflow_runner.tick()

        self.assertEqual(memory.claims, [])
        self.assertEqual(adapter.payloads, [])

    def test_runtime_ready_allows_claims(self):
        runner_module = load_runner()
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "prd_request",
            "step_name": "draft_prd",
            "agent_id": "pm-agent",
            "input": {},
        }
        memory = FakeMemoryClient(ready=[task])
        adapter = FakeAdapter({"status": "succeeded", "output": {"summary": "ok"}})
        workflow_runner = runner_module.WorkflowRunner(
            memory,
            adapter,
            runner_id="runner-1",
            runtime_url="http://runtime",
        )

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"status":"ok"}'

        with patch.object(
            runner_module.urllib.request, "urlopen", lambda *a, **k: FakeResponse()
        ):
            workflow_runner.tick()

        self.assertEqual(len(memory.claims), 1)
        self.assertEqual(len(adapter.payloads), 1)

    def test_main_refuses_missing_workflow_directory(self):
        runner_module = load_runner()
        missing_dir = Path(tempfile.mkdtemp()) / "missing"

        with self.assertRaises(RuntimeError):
            runner_module.load_workflow_skeletons(missing_dir)

    def test_load_workflow_skeletons_skips_schema_json(self):
        runner_module = load_runner()
        workflow_dir = REPO_ROOT / "config" / "workflows"
        skeletons = runner_module.load_workflow_skeletons(workflow_dir)
        self.assertIn("prd_request", skeletons)
        self.assertNotIn("schema", skeletons)

    def test_load_workflow_skeletons_ignores_read_only_history_snapshot_failure(self):
        runner_module = load_runner()
        workflow_dir = REPO_ROOT / "config" / "workflows"

        with patch.object(
            runner_module,
            "ensure_history_snapshot",
            side_effect=PermissionError("read-only workflow-definitions"),
        ):
            skeletons = runner_module.load_workflow_skeletons(workflow_dir)

        self.assertIn("prd_request", skeletons)

    def test_runner_builtin_missing_history_after_upgrade_fails_closed(self):
        runner_module = load_runner()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            active = runner_module.workflow_definitions.WorkflowSkeleton(
                workflow_type="custom_flow",
                definition_version="v2",
                stages=[
                    runner_module.workflow_definitions.Stage(
                        name="start",
                        kind="task",
                        agent_id="agent",
                        allowed_agents=["agent"],
                        allowed_external_effects=[],
                        depends_on=[],
                        required_artifacts=[],
                        skill_hints=[],
                        retry={"max_retries": 2, "timeout_seconds": 1800},
                    )
                ],
            )
            task = {
                "id": "task-1",
                "workflow_id": "workflow-1",
                "workflow_type": "custom_flow",
                "definition_version": "v1",
                "step_name": "start",
                "agent_id": "agent",
                "input": {},
            }
            workflow_runner = runner_module.WorkflowRunner(
                FakeMemoryClient(ready=[task]),
                FakeAdapter({"status": "succeeded", "output": {}}),
                runner_id="runner-1",
                skeletons={"custom_flow": active},
            )

            with patch.dict(
                "os.environ",
                {"TS_HARNESS_WORKFLOW_DEFINITION_STORE": str(root)},
            ):
                with self.assertRaises(RuntimeError):
                    workflow_runner._skeleton_for_task(task)

    def test_runner_creates_workflow_record_with_current_version_for_legacy_task(self):
        runner_module = load_runner()
        skeleton = runner_module.workflow_definitions.WorkflowSkeleton(
            workflow_type="custom_flow",
            definition_version="managed-9",
            stages=[
                runner_module.workflow_definitions.Stage(
                    name="build",
                    kind="task",
                    agent_id="agent",
                    allowed_agents=["agent"],
                    allowed_external_effects=[],
                    depends_on=[],
                    required_artifacts=[],
                    skill_hints=[],
                    retry={"max_retries": 2, "timeout_seconds": 1800},
                )
            ],
        )
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "custom_flow",
            "step_name": "build",
            "agent_id": "agent",
            "tenant_id": "tenant-1",
            "input": {},
        }
        memory = FakeMemoryClient(ready=[task])
        workflow_runner = runner_module.WorkflowRunner(
            memory,
            FakeAdapter({"status": "succeeded", "output": {"ok": True}}),
            runner_id="runner-1",
            skeletons={"custom_flow": skeleton},
        )

        workflow_runner.tick()

        self.assertEqual(
            memory.created_workflows,
            [("workflow-1", "custom_flow", "tenant-1", "managed-9")],
        )

    def test_load_workflow_skeletons_uses_manifest_history_when_active_stale(self):
        runner_module = load_runner()
        from ts_harness.workflows.definition_paths import (
            definition_content_revision,
            history_dir,
            history_filename,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtins = root / "builtins"
            managed = root / "managed"
            active = managed / "active"
            history = history_dir(managed, "custom_flow")
            builtins.mkdir()
            active.mkdir(parents=True)
            history.mkdir(parents=True)
            builtin = {
                "workflow_type": "custom_flow",
                "definition_version": "builtin",
                "stages": [{"name": "start", "kind": "task", "agent_id": "builtin"}],
            }
            managed_payload = {
                "workflow_type": "custom_flow",
                "definition_version": "managed-2",
                "stages": [{"name": "start", "kind": "task", "agent_id": "managed"}],
            }
            stale = {
                "workflow_type": "custom_flow",
                "definition_version": "managed-1",
                "stages": [{"name": "start", "kind": "task", "agent_id": "stale"}],
            }
            revision = definition_content_revision(managed_payload)
            (builtins / "custom_flow.json").write_text(
                json.dumps(builtin), encoding="utf-8"
            )
            (active / "custom_flow.json").write_text(
                json.dumps(stale), encoding="utf-8"
            )
            (history / history_filename("managed-2", revision)).write_text(
                json.dumps(managed_payload), encoding="utf-8"
            )
            (active / "manifest.json").write_text(
                json.dumps(
                    {
                        "generation": 2,
                        "workflows": {
                            "custom_flow": {
                                "definition_version": "managed-2",
                                "revision": revision,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ",
                {
                    "TS_HARNESS_MANAGED_WORKFLOWS_DIR": str(active),
                    "TS_HARNESS_WORKFLOW_DEFINITION_STORE": str(managed),
                },
            ):
                skeletons = runner_module.load_workflow_skeletons(builtins)

            self.assertEqual(skeletons["custom_flow"].definition_version, "managed-2")
            self.assertEqual(skeletons["custom_flow"].stage("start").agent_id, "managed")

    def test_runner_resolves_unversioned_task_from_workflow_record(self):
        runner_module = load_runner()
        active = runner_module.workflow_definitions.WorkflowSkeleton(
            workflow_type="custom_flow",
            definition_version="managed-10",
            stages=[
                runner_module.workflow_definitions.Stage(
                    name="start",
                    kind="task",
                    agent_id="agent",
                    allowed_agents=["agent"],
                    allowed_external_effects=[],
                    depends_on=[],
                    required_artifacts=[],
                    skill_hints=[],
                    retry={"max_retries": 0, "timeout_seconds": 1800},
                )
            ],
        )
        old = runner_module.workflow_definitions.WorkflowSkeleton(
            workflow_type="custom_flow",
            definition_version="managed-9",
            stages=[
                runner_module.workflow_definitions.Stage(
                    name="start",
                    kind="task",
                    agent_id="legacy-agent",
                    allowed_agents=["legacy-agent"],
                    allowed_external_effects=[],
                    depends_on=[],
                    required_artifacts=[],
                    skill_hints=[],
                    retry={"max_retries": 0, "timeout_seconds": 1800},
                )
            ],
        )
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "custom_flow",
            "step_name": "start",
            "agent_id": "legacy-agent",
            "input": {},
        }
        memory = FakeMemoryClient(ready=[task])
        memory._workflow_records = {
            "workflow-1": {
                "workflow": {
                    "id": "workflow-1",
                    "definition_version": "managed-9",
                },
                "tasks": [task],
            }
        }
        workflow_runner = runner_module.WorkflowRunner(
            memory,
            FakeAdapter({"status": "succeeded", "output": {}}),
            runner_id="runner-1",
            skeletons={"custom_flow": active},
        )
        workflow_runner._versioned_skeletons[("custom_flow", "managed-9")] = old
        resolved = workflow_runner._skeleton_for_task(task)
        self.assertEqual(resolved.definition_version, "managed-9")
        self.assertEqual(resolved.stage("start").agent_id, "legacy-agent")

    def test_hook_transition_error_fails_task_instead_of_completing(self):
        runner_module = load_runner()
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "prd_request",
            "step_name": "draft_prd",
            "agent_id": "pm-agent",
            "input": {},
        }
        memory = FakeMemoryClient(ready=[task])
        adapter = FakeAdapter({"status": "succeeded", "output": {"summary": "ok"}})
        workflow_runner = runner_module.WorkflowRunner(
            memory,
            adapter,
            runner_id="runner-1",
            skeletons=runner_module.load_workflow_skeletons(
                REPO_ROOT / "config" / "workflows"
            ),
        )

        class BrokenEngine:
            def reload_if_changed(self):
                return True

            def on_task_transition(self, task, event, dispatch_result=None):
                raise LookupError("missing version")

        with patch.object(runner_module, "_workflow_engine", return_value=BrokenEngine()):
            with patch.dict("os.environ", {"TS_HARNESS_HOOK_ENGINE": "true"}):
                workflow_runner.tick()

        self.assertEqual(memory.completed, [])
        self.assertEqual(len(memory.failed), 1)
        self.assertIn("hook_transition_error", memory.failed[0][1])

    def test_runner_builtin_snapshot_preserves_old_version_after_upgrade(self):
        runner_module = load_runner()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtins = root / "builtins"
            managed = root / "managed"
            builtins.mkdir()

            def payload(version, event_type):
                return {
                    "workflow_type": "custom_flow",
                    "definition_version": version,
                    "stages": [
                        {
                            "name": "await_external",
                            "kind": "task",
                            "agent_id": "orchestrator",
                            "wait_external": {"event_type": event_type},
                        }
                    ],
                }

            (builtins / "custom_flow.json").write_text(
                json.dumps(payload("builtin:1", "v1.event")),
                encoding="utf-8",
            )

            env = {"TS_HARNESS_WORKFLOW_DEFINITION_STORE": str(managed)}
            with patch.dict("os.environ", env):
                first = runner_module.load_workflow_skeletons(builtins)
                self.assertEqual(first["custom_flow"].definition_version, "builtin:1")

                (builtins / "custom_flow.json").write_text(
                    json.dumps(payload("builtin:2", "v2.event")),
                    encoding="utf-8",
                )
                current = runner_module.load_workflow_skeletons(builtins)
                task = {
                    "id": "task-1",
                    "workflow_id": "workflow-1",
                    "workflow_type": "custom_flow",
                    "definition_version": "builtin:1",
                    "step_name": "await_external",
                    "agent_id": "orchestrator",
                    "input": {},
                }
                memory = FakeMemoryClient(ready=[task])
                adapter = FakeAdapter({"status": "succeeded", "output": {"bad": True}})
                workflow_runner = runner_module.WorkflowRunner(
                    memory,
                    adapter,
                    runner_id="runner-1",
                    skeletons=current,
                )
                workflow_runner.tick()

        self.assertEqual(adapter.payloads, [])
        self.assertEqual(memory.waits[0][2], "v1.event")
        self.assertEqual(memory.blocked, [("task-1", "awaiting_external:v1.event")])


class BlockingAdapter:
    """Holds every dispatch until ``release`` is set, like a long OpenCode run."""

    def __init__(self, result=None):
        self.result = result or {"status": "succeeded", "output": {"summary": "done"}}
        self.release = threading.Event()
        self.started = []
        self._lock = threading.Lock()

    def dispatch(self, payload):
        with self._lock:
            self.started.append(payload["task_id"])
        self.release.wait(5)
        return self.result


def _task(task_id, workflow_id):
    return {
        "id": task_id,
        "workflow_id": workflow_id,
        "workflow_type": "prd_request",
        "step_name": "draft_prd",
        "agent_id": "pm-agent",
    }


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class WorkerPoolTests(unittest.TestCase):
    def setUp(self):
        self._hook_engine_patch = patch.dict("os.environ", {"TS_HARNESS_HOOK_ENGINE": "false"})
        self._hook_engine_patch.start()
        self.addCleanup(self._hook_engine_patch.stop)
        self.runner_module = load_runner()
        self.tick_path = Path(tempfile.mkdtemp()) / "workflow-runner.tick"

    def _runner(self, memory, adapter, max_workers=4):
        from concurrent.futures import ThreadPoolExecutor

        executor = ThreadPoolExecutor(max_workers=max_workers)
        self.addCleanup(executor.shutdown, wait=True)
        runner = self.runner_module.WorkflowRunner(
            memory,
            adapter,
            runner_id="runner-1",
            tick_file=self.tick_path,
            concurrency_limiter=self.runner_module.ConcurrencyLimiter(max_workers),
            executor=executor,
        )
        self.addCleanup(adapter.release.set)
        return runner

    def test_long_dispatches_run_in_parallel_and_tick_returns(self):
        memory = FakeMemoryClient(ready=[_task("t1", "wf-1"), _task("t2", "wf-2")])
        adapter = BlockingAdapter()
        runner = self._runner(memory, adapter)

        runner.tick()

        self.assertTrue(_wait_for(lambda: len(adapter.started) == 2))
        self.assertEqual(memory.completed, [])
        self.assertTrue(self.tick_path.exists())
        adapter.release.set()
        self.assertTrue(_wait_for(lambda: len(memory.completed) == 2))

    def test_only_one_task_per_workflow_is_in_flight(self):
        memory = FakeMemoryClient(ready=[_task("t1", "wf-1"), _task("t2", "wf-1")])
        adapter = BlockingAdapter()
        runner = self._runner(memory, adapter)

        runner.tick()
        self.assertTrue(_wait_for(lambda: len(adapter.started) == 1))
        memory.ready = [_task("t2", "wf-1")]
        runner.tick()

        self.assertEqual([c[0]["id"] for c in memory.claims], ["t1"])
        adapter.release.set()
        self.assertTrue(_wait_for(lambda: len(memory.completed) == 1))
        runner.tick()
        self.assertTrue(_wait_for(lambda: [c[0]["id"] for c in memory.claims] == ["t1", "t2"]))

    def test_limit_caps_parallel_dispatches(self):
        memory = FakeMemoryClient(ready=[_task("t1", "wf-1"), _task("t2", "wf-2")])
        adapter = BlockingAdapter()
        runner = self._runner(memory, adapter, max_workers=1)

        runner.tick()

        self.assertTrue(_wait_for(lambda: len(adapter.started) == 1))
        self.assertEqual(len(memory.claims), 1)

    def test_stale_sweep_skips_tasks_this_runner_is_still_running(self):
        memory = FakeMemoryClient(ready=[_task("t1", "wf-1")])
        adapter = BlockingAdapter()
        runner = self._runner(memory, adapter)
        runner.tick()
        self.assertTrue(_wait_for(lambda: len(adapter.started) == 1))

        memory.ready = []
        memory.stale = [{"id": "t1"}, {"id": "orphan"}]
        runner.tick()

        self.assertEqual(memory.blocked, [("orphan", "task lease expired")])

    def test_adapter_failure_in_a_worker_still_recovers_the_task(self):
        task = {**_task("t1", "wf-1"), "attempt": 0, "max_retries": 2}
        memory = FakeMemoryClient(ready=[task])

        class RaisingAdapter(BlockingAdapter):
            def dispatch(self, payload):
                raise RuntimeError("boom")

        adapter = RaisingAdapter()
        runner = self._runner(memory, adapter)
        runner.tick()

        self.assertTrue(_wait_for(lambda: len(memory.retried) == 1))
        self.assertIn("boom", memory.blocked[0][1])
        memory.ready = [task]
        runner.tick()
        self.assertTrue(_wait_for(lambda: len(memory.claims) == 2))


class GitHubWaitRetryTests(unittest.TestCase):
    def _dispatch(self, error):
        runner_module = load_runner()
        stage = runner_module.workflow_definitions.Stage(
            name="await_pr_checks",
            kind="task",
            agent_id="orchestrator",
            allowed_agents=["orchestrator"],
            allowed_external_effects=[],
            depends_on=[],
            required_artifacts=[],
            skill_hints=[],
            retry={"max_retries": 3, "timeout_seconds": 60},
            wait_external={
                "event_type": "github.pr_checks",
                "pr_checks": {"pr_url": "https://github.com/o/r/pull/1"},
            },
        )
        skeleton = runner_module.workflow_definitions.WorkflowSkeleton(
            workflow_type="custom_flow",
            definition_version="1",
            stages=[stage],
        )
        task = {
            "id": "task-1",
            "workflow_id": "workflow-1",
            "workflow_type": "custom_flow",
            "step_name": "await_pr_checks",
            "agent_id": "orchestrator",
            "attempt": 0,
            "max_retries": 3,
            "input": {},
        }
        memory = FakeMemoryClient()
        runner = runner_module.WorkflowRunner(memory, FakeAdapter({}), skeletons={"custom_flow": skeleton})
        with patch(
            "ts_harness.workflows.merge_readiness.prepare_checks_wait",
            side_effect=error,
        ):
            runner._dispatch_task(task)
        return memory

    def test_server_error_retries_the_checks_wait(self):
        from ts_harness.integrations.github_client import GitHubError

        memory = self._dispatch(GitHubError(502, "bad gateway"))
        self.assertEqual(len(memory.retried), 1)
        self.assertIn("wait_external setup failed", memory.blocked[-1][1])

    def test_forbidden_does_not_retry_and_names_the_permission(self):
        from ts_harness.integrations.github_client import GitHubError

        memory = self._dispatch(GitHubError(403, "Resource not accessible"))
        self.assertEqual(memory.retried, [])
        self.assertIn("actions read", memory.blocked[-1][1])


class QaReviewEvidenceTests(unittest.TestCase):
    def test_qa_without_files_receives_the_latest_review_diff(self):
        runner_module = load_runner()
        older = {
            "step_name": "review_precheck",
            "status": "succeeded",
            "created_at": "2026-10-02T01:00:00Z",
            "output": {"files": [{"filename": "old.py", "patch": "old"}]},
        }
        newer = {
            "step_name": "review_precheck",
            "status": "succeeded",
            "created_at": "2026-10-02T02:00:00Z",
            "output": {
                "files": [{"filename": "app/bin_label.py", "patch": "+def bin_label"}],
                "diff_stats": {"files": 1},
                "prs": [{"pr_url": "https://github.com/o/r/pull/24"}],
            },
        }
        evidence = runner_module.qa_review_evidence(
            {"step_name": "qa_gate", "input": {"pr_url": "https://github.com/o/r/pull/24"}},
            [older, newer],
        )
        self.assertEqual(evidence["files"][0]["filename"], "app/bin_label.py")
        self.assertEqual(evidence["diff_stats"], {"files": 1})

    def test_qa_keeps_files_already_on_the_task(self):
        runner_module = load_runner()
        evidence = runner_module.qa_review_evidence(
            {"step_name": "qa_gate", "input": {"files": [{"filename": "kept.py"}]}},
            [
                {
                    "step_name": "review_precheck",
                    "status": "succeeded",
                    "created_at": "2026-10-02T02:00:00Z",
                    "output": {"files": [{"filename": "other.py"}]},
                }
            ],
        )
        self.assertEqual(evidence, {})


class LeaderLeaseTests(unittest.TestCase):
    def test_only_the_current_lease_holder_leads(self):
        runner_module = load_runner()

        class LeaseMemory:
            def __init__(self):
                self.row = None

            def acquire_leader(self, runner_id, ttl_seconds):
                now = time.time()
                if self.row is None or self.row[0] == runner_id or self.row[1] < now:
                    self.row = (runner_id, now + ttl_seconds)
                    return True
                return False

        memory = LeaseMemory()
        first = runner_module.LeaderElection("runner-a", memory=memory, ttl_seconds=30)
        second = runner_module.LeaderElection("runner-b", memory=memory, ttl_seconds=30)

        self.assertTrue(first.try_become_leader())
        self.assertFalse(second.try_become_leader())
        memory.row = (memory.row[0], time.time() - 1)
        self.assertTrue(second.try_become_leader())

    def test_redis_errors_fail_closed(self):
        runner_module = load_runner()

        class Boom:
            def set(self, *args, **kwargs):
                raise RuntimeError("redis down")

            def get(self, *args, **kwargs):
                raise RuntimeError("redis down")

        election = runner_module.LeaderElection("runner-a", redis_url="redis://localhost:6379")
        election._redis = Boom()
        election._redis_required = True
        self.assertFalse(election.try_become_leader())

    def test_memory_errors_fail_closed(self):
        runner_module = load_runner()

        class Boom:
            def acquire_leader(self, runner_id, ttl_seconds):
                raise RuntimeError("memory down")

        election = runner_module.LeaderElection("runner-a", memory=Boom())
        self.assertFalse(election.try_become_leader())

    def test_stop_requested_dispatches_the_claimed_task_and_stops(self):
        runner_module = load_runner()
        tasks = [
            {
                "id": "task-1",
                "workflow_id": "workflow-1",
                "workflow_type": "prd_request",
                "step_name": "draft_prd",
                "agent_id": "pm-agent",
                "input": {},
            },
            {
                "id": "task-2",
                "workflow_id": "workflow-1",
                "workflow_type": "prd_request",
                "step_name": "draft_prd",
                "agent_id": "pm-agent",
                "input": {},
            },
        ]
        memory = FakeMemoryClient(ready=tasks)
        adapter = FakeAdapter({"status": "succeeded", "output": {}, "runtime_task_id": "rt-1"})
        workflow_runner = runner_module.WorkflowRunner(memory, adapter, runner_id="runner-1")
        original = memory.claim_task

        def claim(task, locked_by, lease_seconds):
            ok = original(task, locked_by, lease_seconds)
            workflow_runner.stop_requested.set()
            return ok

        memory.claim_task = claim
        workflow_runner.tick()

        self.assertEqual([item[0]["id"] for item in memory.claims], ["task-1"])
        self.assertEqual(len(adapter.payloads), 1)

    def test_run_loop_returns_without_sleeping_when_already_stopped(self):
        runner_module = load_runner()
        memory = FakeMemoryClient()
        adapter = FakeAdapter({})
        workflow_runner = runner_module.WorkflowRunner(memory, adapter, runner_id="runner-1")
        stop_requested = threading.Event()
        stop_requested.set()
        started = time.monotonic()
        with redirect_stdout(io.StringIO()) as captured:
            runner_module.run_until_stopped(
                workflow_runner,
                interval=30,
                stop_requested=stop_requested,
            )
        self.assertLess(time.monotonic() - started, 1)
        self.assertIn("runner_shutdown", captured.getvalue())


class CodingJobMemoryClient(FakeMemoryClient):
    def __init__(self, ready=None, jobs=None):
        super().__init__(ready)
        self.jobs = list(jobs or [])
        self.created_jobs = []
        self.consumed = []

    def list_coding_jobs(self, *, task_id=None, unconsumed=False, limit=50, **_):
        return [j for j in self.jobs if j.get("task_id") == task_id]

    def create_coding_job(self, **kwargs):
        self.created_jobs.append(kwargs)
        return {"id": "job-new", "status": "queued", "created": True}

    def consume_coding_job(self, job_id):
        self.consumed.append(job_id)

    def task_program(self, task_id):
        return {"task_id": task_id, "program_id": "prog-1"}


class CodingJobDispatchTests(unittest.TestCase):
    PR = "https://github.com/acme/app/pull/7"

    def setUp(self):
        env = patch.dict("os.environ", {"TS_HARNESS_HOOK_ENGINE": "false", "CODING_JOBS_ENABLED": "true"})
        env.start()
        self.addCleanup(env.stop)
        self.runner_module = load_runner()
        self.skeletons = self.runner_module.load_workflow_skeletons(REPO_ROOT / "config" / "workflows")

    def task(self, **extra):
        return {
            "id": "task-1",
            "workflow_id": "wf-1",
            "workflow_type": "deliver_work_order",
            "step_name": "deliver_work_order",
            "agent_id": "dev-agent",
            "input": {"work_order_id": "WO-1", "work_order_body": "Do it", "repo_url": "https://github.com/acme/app"},
            "attempt": 0,
            "max_retries": 2,
            **extra,
        }

    def run_tick(self, memory, adapter=None):
        adapter = adapter or FakeAdapter({"status": "succeeded", "output": {"summary": "agent ran"}})
        runner = self.runner_module.WorkflowRunner(
            memory, adapter, runner_id="runner-1", skeletons={"deliver_work_order": self.skeletons["deliver_work_order"]}
        )
        runner.tick()
        return adapter

    def test_flag_off_keeps_the_agent_path(self):
        memory = CodingJobMemoryClient(ready=[self.task()])
        with patch.dict("os.environ", {"CODING_JOBS_ENABLED": "false"}):
            adapter = self.run_tick(memory)
        self.assertEqual(len(adapter.payloads), 1)
        self.assertEqual(memory.created_jobs, [])

    def test_queues_a_job_and_parks_the_task_without_dispatching(self):
        memory = CodingJobMemoryClient(ready=[self.task()])
        with patch("ts_harness.workflows.coding_job_stage.stage_spec", return_value={"repo_url": "https://github.com/acme/app"}) as spec:
            adapter = self.run_tick(memory)
        self.assertEqual(adapter.payloads, [])
        self.assertEqual(spec.call_args.kwargs["program_id"], "prog-1")
        self.assertEqual(memory.created_jobs[0]["task_id"], "task-1")
        self.assertEqual(memory.created_jobs[0]["program_id"], "prog-1")
        self.assertEqual(memory.waits, [("task-1", "job-new", "coding.finished")])
        self.assertEqual(memory.blocked, [("task-1", "awaiting_external:coding.finished")])

    def test_an_active_job_is_reparked_not_duplicated(self):
        memory = CodingJobMemoryClient(ready=[self.task()], jobs=[{"id": "job-1", "task_id": "task-1", "status": "running"}])
        self.run_tick(memory)
        self.assertEqual(memory.created_jobs, [])
        self.assertEqual(memory.waits, [("task-1", "job-1", "coding.finished")])

    def test_bad_request_blocks_with_the_reason(self):
        from ts_harness.implementation.coding_job import CodingRequestError

        memory = CodingJobMemoryClient(ready=[self.task()])
        with patch("ts_harness.workflows.coding_job_stage.stage_spec", side_effect=CodingRequestError("repo not allowed")):
            self.run_tick(memory)
        self.assertEqual(memory.created_jobs, [])
        self.assertEqual(memory.blocked[0], ("task-1", "repo not allowed"))

    def test_succeeded_job_completes_the_task_and_is_consumed(self):
        result = {"ok": True, "work_order_id": "WO-1", "pr_url": self.PR, "branch": "wo-1", "head_sha": "abc", "security": {"secret_scan": {}}}
        memory = CodingJobMemoryClient(ready=[self.task()], jobs=[{"id": "job-1", "task_id": "task-1", "status": "succeeded", "result": result}])
        adapter = self.run_tick(memory)
        self.assertEqual(adapter.payloads, [])
        (task_id, output), = memory.completed
        self.assertEqual((task_id, output["pr_url"], output["head_sha"], output["branch"]), ("task-1", self.PR, "abc", "wo-1"))
        self.assertEqual(memory.consumed, ["job-1"])

    def test_failed_job_blocks_with_the_contract_error_and_is_consumed(self):
        result = {"ok": False, "work_order_id": "WO-1", "error": "tests failed"}
        memory = CodingJobMemoryClient(ready=[self.task()], jobs=[{"id": "job-1", "task_id": "task-1", "status": "failed", "result": result}])
        self.run_tick(memory)
        self.assertEqual(memory.completed, [])
        self.assertIn("run_coding_backend failed: tests failed", memory.blocked[0][1])
        self.assertEqual(memory.retried, [])
        self.assertEqual(memory.consumed, ["job-1"])

    def test_lost_job_is_retried(self):
        memory = CodingJobMemoryClient(
            ready=[self.task()],
            jobs=[{"id": "job-1", "task_id": "task-1", "status": "lost", "result": {"ok": False, "error": "coding job lost: no heartbeat for 5 minutes"}}],
        )
        self.run_tick(memory)
        self.assertEqual(memory.retried[0][0], "task-1")
        self.assertIn("coding job lost: no heartbeat", memory.retried[0][1])
        self.assertEqual(memory.consumed, ["job-1"])


if __name__ == "__main__":
    unittest.main()
