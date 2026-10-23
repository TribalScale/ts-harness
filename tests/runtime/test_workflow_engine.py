"""Tests for WorkflowEngine dispatch and hook integration."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ts_harness.workflows.engine import WorkflowEngine
from ts_harness.workflows.hooks import build_default_registry
from ts_harness.workflows.materialize import load_skeleton
from ts_harness.agent.sessions import SessionRecord


class WorkflowEngineTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.engine = WorkflowEngine(
            client=self.client,
            registry=build_default_registry(),
        )
        self.skeleton = load_skeleton("prd_request")

    def test_resume_external_blocked_runs_on_block(self):
        task = {"id": "w1", "workflow_id": "wf-1", "step_name": "await_human_merge", "status": "blocked"}
        self.client.get_workflow.return_value = {"workflow": {"workflow_type": "deliver_work_order"}, "tasks": [task]}
        with (
            patch(
                "ts_harness.workflows.hooks.wait_external.resume",
                return_value={"status": "blocked", "task_id": "w1", "reason": "external_blocked:x base_advanced"},
            ),
            patch.object(self.engine, "on_task_transition", return_value={"status": "revising"}) as transition,
        ):
            result = self.engine.resume_external("wf-1", event_type="x", payload={}, actor="a")
        self.assertEqual(result["on_block"], {"status": "revising"})
        args, kwargs = transition.call_args
        self.assertEqual(args[1], "blocked")
        self.assertIn("base_advanced", kwargs["error"])

    def test_block_case_prefixes_follow_the_pinned_definition(self):
        current = load_skeleton("deliver_work_order").definition_version
        for version, expected in (
            (current, ["external_blocked:github.pr_changes_requested", "external_blocked:github.pr_closed"]),
            ("0.0.1-missing", []),
        ):
            with self.subTest(version=version):
                task = {"id": "w1", "step_name": "await_human_merge", "status": "blocked", "definition_version": version}
                self.client.get_workflow.return_value = {"workflow": {"workflow_type": "deliver_work_order"}, "tasks": [task]}
                self.assertEqual(self.engine.block_case_prefixes("wf-1", "w1"), expected)
        self.assertEqual(self.engine.block_case_prefixes("wf-1", "other"), [])

    def test_block_wait_blocks_and_runs_on_block(self):
        task = {"id": "w1", "workflow_id": "wf-1", "step_name": "await_human_merge", "status": "blocked"}
        self.client.get_workflow.return_value = {"workflow": {}, "tasks": [task]}
        with patch.object(self.engine, "on_task_transition", return_value={"status": "revising"}) as transition:
            result = self.engine.block_wait("wf-1", "w1", "base_advanced", output={"stale": [1]})
        self.client.block_task_api.assert_called_once_with("w1", "base_advanced", output={"stale": [1]}, actor="workflow-runner")
        self.assertEqual(result, {"status": "revising"})
        self.assertEqual(transition.call_args.kwargs["dispatch_result"], {"output": {"stale": [1]}})

    def test_revision_cause_messages(self):
        from ts_harness.workflows.engine import _qa_feedback_text, _revision_cause

        self.assertEqual(_revision_cause("barrier_blocked: stale_base: x")[0], "The base branch moved; refreshing the PR")
        self.assertEqual(_revision_cause("barrier_blocked: code_review_blocked: 2")[0], "Code review blocked the PR")
        self.assertEqual(_revision_cause(None)[0], "QA rejected the PR")
        self.assertEqual(
            _revision_cause("external_blocked:github.pr_changes_requested by alice"),
            ("alice requested changes", "Human-requested revision failed"),
        )
        self.assertEqual(_revision_cause("external_blocked:github.pr_closed by bob")[0], "bob closed the PR without merging")
        self.assertEqual(_revision_cause("external_blocked:github.pr_closed")[0], "A reviewer closed the PR without merging")
        self.assertEqual(_qa_feedback_text({}, {"output": {"review_feedback": "fix it"}}, None), "fix it")

    def test_handle_reject_falls_back_without_on_reject(self):
        gate = {
            "id": "gate-1",
            "workflow_type": "implement_ticket",
            "step_name": "implement",
            "approval_required": True,
        }
        self.client.reject_task.return_value = {
            "id": "gate-1",
            "approval_status": "rejected",
        }
        result = self.engine.handle_reject(
            workflow_id="wf-1",
            gate_task=gate,
            comments="no",
            actor="orchestrator",
        )
        self.client.reject_task.assert_called_once_with(
            "gate-1",
            actor="orchestrator",
        )
        self.assertEqual(result["approval_status"], "rejected")

    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_handle_reject_revise_on_reject(self, mock_bulk):
        mock_bulk.side_effect = [["rev-1"], ["gate-2"]]
        self.client.get_workflow.return_value = {
            "workflow": {
                "workflow_type": "prd_request",
                "source_channel": "slack:C:1",
            },
            "tasks": [
                {
                    "id": "draft-1",
                    "step_name": "draft_prd",
                    "status": "succeeded",
                    "output": {"summary": "draft"},
                    "created_at": "2026-01-01",
                },
                {
                    "id": "gate-1",
                    "step_name": "approve_prd",
                    "approval_status": "pending",
                    "status": "pending",
                    "workflow_type": "prd_request",
                },
            ],
        }
        gate = {
            "id": "gate-1",
            "workflow_type": "prd_request",
            "step_name": "approve_prd",
            "approval_status": "pending",
        }
        result = self.engine.handle_reject(
            workflow_id="wf-1",
            gate_task=gate,
            comments="add mobile",
            actor="orchestrator",
        )
        self.client.reject_task.assert_called_once()
        self.assertEqual(result["revise_task_id"], "rev-1")
        self.assertEqual(result["re_gate_task_ids"], ["gate-2"])

    @patch("ts_harness.workflows.engine.hook_engine_enabled", return_value=True)
    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_blocked_qa_gate_starts_revise_loop(self, mock_bulk, _enabled):
        mock_bulk.side_effect = [["deliver-2"], ["checks-2"], ["precheck-2"], ["review-2"], ["finalize-2"], ["qa-2"], ["fresh-2"], ["merge-2"]]
        output = {
            "status": "failed",
            "result": {
                "summary": "No evidence of implementation or testing",
                "prs": [
                    {
                        "pr_url": "https://github.com/org/app/pull/8",
                        "work_item_ref": "WO-003",
                        "status": "failed",
                        "defects": ["missing tests"],
                    }
                ],
            },
        }
        task = {
            "id": "qa-1",
            "workflow_id": "wf-1",
            "workflow_type": "deliver_work_order",
            "step_name": "qa_gate",
            "status": "blocked",
            "tenant_id": "default",
            "output": output,
        }
        deliver = {
            "id": "deliver-1",
            "step_name": "deliver_work_order",
            "status": "succeeded",
            "input": {
                "work_order_id": "WO-003",
                "work_order_body": "# WO-003",
            },
            "output": {
                "pr_url": "https://github.com/org/app/pull/8",
                "work_order_id": "WO-003",
            },
            "created_at": "2026-01-01",
        }
        self.client.get_workflow.return_value = {
            "workflow": {
                "id": "wf-1",
                "workflow_type": "deliver_work_order",
                "source_channel": "slack:C:1",
            },
            "tasks": [
                deliver,
                task,
                {
                    "id": "merge-1",
                    "step_name": "await_human_merge",
                    "status": "pending",
                    "created_at": "2026-01-02",
                },
            ],
        }
        self.client.cancel_task.return_value = {"status": "cancelled"}

        result = self.engine.on_task_transition(
            task,
            "blocked",
            dispatch_result={"output": output},
            error="QA failed",
        )

        self.assertEqual(result.get("status"), "revising")
        self.assertEqual(result.get("revise_task_id"), "deliver-2")
        self.client.reject_task.assert_not_called()

    def test_get_status_delegates_to_client(self):
        self.client.get_workflow_status.return_value = {
            "workflow_id": "wf-1",
            "status": "running",
        }
        status = self.engine.get_status("wf-1")
        self.assertEqual(status["workflow_id"], "wf-1")

    def test_reload_if_changed_keeps_last_good_on_malformed_definition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtins = root / "builtins"
            managed_active = root / "managed" / "active"
            builtins.mkdir()
            managed_active.mkdir(parents=True)
            stage = {"name": "start", "kind": "task", "agent_id": "a"}
            (builtins / "custom_flow.json").write_text(
                json.dumps(
                    {
                        "workflow_type": "custom_flow",
                        "definition_version": "builtin",
                        "stages": [stage],
                    }
                ),
                encoding="utf-8",
            )
            managed_path = managed_active / "custom_flow.json"
            manifest_path = managed_active / "manifest.json"
            managed_path.write_text(
                json.dumps(
                    {
                        "workflow_type": "custom_flow",
                        "definition_version": "managed-1",
                        "stages": [stage],
                    }
                ),
                encoding="utf-8",
            )
            manifest_path.write_text(
                json.dumps({"generation": 1, "workflows": {}}),
                encoding="utf-8",
            )

            env = {
                "TS_HARNESS_MANAGED_WORKFLOWS_DIR": str(managed_active),
                "TS_HARNESS_WORKFLOW_DEFINITION_STORE": str(
                    root / "managed"
                ),
            }
            with patch.dict("os.environ", env):
                engine = WorkflowEngine(
                    client=self.client,
                    registry=build_default_registry(),
                    skeleton_dir=builtins,
                )
                self.assertEqual(
                    engine.skeleton("custom_flow").definition_version,
                    "managed-1",
                )

                managed_path.write_text(
                    json.dumps(
                        {
                            "workflow_type": "custom_flow",
                            "definition_version": "managed-2",
                            "stages": [stage],
                        }
                    ),
                    encoding="utf-8",
                )
                manifest_path.write_text(
                    json.dumps({"generation": 2, "workflows": {}}),
                    encoding="utf-8",
                )
                self.assertTrue(engine.reload_if_changed())
                self.assertEqual(
                    engine.skeleton("custom_flow").definition_version,
                    "managed-2",
                )

                self.assertTrue(engine.reload_if_changed())

                managed_path.write_text("{not-json", encoding="utf-8")
                manifest_path.write_text(
                    json.dumps({"generation": 3, "workflows": {}}),
                    encoding="utf-8",
                )
                self.assertFalse(engine.reload_if_changed())
                self.assertEqual(
                    engine.skeleton("custom_flow").definition_version,
                    "managed-2",
                )

    def test_task_transition_uses_persisted_version_after_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtins = root / "builtins"
            managed = root / "managed"
            active = managed / "active"
            history = managed / "history" / "custom_flow"
            builtins.mkdir()
            active.mkdir(parents=True)
            history.mkdir(parents=True)

            def skeleton_payload(version: str, hook: str) -> dict:
                return {
                    "workflow_type": "custom_flow",
                    "definition_version": version,
                    "stages": [
                        {
                            "name": "review",
                            "kind": "task",
                            "agent_id": "agent",
                            "on_complete": {"hook": hook},
                        }
                    ],
                }

            v1 = skeleton_payload("v1", "v1_hook")
            v2 = skeleton_payload("v2", "v2_hook")
            (builtins / "custom_flow.json").write_text(
                json.dumps(v1),
                encoding="utf-8",
            )
            (active / "custom_flow.json").write_text(
                json.dumps(v2),
                encoding="utf-8",
            )
            (active / "manifest.json").write_text(
                json.dumps({"generation": 2}),
                encoding="utf-8",
            )
            (history / "v1-1.json").write_text(
                json.dumps(v1),
                encoding="utf-8",
            )

            class RecordingRegistry:
                def __init__(self):
                    self.hooks = []

                def invoke(self, hook, ctx):
                    self.hooks.append(
                        (
                            hook,
                            ctx.skeleton.definition_version,
                            ctx.stage.name if ctx.stage else None,
                        )
                    )
                    if hook == "v1_hook":
                        return {"status": "used-v1"}
                    if hook == "v2_hook":
                        return {"status": "used-v2"}
                    return None

            task = {
                "id": "task-1",
                "workflow_id": "wf-1",
                "workflow_type": "custom_flow",
                "step_name": "review",
                "definition_version": "v1",
                "tenant_id": "default",
            }
            self.client.get_workflow.return_value = {
                "workflow": {
                    "id": "wf-1",
                    "workflow_type": "custom_flow",
                    "definition_version": "v1",
                },
                "tasks": [task],
            }
            registry = RecordingRegistry()

            with patch.dict(
                "os.environ",
                {
                    "TS_HARNESS_MANAGED_WORKFLOWS_DIR": str(active),
                    "TS_HARNESS_WORKFLOW_DEFINITION_STORE": str(managed),
                },
            ):
                engine = WorkflowEngine(
                    client=self.client,
                    registry=registry,
                    skeleton_dir=builtins,
                )

                result = engine.on_task_transition(
                    task,
                    "succeeded",
                    dispatch_result={"output": {}},
                )

            self.assertEqual(result, {"status": "used-v1"})
            self.assertEqual(registry.hooks, [("v1_hook", "v1", "review")])

    def test_definition_version_prefers_unanimous_task_versions_over_workflow_default(self):
        payload = {
            "workflow": {
                "id": "wf-1",
                "workflow_type": "custom_flow",
                "definition_version": "1.0.0",
            },
            "tasks": [
                {"id": "task-1", "definition_version": "managed-2"},
                {"id": "task-2", "definition_version": "managed-2"},
            ],
        }

        version = self.engine._definition_version_from_state(
            task={"id": "task-3"},
            payload=payload,
        )

        self.assertEqual(version, "managed-2")

    def test_skeleton_resolves_colon_version_from_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtins = root / "builtins"
            managed = root / "managed"
            history = managed / "history" / "custom_flow"
            builtins.mkdir()
            history.mkdir(parents=True)
            current = {
                "workflow_type": "custom_flow",
                "definition_version": "current",
                "stages": [{"name": "start", "kind": "task", "agent_id": "agent"}],
            }
            historical = {
                **current,
                "definition_version": "release:2026.08",
            }
            (builtins / "custom_flow.json").write_text(
                json.dumps(current),
                encoding="utf-8",
            )
            (history / "release_2026.08-rev.json").write_text(
                json.dumps(historical),
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ",
                {"TS_HARNESS_WORKFLOW_DEFINITION_STORE": str(managed)},
            ):
                engine = WorkflowEngine(
                    client=self.client,
                    registry=build_default_registry(),
                    skeleton_dir=builtins,
                )
                skeleton = engine.skeleton(
                    "custom_flow",
                    definition_version="release:2026.08",
                )

            self.assertEqual(skeleton.definition_version, "release:2026.08")

    def test_builtin_snapshot_preserves_old_version_after_upgrade(self):
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

            with patch.dict(
                "os.environ",
                {"TS_HARNESS_WORKFLOW_DEFINITION_STORE": str(managed)},
            ):
                engine = WorkflowEngine(
                    client=self.client,
                    registry=build_default_registry(),
                    skeleton_dir=builtins,
                )
                self.assertEqual(
                    engine.skeleton("custom_flow").definition_version,
                    "builtin:1",
                )

                (builtins / "custom_flow.json").write_text(
                    json.dumps(payload("builtin:2", "v2.event")),
                    encoding="utf-8",
                )
                self.assertTrue(engine.reload_definitions())

                old = engine.skeleton(
                    "custom_flow",
                    definition_version="builtin:1",
                )

            self.assertEqual(
                old.stage("await_external").wait_external["event_type"],
                "v1.event",
            )

    def test_builtin_missing_history_after_upgrade_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtins = root / "builtins"
            managed = root / "managed"
            builtins.mkdir()
            managed.mkdir()
            (builtins / "custom_flow.json").write_text(
                json.dumps(
                    {
                        "workflow_type": "custom_flow",
                        "definition_version": "builtin:2",
                        "stages": [
                            {
                                "name": "start",
                                "kind": "task",
                                "agent_id": "orchestrator",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ",
                {"TS_HARNESS_WORKFLOW_DEFINITION_STORE": str(managed)},
            ):
                engine = WorkflowEngine(
                    client=self.client,
                    registry=build_default_registry(),
                    skeleton_dir=builtins,
                )
                with self.assertRaises(LookupError):
                    engine.skeleton("custom_flow", definition_version="builtin:1")

    def test_load_skeleton_fails_closed_when_manifest_history_missing(self):

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtins = root / "builtins"
            managed = root / "managed"
            active = managed / "active"
            builtins.mkdir()
            active.mkdir(parents=True)
            builtin = {
                "workflow_type": "custom_flow",
                "definition_version": "builtin",
                "stages": [{"name": "start", "kind": "task", "agent_id": "builtin"}],
            }
            stale = {
                "workflow_type": "custom_flow",
                "definition_version": "stale",
                "stages": [{"name": "start", "kind": "task", "agent_id": "stale"}],
            }
            (builtins / "custom_flow.json").write_text(
                json.dumps(builtin),
                encoding="utf-8",
            )
            (active / "custom_flow.json").write_text(
                json.dumps(stale),
                encoding="utf-8",
            )
            (active / "manifest.json").write_text(
                json.dumps(
                    {
                        "generation": 1,
                        "workflows": {
                            "custom_flow": {
                                "definition_version": "managed-1",
                                "revision": "missing-revision",
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
                with self.assertRaises(FileNotFoundError):
                    load_skeleton("custom_flow", builtins)

    def test_load_skeleton_uses_manifest_history_when_active_file_missing(self):
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
                "definition_version": "managed-1",
                "stages": [{"name": "start", "kind": "task", "agent_id": "managed"}],
            }
            revision = definition_content_revision(managed_payload)
            (builtins / "custom_flow.json").write_text(
                json.dumps(builtin),
                encoding="utf-8",
            )
            (history / history_filename("managed-1", revision)).write_text(
                json.dumps(managed_payload),
                encoding="utf-8",
            )
            (active / "manifest.json").write_text(
                json.dumps(
                    {
                        "generation": 1,
                        "workflows": {
                            "custom_flow": {
                                "definition_version": "managed-1",
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
                skeleton = load_skeleton("custom_flow", builtins)

            self.assertEqual(skeleton.definition_version, "managed-1")
            self.assertEqual(skeleton.stage("start").agent_id, "managed")

    def test_bridge_workflow_uses_source_workflow_definition_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtins = root / "builtins"
            managed = root / "managed"
            history = managed / "history" / "custom_flow"
            builtins.mkdir()
            history.mkdir(parents=True)

            def payload(version: str, target: str) -> dict:
                return {
                    "workflow_type": "custom_flow",
                    "definition_version": version,
                    "bridge": {"target_workflow_type": target},
                    "stages": [{"name": "start", "kind": "task", "agent_id": "agent"}],
                }

            v1 = payload("v1", "legacy_delivery")
            v2 = payload("v2", "current_delivery")
            (builtins / "custom_flow.json").write_text(
                json.dumps(v2),
                encoding="utf-8",
            )
            (history / "v1-rev.json").write_text(json.dumps(v1), encoding="utf-8")
            self.client.get_workflow.return_value = {
                "workflow": {
                    "id": "wf-1",
                    "workflow_type": "custom_flow",
                    "definition_version": "v1",
                },
                "tasks": [],
            }

            class RecordingRegistry:
                def __init__(self):
                    self.calls = []

                def invoke(self, hook, ctx):
                    self.calls.append(
                        (
                            hook,
                            ctx.skeleton.definition_version,
                            ctx.extras.get("target_workflow_type"),
                        )
                    )
                    return {"ok": True}

            registry = RecordingRegistry()
            session = SessionRecord(
                session_id="session-1",
                routing_key="route",
                tenant_id="default",
                agent_id="agent",
                source="slack",
                metadata={
                    "workflow_type": "custom_flow",
                    "source_workflow_id": "wf-1",
                },
            )

            with patch.dict(
                "os.environ",
                {"TS_HARNESS_WORKFLOW_DEFINITION_STORE": str(managed)},
            ):
                engine = WorkflowEngine(
                    client=self.client,
                    registry=registry,
                    skeleton_dir=builtins,
                )
                engine.bridge_workflow(session=session, actor="agent")

            self.assertEqual(
                registry.calls,
                [("bridge_workflow", "v1", "legacy_delivery")],
            )


if __name__ == "__main__":
    unittest.main()
