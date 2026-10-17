"""Config-driven workflow engine with hook registry."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from ts_harness.agent.sessions import SessionRecord
from ts_harness.memory.client import MemoryClient, get_memory_client
from ts_harness.repo_paths import workflows_dir
from ts_harness.workflows.hooks import build_default_registry
from ts_harness.workflows.hooks._helpers import load_workflow_payload
from ts_harness.workflows.materialize import (
    list_skeleton_types,
    load_skeleton,
)
from ts_harness.workflows.presentation import (
    should_present_specialist_output,
)
from ts_harness.workflows.definition_paths import (
    ensure_history_snapshot,
    list_history_paths_for_version,
    safe_component,
)
from ts_harness.workflows.decisions import decision_request_fields
from ts_harness.workflows.registry import HookContext, HookRegistry
from ts_harness.workflows.session_compat import (
    read_source_workflow_id,
    read_workflow_phase,
)
from ts_harness.workflows.workflow_session import (
    derive_workflow_phase,
    get_phase_tracking,
    has_session_tracking,
    phase_for_step_transition,
    set_workflow_phase_for_workflow,
    sync_workflow_session_for_workflow,
)
import workflow_definitions  # noqa: E402

logger = logging.getLogger(__name__)


def hook_engine_enabled() -> bool:
    enabled = os.environ.get("TS_HARNESS_HOOK_ENGINE", "true").lower()
    return enabled in {"1", "true", "yes"}


class WorkflowEngine:
    def __init__(
        self,
        *,
        client: MemoryClient | None = None,
        registry: HookRegistry | None = None,
        skeleton_dir: Path | None = None,
    ):
        self.client = client or get_memory_client()
        self.registry = registry or build_default_registry()
        self._skeletons: dict[str, Any] = {}
        self._versioned_skeletons: dict[tuple[str, str], Any] = {}
        self._skeleton_dir = skeleton_dir or workflows_dir()
        self._definition_manifest_generation: int | None = None
        root = self._skeleton_dir
        if root.exists():
            for workflow_type in list_skeleton_types(root):
                skeleton = load_skeleton(workflow_type, root)
                self._skeletons[skeleton.workflow_type] = skeleton
                self._ensure_history_snapshot(skeleton)
        self._definition_manifest_generation = self._read_manifest_generation()

    def _definition_manifest_path(self) -> Path | None:
        root = os.environ.get(
            "TS_HARNESS_WORKFLOW_DEFINITION_STORE",
            "",
        ).strip()
        if not root:
            return None
        return Path(root) / "active" / "manifest.json"

    def _definition_store_root(self) -> Path | None:
        root = os.environ.get(
            "TS_HARNESS_WORKFLOW_DEFINITION_STORE",
            "",
        ).strip()
        return Path(root) if root else None

    def _read_manifest_generation(self) -> int | None:
        path = self._definition_manifest_path()
        if path is None or not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("workflow definition manifest must be an object")
        return int(data.get("generation") or 0)

    def _managed_active_path(self, workflow_type: str) -> Path | None:
        safe_type = safe_component(workflow_type, field="workflow_type")
        store_root = self._definition_store_root()
        if store_root is not None:
            return store_root / "active" / f"{safe_type}.json"
        managed_root = os.environ.get(
            "TS_HARNESS_MANAGED_WORKFLOWS_DIR",
            "",
        ).strip()
        if managed_root:
            return Path(managed_root) / f"{safe_type}.json"
        return None

    def _has_managed_active(self, workflow_type: str) -> bool:
        path = self._managed_active_path(workflow_type)
        return path is not None and path.exists()

    def _ensure_history_snapshot(self, skeleton: Any) -> None:
        store_root = self._definition_store_root()
        if store_root is None:
            return
        ensure_history_snapshot(store_root, skeleton)

    def reload_definitions(self) -> bool:
        try:
            root = self._skeleton_dir
            skeletons: dict[str, Any] = {}
            for workflow_type in list_skeleton_types(root):
                skeleton = load_skeleton(workflow_type, root)
                skeletons[skeleton.workflow_type] = skeleton
                self._ensure_history_snapshot(skeleton)
            generation = self._read_manifest_generation()
        except Exception:
            logger.exception(
                "workflow definition reload failed; keeping last good set"
            )
            return False
        for workflow_type, skeleton in self._skeletons.items():
            version = str(getattr(skeleton, "definition_version", "") or "").strip()
            if version:
                self._versioned_skeletons.setdefault((workflow_type, version), skeleton)
        self._skeletons = skeletons
        self._definition_manifest_generation = generation
        # Keep program registry in sync for snapshot-driven starts; failures are
        # non-fatal here because in-flight programs use pinned snapshots.
        try:
            from ts_harness.workflows.program_registry import (
                get_program_definition_registry,
            )

            get_program_definition_registry().reload_definitions()
        except Exception:
            logger.exception("program definition reload failed during workflow reload")
        return True

    def reload_if_changed(self) -> bool:
        workflow_ok = True
        try:
            generation = self._read_manifest_generation()
        except Exception:
            logger.exception("workflow definition manifest read failed")
            workflow_ok = False
            generation = self._definition_manifest_generation
        if workflow_ok and generation != self._definition_manifest_generation:
            workflow_ok = self.reload_definitions()
        try:
            from ts_harness.workflows.program_registry import (
                get_program_definition_registry,
            )

            program_ok = get_program_definition_registry().reload_if_changed()
        except Exception:
            logger.exception("program definition reload_if_changed failed")
            program_ok = False
        return bool(workflow_ok and program_ok)

    def skeleton(
        self,
        workflow_type: str,
        definition_version: str | None = None,
    ) -> Any:
        if workflow_type not in self._skeletons:
            self._skeletons[workflow_type] = load_skeleton(workflow_type)
        current = self._skeletons[workflow_type]
        requested = str(definition_version or "").strip()
        if not requested or requested == str(current.definition_version):
            return current

        key = (workflow_type, requested)
        if key in self._versioned_skeletons:
            return self._versioned_skeletons[key]

        store_root = self._definition_store_root()
        if store_root is not None:
            matches = list_history_paths_for_version(
                store_root,
                workflow_type,
                requested,
            )
            if matches:
                skeleton = workflow_definitions.load_skeleton(matches[-1])
                if skeleton.workflow_type != workflow_type:
                    raise ValueError(
                        "workflow history definition type mismatch: "
                        f"requested {workflow_type}, "
                        f"loaded {skeleton.workflow_type}"
                    )
                if str(skeleton.definition_version) != requested:
                    raise ValueError(
                        "workflow history definition version mismatch: "
                        f"requested {requested}, "
                        f"loaded {skeleton.definition_version}"
                    )
                self._versioned_skeletons[key] = skeleton
                return skeleton

        if store_root is not None and not self._has_managed_active(workflow_type):
            try:
                self._ensure_history_snapshot(current)
            except Exception:
                logger.exception(
                    "workflow builtin history snapshot failed: %s",
                    workflow_type,
                )
            matches = list_history_paths_for_version(
                store_root,
                workflow_type,
                requested,
            )
            if matches:
                skeleton = workflow_definitions.load_skeleton(matches[-1])
                if skeleton.workflow_type == workflow_type and str(
                    skeleton.definition_version
                ) == requested:
                    self._versioned_skeletons[key] = skeleton
                    return skeleton

        message = (
            "workflow skeleton version not found: "
            f"workflow_type={workflow_type} definition_version={requested}"
        )
        logger.error(message)
        raise LookupError(message)

    def _workflow_payload(self, workflow_id: str) -> dict[str, Any]:
        if workflow_id and workflow_id not in {"pending"}:
            return self.client.get_workflow(workflow_id)
        return {"workflow": {}, "tasks": []}

    def _definition_version_from_state(
        self,
        *,
        task: dict[str, Any] | None,
        payload: dict[str, Any],
    ) -> str | None:
        if task and task.get("definition_version"):
            return str(task["definition_version"])

        task_id = str((task or {}).get("id") or "")
        tasks = list(payload.get("tasks") or [])
        if task_id:
            for item in tasks:
                if (
                    str(item.get("id") or "") == task_id
                    and item.get("definition_version")
                ):
                    return str(item["definition_version"])

        versions = {
            str(item.get("definition_version"))
            for item in tasks
            if item.get("definition_version")
        }
        if len(versions) == 1:
            return next(iter(versions))

        workflow = payload.get("workflow") or {}
        if isinstance(workflow, dict) and workflow.get("definition_version"):
            return str(workflow["definition_version"])
        return None

    def _skeleton_for_state(
        self,
        workflow_type: str,
        *,
        task: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        workflow_id: str = "",
    ) -> Any:
        state = (
            payload
            if payload is not None
            else self._workflow_payload(workflow_id)
        )
        version = self._definition_version_from_state(task=task, payload=state)
        return self.skeleton(workflow_type, definition_version=version)

    def _context(
        self,
        *,
        workflow_id: str,
        workflow_type: str,
        tenant_id: str,
        task: dict[str, Any] | None = None,
        actor: str,
        reject_comments: str = "",
        runner_event: str = "",
        dispatch_result: dict[str, Any] | None = None,
        extras: dict[str, Any] | None = None,
    ) -> HookContext:
        payload = self._workflow_payload(workflow_id)
        skeleton = self._skeleton_for_state(
            workflow_type,
            task=task,
            payload=payload,
        )
        stage = None
        if task and task.get("step_name"):
            try:
                stage = skeleton.stage(str(task["step_name"]))
            except KeyError:
                stage = None
        return HookContext(
            workflow_id=workflow_id,
            workflow_type=workflow_type,
            tenant_id=tenant_id,
            task=task,
            stage=stage,
            skeleton=skeleton,
            actor=actor,
            memory_client=self.client,
            reject_comments=reject_comments,
            runner_event=runner_event,
            dispatch_result=dispatch_result or {},
            extras=extras or {},
            tasks=list(payload.get("tasks") or []),
            workflow=dict(payload.get("workflow") or {}),
        )

    def _sync_session(
        self,
        workflow_id: str,
        workflow_type: str,
        tenant_id: str,
        *,
        task: dict[str, Any] | None = None,
    ) -> None:
        skeleton = self._skeleton_for_state(
            workflow_type,
            task=task,
            workflow_id=workflow_id,
        )
        if has_session_tracking(skeleton):
            sync_workflow_session_for_workflow(workflow_id, skeleton, tenant_id=tenant_id)

    def start_promotion(
        self,
        *,
        session: SessionRecord,
        workflow_type: str,
        summary: str,
        tenant_id: str,
        binding_key: str,
        requester_actor_id: str | None = None,
        correlation_id: str | None = None,
        transcript_messages: list[dict[str, Any]] | None = None,
        extra_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        skeleton = self.skeleton(workflow_type)
        ctx = self._context(
            workflow_id="pending",
            workflow_type=workflow_type,
            tenant_id=tenant_id,
            actor=requester_actor_id or session.agent_id,
            extras={
                "session": session,
                "summary": summary,
                "binding_key": binding_key,
                "correlation_id": correlation_id,
                "transcript_messages": transcript_messages or [],
                "extra_input": extra_input or {},
            },
        )
        ctx.skeleton = skeleton
        return self.registry.invoke("promote", ctx)

    def handle_reject(
        self,
        *,
        workflow_id: str,
        gate_task: dict[str, Any],
        comments: str,
        actor: str,
        tenant_id: str = "default",
        decision: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        workflow_type = str(gate_task.get("workflow_type") or "")
        payload = self._workflow_payload(workflow_id)
        skeleton = self._skeleton_for_state(
            workflow_type,
            task=gate_task,
            payload=payload,
        )
        stage = skeleton.stage(str(gate_task.get("step_name")))
        on_reject = dict(stage.on_reject or {})
        hook_name = str(on_reject.get("hook") or "")
        if hook_name == "revise_on_reject":
            ctx = self._context(
                workflow_id=workflow_id,
                workflow_type=workflow_type,
                tenant_id=tenant_id,
                task=gate_task,
                actor=actor,
                reject_comments=comments,
                extras={"decision": dict(decision or {})},
            )
            result = self.registry.invoke("revise_on_reject", ctx)
            tracking = get_phase_tracking(skeleton)
            if tracking is not None:
                set_workflow_phase_for_workflow(
                    workflow_id,
                    tracking.drafting_value,
                    skeleton,
                    tenant_id=tenant_id,
                )
            return result
        result = self.client.reject_task(
            str(gate_task["id"]),
            actor=actor,
            **decision_request_fields(decision, payload.get("workflow")),
        )
        self._sync_session(workflow_id, workflow_type, tenant_id, task=gate_task)
        return result

    def handle_approve(
        self,
        *,
        workflow_id: str,
        gate_task: dict[str, Any],
        actor: str,
        tenant_id: str = "default",
        decision: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        workflow_type = str(gate_task.get("workflow_type") or "")
        ctx = self._context(
            workflow_id=workflow_id,
            workflow_type=workflow_type,
            tenant_id=tenant_id,
            task=gate_task,
            actor=actor,
            extras={"decision": dict(decision or {})},
        )
        result = self.registry.invoke("approve_gate", ctx)
        self._maybe_notify(ctx, "succeeded")
        self._sync_session(workflow_id, workflow_type, tenant_id, task=gate_task)
        return result

    def on_task_transition(
        self,
        task: dict[str, Any],
        event: str,
        *,
        dispatch_result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        if not hook_engine_enabled():
            return None

        workflow_id = str(task.get("workflow_id") or "")
        workflow_type = str(task.get("workflow_type") or "")
        tenant_id = str(task.get("tenant_id") or "default")
        if not workflow_type and workflow_id:
            workflow_type = str(
                (
                    self.client.get_workflow(workflow_id).get("workflow") or {}
                ).get("workflow_type")
                or ""
            )
        ctx = self._context(
            workflow_id=workflow_id,
            workflow_type=workflow_type,
            tenant_id=tenant_id,
            task=task,
            actor="workflow-runner",
            runner_event=event,
            dispatch_result=dispatch_result or {},
            extras={"error": error},
        )
        skeleton = ctx.skeleton

        if event == "succeeded":
            step_name = str(task.get("step_name") or "")
            if ctx.stage is not None:
                proposed = (dispatch_result or {}).get("proposed_tasks")
                if proposed and ctx.stage.kind == "expand":
                    expand_result = self.registry.invoke("expand_on_complete", ctx)
                    if expand_result.get("status") == "awaiting_plan_approval":
                        self._maybe_notify(ctx, "awaiting_approval")
                        return expand_result

                on_complete = dict(ctx.stage.on_complete or {})
                hook_name = str(on_complete.get("hook") or "")
                if hook_name == "hydrate_input":
                    downstream = list(on_complete.get("downstream") or [])
                    if downstream:
                        from ts_harness.workflows.hooks import hydrate_input

                        hydrate_input.hydrate_downstream(ctx, downstream)
                elif hook_name:
                    hook_result = self.registry.invoke(hook_name, ctx)
                    if hook_result is not None:
                        return hook_result

                if ctx.stage.join and ctx.stage.join.get("hook") == "merge_outputs":
                    return self.registry.invoke("merge_outputs", ctx)

            tracking = get_phase_tracking(skeleton)
            if tracking is not None and step_name:
                phase = phase_for_step_transition(step_name, tracking)
                if phase is not None:
                    set_workflow_phase_for_workflow(workflow_id, phase, skeleton, tenant_id=tenant_id)

            if should_present_specialist_output(ctx.stage, skeleton, step_name=step_name):
                try:
                    workflow, tasks = load_workflow_payload(self.client, workflow_id)
                    ctx.workflow = workflow
                    ctx.tasks = tasks
                    present_result = self.registry.invoke(
                        "present_specialist_output",
                        ctx,
                    )
                    if present_result.get("status") == "presented":
                        self._maybe_notify(ctx, "awaiting_approval")
                        self._sync_session(workflow_id, workflow_type, tenant_id)
                        return present_result
                    return present_result
                except Exception as exc:
                    return {
                        "hook": "present_specialist_output",
                        "status": "error",
                        "error": f"specialist output presentation failed: {exc}",
                    }

        if event == "failed":
            self._notify_parent_of_failure(workflow_id)

        if event == "failed" and ctx.stage is not None:
            on_failure = dict(ctx.stage.on_failure or {})
            if on_failure.get("hook") == "compensate_on_failure":
                return self.registry.invoke("compensate_on_failure", ctx)

        if event == "blocked" and ctx.stage is not None:
            on_block = dict(getattr(ctx.stage, "on_block", None) or {})
            if on_block.get("hook") == "revise_on_reject":
                ctx.reject_comments = _qa_feedback_text(
                    task,
                    dispatch_result,
                    error,
                )
                revise_result = self.registry.invoke("revise_on_reject", ctx)
                cause, failed = _revision_cause(error)
                if revise_result.get("status") == "revising":
                    ctx.extras["error"] = (
                        f"{cause}; sending it back to OpenCode "
                        f"(revision {revise_result.get('revision_number')}/"
                        f"{revise_result.get('max_revisions')})."
                    )
                elif revise_result.get("status") == "revision_limit":
                    ctx.extras["error"] = (
                        f"{error or failed}\n"
                        "Automatic OpenCode revision limit reached; a human "
                        "must decide next steps."
                    )
                notify_result = self._maybe_notify(ctx, "blocked")
                self._sync_session(workflow_id, workflow_type, tenant_id, task=task)
                return {**revise_result, "notify": notify_result}

        if event == "ready" and ctx.stage is not None and ctx.stage.wait_external:
            wait_result = self.registry.invoke("wait_external", ctx)
            self._maybe_notify(ctx, "awaiting_external")
            return wait_result

        notify_result = self._maybe_notify(ctx, event)
        self._sync_session(workflow_id, workflow_type, tenant_id, task=task)
        # Program-phase completion needs persisted task status/output. The runner
        # calls after_task_succeeded() after complete_task(); resume/recovery
        # paths pass an already-succeeded task and complete here.
        if event == "succeeded" and str(task.get("status") or "") == "succeeded":
            self._maybe_complete_program_phase(ctx)
        if event in {"blocked", "failed"}:
            return notify_result
        return None

    def notify_tool_approval_waiting(
        self,
        task: dict[str, Any],
        *,
        tool_name: str,
    ) -> dict[str, Any] | None:
        """Tell the bound thread a stage is parked on a tool approval, without on_block hooks."""
        if not hook_engine_enabled():
            return None
        workflow_id = str(task.get("workflow_id") or "")
        workflow_type = str(task.get("workflow_type") or "")
        if not workflow_type and workflow_id:
            workflow_type = str(
                (self.client.get_workflow(workflow_id).get("workflow") or {}).get("workflow_type") or ""
            )
        step_name = str(task.get("step_name") or "workflow step")
        ctx = self._context(
            workflow_id=workflow_id,
            workflow_type=workflow_type,
            tenant_id=str(task.get("tenant_id") or "default"),
            task=task,
            actor="workflow-runner",
            extras={
                "channel_text": (
                    f"Step `{step_name}` is waiting for approval to run `{tool_name or 'a tool'}`.\n"
                    "Reply *approve* or *reject* in this thread, or decide it in Inbox → Approvals."
                ),
            },
        )
        return self._maybe_notify(ctx, "awaiting_approval")

    def after_task_succeeded(
        self,
        task: dict[str, Any],
        *,
        dispatch_result: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Run program-phase completion after task output is persisted.

        Unlike on_task_transition(), this skips presentation/notify side effects
        so the runner can call it safely after complete_task().
        """
        if not hook_engine_enabled():
            return None
        workflow_id = str(task.get("workflow_id") or "")
        workflow_type = str(task.get("workflow_type") or "")
        tenant_id = str(task.get("tenant_id") or "default")
        persisted = {**task, "status": "succeeded"}
        output = (dispatch_result or {}).get("output")
        if isinstance(output, dict) and not persisted.get("output"):
            persisted["output"] = dict(output)
        ctx = self._context(
            workflow_id=workflow_id,
            workflow_type=workflow_type,
            tenant_id=tenant_id,
            task=persisted,
            actor="workflow-runner",
            runner_event="succeeded",
            dispatch_result=dispatch_result or {},
        )
        # on_complete hydrate also runs before persist; repeat it here so
        # $prior.<step>.output is read from the committed task row.
        if ctx.stage is not None:
            on_complete = dict(ctx.stage.on_complete or {})
            if str(on_complete.get("hook") or "") == "hydrate_input":
                downstream = list(on_complete.get("downstream") or [])
                if downstream:
                    from ts_harness.workflows.hooks import hydrate_input

                    hydrate_input.hydrate_downstream(ctx, downstream)
        result = self._maybe_complete_program_phase(ctx)
        self._sync_session(workflow_id, workflow_type, tenant_id, task=persisted)
        return result

    def _notify_parent_of_failure(self, workflow_id: str) -> None:
        try:
            workflow = self.client.get_workflow(workflow_id).get("workflow") or {}
            if workflow.get("parent_workflow_id") and not workflow.get("program_id"):
                from ts_harness.workflows.work_order_waves import notify_parent

                notify_parent(self.client, workflow, "failed")
        except Exception:
            logger.exception("parent join notification failed workflow_id=%s", workflow_id)

    def _maybe_complete_program_phase(self, ctx: HookContext) -> dict[str, Any] | None:
        try:
            return self.registry.invoke("complete_program_phase", ctx)
        except Exception:
            logger.exception(
                "complete_program_phase failed workflow_id=%s workflow_type=%s",
                ctx.workflow_id,
                ctx.workflow_type,
            )
            return None

    def cancel(self, workflow_id: str, *, actor: str, tenant_id: str = "default", reason: str = "") -> dict[str, Any]:
        payload = self.client.get_workflow(workflow_id)
        workflow_type = str((payload.get("workflow") or {}).get("workflow_type") or "generic")
        ctx = self._context(
            workflow_id=workflow_id,
            workflow_type=workflow_type,
            tenant_id=tenant_id,
            actor=actor,
            extras={"reason": reason},
        )
        result = self.registry.invoke("cancel_workflow", ctx)
        self._maybe_notify(ctx, "cancelled")
        return result

    def resume_external(
        self,
        workflow_id: str,
        *,
        event_type: str,
        payload: dict[str, Any],
        actor: str,
        tenant_id: str = "default",
        task_output: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        workflow_payload = self.client.get_workflow(workflow_id)
        workflow_type = str(
            (workflow_payload.get("workflow") or {}).get("workflow_type") or "generic"
        )
        event_payload = dict(payload or {})
        ctx = self._context(
            workflow_id=workflow_id,
            workflow_type=workflow_type,
            tenant_id=tenant_id,
            actor=actor,
            extras={
                "event_type": event_type,
                # Must remain the external event body (e.g. github.merge pr_url),
                # not the workflow record — pollers/webhooks match against this.
                "payload": event_payload,
                "task_output": task_output,
            },
        )
        from ts_harness.workflows.hooks import wait_external

        result = wait_external.resume(ctx)
        task_id = str(result.get("task_id") or "")
        status = result.get("status")
        if status in {"resumed", "blocked"} and task_id:
            task = self._find_task(workflow_id, task_id)
            if task is not None and status == "resumed":
                self.on_task_transition(
                    task,
                    "succeeded",
                    dispatch_result={"output": task.get("output") or {}},
                )
            elif task is not None:
                result = {
                    **result,
                    "on_block": self.on_task_transition(
                        task,
                        "blocked",
                        dispatch_result={"output": task.get("output") or {}},
                        error=str(result.get("reason") or ""),
                    ),
                }
        return result

    def block_wait(
        self,
        workflow_id: str,
        task_id: str,
        reason: str,
        *,
        output: dict[str, Any] | None = None,
        actor: str = "workflow-runner",
    ) -> dict[str, Any] | None:
        """Block an armed external wait and run the stage's ``on_block``."""
        self.client.block_task_api(task_id, reason, output=output, actor=actor)
        task = self._find_task(workflow_id, task_id)
        if task is None:
            return None
        return self.on_task_transition(
            task,
            "blocked",
            dispatch_result={"output": output or task.get("output") or {}},
            error=reason,
        )

    def _find_task(self, workflow_id: str, task_id: str) -> dict[str, Any] | None:
        workflow_payload = self.client.get_workflow(workflow_id)
        return next(
            (item for item in (workflow_payload.get("tasks") or []) if str(item.get("id")) == task_id),
            None,
        )

    def get_status(self, workflow_id: str) -> dict[str, Any]:
        return self.client.get_workflow_status(workflow_id)

    def get_workflow_phase(self, workflow_id: str, workflow_type: str, *, tenant_id: str = "default") -> str:
        from ts_harness.agent.sessions import get_session_store

        payload = self.client.get_workflow(workflow_id)
        skeleton = self._skeleton_for_state(
            workflow_type,
            payload=payload,
            workflow_id=workflow_id,
        )
        tracking = get_phase_tracking(skeleton)
        if tracking is None:
            return "unknown"

        for session in get_session_store().list_for_tenant(tenant_id):
            metadata = session.metadata or {}
            if metadata.get("workflow_id") == workflow_id or read_source_workflow_id(metadata) == workflow_id:
                phase = read_workflow_phase(metadata)
                if phase:
                    return phase

        tasks = list(payload.get("tasks") or [])
        return derive_workflow_phase(tasks, tracking)

    def get_prd_phase(self, workflow_id: str, *, tenant_id: str = "default") -> str:
        return self.get_workflow_phase(workflow_id, "prd_request", tenant_id=tenant_id)

    def bridge_workflow(
        self,
        *,
        session: SessionRecord,
        actor: str,
        tenant_id: str = "default",
        summary: str = "",
        target_workflow_type: str | None = None,
        target_node: str | None = None,
        decision: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = session.metadata or {}
        source_workflow_type = str(metadata.get("workflow_type") or "prd_request")
        source_workflow_id = read_source_workflow_id(metadata)
        payload = self._workflow_payload(source_workflow_id or "pending")
        skeleton = self._skeleton_for_state(
            source_workflow_type,
            payload=payload,
        )
        bridge = dict(getattr(skeleton, "bridge", None) or {})
        target = str(target_workflow_type or bridge.get("target_workflow_type") or "")
        from ts_harness.workflows.session_compat import read_approved_artifact

        extras = {
            "session": session,
            "source_workflow_id": source_workflow_id,
            "approved_artifact": read_approved_artifact(metadata),
            "binding_key": metadata.get("binding_key"),
            "summary": summary,
            "target_workflow_type": target,
        }
        if target_node:
            extras["target_node"] = target_node
        if decision:
            extras["decision"] = dict(decision)
        ctx = self._context(
            workflow_id=source_workflow_id or "pending",
            workflow_type=source_workflow_type,
            tenant_id=tenant_id,
            actor=actor,
            extras=extras,
        )
        return self.registry.invoke("bridge_workflow", ctx)

    def continue_delivery(
        self,
        *,
        session: SessionRecord,
        actor: str,
        tenant_id: str = "default",
        summary: str = "",
    ) -> dict[str, Any]:
        return self.bridge_workflow(
            session=session,
            actor=actor,
            tenant_id=tenant_id,
            summary=summary,
            target_workflow_type=None,
        )

    def _maybe_notify(self, ctx: HookContext, event: str) -> dict[str, Any] | None:
        ctx.runner_event = event
        try:
            return self.registry.invoke("notify_bound_channel", ctx)
        except Exception as exc:
            logger.exception(
                "notify_bound_channel failed workflow_id=%s event=%s",
                ctx.workflow_id,
                event,
            )
            return {"status": "error", "error": str(exc)}


_default_engine: WorkflowEngine | None = None


def _revision_cause(error: str | None) -> tuple[str, str]:
    """(message for a started revision, fallback text when the limit is hit)."""
    text = str(error or "")
    if "stale_base" in text or "base_advanced" in text:
        return "The base branch moved; refreshing the PR", "Base branch refresh failed"
    if "code_review" in text:
        return "Code review blocked the PR", "Code review failed"
    return "QA rejected the PR", "QA failed"


def _qa_feedback_text(
    task: dict[str, Any],
    dispatch_result: dict[str, Any] | None,
    error: str | None,
) -> str:
    output = (dispatch_result or {}).get("output") or (task or {}).get("output") or {}
    if not isinstance(output, dict):
        return str(error or output or "").strip()
    review_feedback = str(output.get("review_feedback") or "").strip()
    if review_feedback:
        return review_feedback
    parts: list[str] = []
    result = output.get("result") if isinstance(output.get("result"), dict) else {}
    summary = str(result.get("summary") or output.get("summary") or "").strip()
    if summary:
        parts.append(summary)
    prs = result.get("prs") if isinstance(result.get("prs"), list) else []
    for pr in prs:
        if not isinstance(pr, dict):
            continue
        pr_summary = str(pr.get("summary") or "").strip()
        if pr_summary and pr_summary not in parts:
            parts.append(pr_summary)
        defects = pr.get("defects") or []
        if defects:
            parts.append("Defects: " + json.dumps(defects))
    if parts:
        return "\n".join(parts)
    return str(error or "").strip()


def get_workflow_engine() -> WorkflowEngine:
    global _default_engine
    if _default_engine is None:
        _default_engine = WorkflowEngine()
    return _default_engine
