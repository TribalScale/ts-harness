"""Workflow engine package – re-exports runtime hook engine for backward compatibility."""

from ts_harness.workflows.engine import WorkflowEngine, get_workflow_engine
from ts_harness.workflows.hooks.expand_on_complete import handle as expand_handle


class ExpandHandler:
    """Backward-compatible shim; use runtime hooks directly."""

    def __init__(self, memory_client):
        self._memory = memory_client

    def handle(self, task, stage, skeleton, proposed_tasks, existing_step_names=None):
        from ts_harness.workflows.registry import HookContext

        ctx = HookContext(
            workflow_id=str(task.get("workflow_id")),
            workflow_type=str(task.get("workflow_type")),
            tenant_id=str(task.get("tenant_id") or "default"),
            task=task,
            stage=stage,
            skeleton=skeleton,
            actor="workflow-runner",
            memory_client=self._memory,
            dispatch_result={"proposed_tasks": proposed_tasks},
        )
        return expand_handle(ctx)

    def needs_approval(self, stage):
        return bool(stage.plan_approval_required)


class CompensateHandler:
    """Backward-compatible shim."""

    @staticmethod
    def is_compensatable(stage):
        return stage.kind == "compensate"

    def build_dispatch_payload(self, task, stage, failed_task=None):
        payload = {
            "workflow_id": task.get("workflow_id"),
            "task_id": task.get("id"),
            "workflow_type": task.get("workflow_type", "generic"),
            "step_name": task.get("step_name"),
            "agent_id": task.get("agent_id"),
            "tenant_id": task.get("tenant_id", ""),
            "input": {
                **(task.get("input") or {}),
                "_compensate": True,
                "_failed_task": failed_task or {},
            },
            "required_artifacts": stage.required_artifacts,
            "skill_hints": stage.skill_hints,
        }
        if getattr(stage, "tool_allow", None) is not None:
            payload["tool_allow"] = list(stage.tool_allow)
        return payload


__all__ = ["WorkflowEngine", "ExpandHandler", "CompensateHandler", "get_workflow_engine"]
