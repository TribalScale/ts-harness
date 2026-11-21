"""Dynamic revision loop on approval-gate reject or blocked QA."""

from __future__ import annotations

from typing import Any

from ts_harness.workflows.decisions import decision_request_fields
from ts_harness.workflows.expressions import resolve_input_map, resolve_value
from ts_harness.workflows.hooks._helpers import create_tasks_bulk, load_workflow_payload
from ts_harness.workflows.materialize import workflow_definitions
from ts_harness.workflows.registry import HookContext


def _loop_config(ctx: HookContext) -> dict[str, Any]:
    stage = ctx.stage
    if ctx.runner_event == "blocked" and getattr(stage, "on_block", None):
        return select_block_case(dict(stage.on_block or {}), str(ctx.extras.get("error") or ""))
    return dict(getattr(stage, "on_reject", None) or {})


def select_block_case(config: dict[str, Any], error: str) -> dict[str, Any]:
    """``on_block`` with the first case whose ``error_prefix`` matches the block reason applied over it."""
    cases = config.pop("cases", None) or []
    for case in cases:
        prefix = str((case or {}).get("error_prefix") or "")
        if prefix and error.startswith(prefix):
            return {**config, **{key: value for key, value in case.items() if key != "error_prefix"}}
    return config


_REVISION_ONLY_KEYS = frozenset(
    {"_revision_budget", "_revision_source", "refresh", "qa_feedback", "review_feedback", "revision_files",
     "_guided", "_guidance", "coding_model"}
)
# Gates whose verdicts can contradict each other. When the revisions they ask for alternate
# (review, QA, review), they are pulling the code in opposite directions, usually because the
# work order asks for two incompatible things: a person decides instead of a further revision.
CONFLICT_GATES = frozenset({"finalize_code_review", "code_review", "qa_gate"})
CONFLICT_RUN = 3


def _feedback(task: dict[str, Any]) -> str:
    task_input = task.get("input") if isinstance(task.get("input"), dict) else {}
    return str(task_input.get("review_feedback") or task_input.get("qa_feedback") or "").strip()


def conflicting_gates(existing: list[dict[str, Any]], gate_step: str, comments: str) -> list[dict[str, str]] | None:
    """The alternating rejections when review and QA keep undoing each other, else None.

    ``existing`` are this budget's revisions, oldest first; each records the gate that asked
    for it (``_revision_source``). Adding the gate blocking now, the last ``CONFLICT_RUN``
    sources must alternate between two different conflict gates.
    """
    if gate_step not in CONFLICT_GATES:
        return None
    history = [
        {"step": str((task.get("input") or {}).get("_revision_source") or ""), "feedback": _feedback(task)}
        for task in sorted(existing, key=lambda task: str(task.get("created_at") or ""))
        if isinstance(task.get("input"), dict) and (task.get("input") or {}).get("_revision_source")
    ]
    history.append({"step": gate_step, "feedback": comments.strip()})
    run = history[-CONFLICT_RUN:]
    if len(run) < CONFLICT_RUN or any(item["step"] not in CONFLICT_GATES for item in run):
        return None
    steps = [_gate_kind(item["step"]) for item in run]
    alternating = all(steps[i] != steps[i + 1] for i in range(len(steps) - 1)) and len(set(steps)) == 2
    return run if alternating else None


def _with_gate_output(ctx: HookContext, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``tasks`` with the blocking gate's verdict, which may not be stored yet when this runs."""
    output = (getattr(ctx, "dispatch_result", None) or {}).get("output") if isinstance(getattr(ctx, "dispatch_result", None), dict) else None
    gate_id = str((ctx.task or {}).get("id") or "")
    if not isinstance(output, dict) or not gate_id:
        return tasks
    merged = [dict(task, output=output, status="blocked") if str(task.get("id") or "") == gate_id else task for task in tasks]
    if not any(str(task.get("id") or "") == gate_id for task in tasks):
        merged.append({**dict(ctx.task or {}), "output": output, "status": "blocked"})
    return merged


def _guided_plan(tasks: list[dict[str, Any]], gate_step: str, implement_step: str) -> dict[str, Any] | None:
    from ts_harness.workflows import revision_guidance

    if gate_step not in revision_guidance.GUIDED_GATES:
        return None
    pr_url = ""
    for task in sorted(tasks, key=lambda item: str(item.get("created_at") or ""), reverse=True):
        output = task.get("output") if isinstance(task.get("output"), dict) else {}
        if task.get("step_name") == implement_step and output.get("pr_url"):
            pr_url = str(output["pr_url"])
            break
    try:
        return revision_guidance.plan_guided_revision(
            tasks, gate_step=gate_step, implement_step=implement_step, pr_url=pr_url
        )
    except Exception:  # noqa: BLE001 - a failed diagnosis falls back to the ordinary loop
        import logging

        logging.getLogger(__name__).exception("guided revision planning failed")
        return None


def _gate_kind(step: str) -> str:
    return "qa" if step == "qa_gate" else "review"


def _revision_budget(task: dict[str, Any]) -> str:
    task_input = task.get("input") if isinstance(task.get("input"), dict) else {}
    return str(task_input.get("_revision_budget") or "")


def _latest_grant_at(tasks: list[dict[str, Any]]) -> str | None:
    """``created_at`` of the newest revision a human granted after the limit, if any."""
    granted = [
        str(task.get("created_at") or "")
        for task in tasks
        if isinstance(task.get("input"), dict) and task["input"].get("_revision_grant")
    ]
    return max(granted) if granted else None


def _is_block_loop(ctx: HookContext) -> bool:
    return ctx.runner_event == "blocked" and bool(getattr(ctx.stage, "on_block", None))


def _latest_succeeded(
    tasks: list[dict[str, Any]],
    step_names: list[str],
) -> dict[str, Any] | None:
    candidates = [
        task
        for task in tasks
        if task.get("step_name") in step_names and task.get("status") == "succeeded"
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda task: str(task.get("created_at") or ""))[-1]


def _task_body_from_stage(
    ctx: HookContext,
    step_name: str,
    *,
    agent_id: str,
    depends_on: list[str],
    input_data: dict[str, Any],
    approval_required: bool = False,
) -> dict[str, Any]:
    skeleton = ctx.skeleton
    try:
        stage = skeleton.stage(step_name)
    except KeyError:
        return {
            "step_name": step_name,
            "agent_id": agent_id,
            "depends_on": depends_on,
            "input": input_data,
            "tenant_id": ctx.tenant_id,
            "approval_required": approval_required,
        }
    return {
        "step_name": step_name,
        "agent_id": agent_id or stage.agent_id,
        "depends_on": depends_on,
        "input": input_data,
        "tenant_id": ctx.tenant_id,
        "approval_required": approval_required,
        "max_retries": int((stage.retry or {}).get("max_retries", 0)),
        "definition_version": skeleton.definition_version,
        "contract": skeleton.resolve_stage_contract(stage) or {},
        "lane": workflow_definitions.stage_lane(stage),
    }


def _cancel_superseded(
    ctx: HookContext,
    tasks: list[dict[str, Any]],
    *,
    current_id: str,
    resume_steps: set[str],
) -> list[str]:
    cancelled: list[str] = []
    terminal = {"succeeded", "cancelled", "failed"}
    for task in tasks:
        task_id = str(task.get("id") or "")
        step = str(task.get("step_name") or "")
        status = str(task.get("status") or "")
        if not task_id or status in terminal:
            continue
        if task_id == current_id or step in resume_steps:
            ctx.memory_client.cancel_task(
                task_id,
                actor=ctx.actor,
                reason="superseded by revision",
            )
            cancelled.append(task_id)
    return cancelled


def _resolve_depends_on(
    raw_deps: list[Any],
    *,
    tasks: list[dict[str, Any]],
    workflow: dict[str, Any],
    reject_comments: str,
    source_steps: list[str],
    extras: dict[str, Any] | None = None,
) -> list[str]:
    resolved_ids: list[str] = []
    for dep in raw_deps:
        if isinstance(dep, str) and dep.startswith("$"):
            resolved = resolve_value(
                dep,
                tasks=tasks,
                workflow=workflow,
                reject_comments=reject_comments,
                source_steps=source_steps,
                extras=extras or {},
            )
            if resolved:
                resolved_ids.append(str(resolved))
        elif dep:
            task_match = next(
                (
                    task
                    for task in tasks
                    if task.get("step_name") == dep and task.get("status") == "succeeded"
                ),
                None,
            )
            if task_match:
                resolved_ids.append(str(task_match["id"]))
    return resolved_ids


def handle(ctx: HookContext) -> dict[str, Any]:
    if ctx.task is None:
        raise ValueError("task required for revise_on_reject")
    config = _loop_config(ctx)
    revise_cfg = dict(config.get("revise") or {})
    re_gate_cfg = dict(config.get("re_gate") or {})
    raw_resume = config.get("resume_after") or {}
    resume_chain = [dict(item) for item in raw_resume] if isinstance(raw_resume, list) else [dict(raw_resume)] if raw_resume else []
    block_loop = _is_block_loop(ctx)
    if block_loop and config.get("action") == "stop":
        return {"status": "stopped", "workflow_id": ctx.workflow_id}

    workflow, tasks = load_workflow_payload(ctx.memory_client, ctx.workflow_id)
    gate = ctx.task
    if not block_loop:
        if gate.get("approval_status") not in {"pending", "rejected"}:
            raise ValueError("gate task is not awaiting revision")
        ctx.memory_client.reject_task(
            str(gate["id"]),
            actor=ctx.actor,
            **decision_request_fields(ctx.extras.get("decision"), ctx.workflow),
        )

    source_steps = list(revise_cfg.get("source_steps") or [])
    revise_step = str(revise_cfg.get("step_name") or "revise")
    budget = str(revise_cfg.get("budget") or "")
    max_revisions = int(config.get("max_revisions") or 0)
    grant = dict(ctx.extras.get("grant") or {}) if block_loop else {}
    position: int | None = None
    guided_plan: dict[str, Any] | None = None
    if block_loop:
        if max_revisions <= 0:
            max_revisions = 2
        existing = [
            task
            for task in tasks
            if task.get("step_name") == revise_step
            and task.get("status") != "cancelled"
            and _revision_budget(task) == budget
        ]
        granted_at = _latest_grant_at(existing)
        if granted_at is not None:
            existing = [task for task in existing if str(task.get("created_at") or "") >= granted_at]
        if grant:
            existing = []
        # The unbudgeted count includes the graph's own first run of the step;
        # a human grant restarts the count at the granted revision.
        limit = max_revisions if budget or grant or granted_at is not None else 1 + max_revisions
        conflict = None if grant else conflicting_gates(existing, str(gate.get("step_name") or ""), ctx.reject_comments or "")
        if conflict:
            return {
                "status": "revision_limit",
                "reason": "conflicting_feedback",
                "workflow_id": ctx.workflow_id,
                "existing_revisions": len(existing),
                "max_revisions": max_revisions,
                "conflict": conflict,
            }
        guided_plan = None
        guided_before = next(
            (dict((task.get("input") or {}).get("_guidance") or {}) for task in reversed(existing)
             if isinstance(task.get("input"), dict) and task["input"].get("_guided")),
            None,
        )
        if not grant and guided_before is None:
            # The same finding came back after a revision: diagnose it and send one guided
            # attempt (even past the limit) before asking a person.
            guided_plan = _guided_plan(_with_gate_output(ctx, tasks), str(gate.get("step_name") or ""), revise_step)
        if len(existing) >= limit and guided_plan is None:
            result = {
                "status": "revision_limit",
                "workflow_id": ctx.workflow_id,
                "existing_revisions": len(existing),
                "max_revisions": max_revisions,
            }
            if guided_before:
                result["diagnosis"] = guided_before
            return result
        if grant or granted_at is not None or guided_plan is not None:
            position = len(existing) + 1

    source = _latest_succeeded(tasks, source_steps) if source_steps else None
    source_input = dict((source or {}).get("input") or {})
    mapped = resolve_input_map(
        dict(revise_cfg.get("input_map") or {}),
        tasks=tasks,
        workflow=workflow,
        reject_comments=ctx.reject_comments,
        source_steps=source_steps,
    )
    revise_input = {
        **{key: value for key, value in source_input.items() if key not in _REVISION_ONLY_KEYS},
        **dict(revise_cfg.get("static_input") or {}),
        **{key: value for key, value in mapped.items() if value is not None},
    }
    revise_input.pop("_revision_grant", None)
    if block_loop:
        revise_input["_revision_source"] = str(gate.get("step_name") or "")
    if budget:
        revise_input["_revision_budget"] = budget
        if block_loop:
            revise_input["revision_number"] = len(existing) + 1
    if grant:
        # Human guidance can ask for changes beyond the files a review finding named.
        revise_input.pop("revision_files", None)
        revise_input["_revision_grant"] = grant
    if position is not None:
        revise_input["revision_number"] = position
    if block_loop and guided_plan is not None:
        feedback_key = "qa_feedback" if "qa_feedback" in mapped and "review_feedback" not in mapped else "review_feedback"
        revise_input[feedback_key] = guided_plan["feedback"]
        revise_input["_guided"] = True
        revise_input["_guidance"] = guided_plan["diagnosis"]
        if guided_plan.get("coding_model"):
            revise_input["coding_model"] = guided_plan["coding_model"]
    revision_number = revise_input.get("revision_number")

    revise_depends_on = _resolve_depends_on(
        list(revise_cfg.get("depends_on") or []),
        tasks=tasks,
        workflow=workflow,
        reject_comments=ctx.reject_comments,
        source_steps=source_steps,
    )
    if not revise_depends_on:
        source = source or _latest_succeeded(tasks, source_steps)
        if source is None:
            raise ValueError("no succeeded source task found to revise")
        revise_depends_on = [str(source["id"])]

    resume_steps = {str(cfg.get("step_name") or "") for cfg in resume_chain} - {""}
    # The rejected gate never succeeds, so it and its pending dependents would wait forever.
    cancelled = _cancel_superseded(
        ctx,
        tasks,
        current_id=str(gate.get("id") or ""),
        resume_steps=resume_steps,
    )

    revise_ids = create_tasks_bulk(
        ctx.memory_client,
        workflow_id=ctx.workflow_id,
        workflow_type=ctx.workflow_type,
        tenant_id=ctx.tenant_id,
        tasks=[
            _task_body_from_stage(
                ctx,
                revise_step,
                agent_id=str(revise_cfg.get("agent_id") or ctx.stage.agent_id),
                depends_on=revise_depends_on,
                input_data=revise_input,
            ),
        ],
        actor=ctx.actor,
    )
    revise_task_id = revise_ids[0]

    re_gate_depends_on = _resolve_depends_on(
        list(re_gate_cfg.get("depends_on") or ["$revise.id"]),
        tasks=tasks,
        workflow=workflow,
        reject_comments=ctx.reject_comments,
        source_steps=source_steps,
        extras={"revise": {"id": revise_task_id}},
    )
    re_gate_step = str(re_gate_cfg.get("step_name") or ctx.stage.name)
    re_gate_ids = create_tasks_bulk(
        ctx.memory_client,
        workflow_id=ctx.workflow_id,
        workflow_type=ctx.workflow_type,
        tenant_id=ctx.tenant_id,
        tasks=[
            _task_body_from_stage(
                ctx,
                re_gate_step,
                agent_id=str(re_gate_cfg.get("agent_id") or ctx.stage.agent_id),
                depends_on=re_gate_depends_on,
                input_data={"revision_number": revision_number} if revision_number else {},
                approval_required=bool(re_gate_cfg.get("approval_required", True)),
            ),
        ],
        actor=ctx.actor,
    )

    resume_ids: list[str] = []
    previous_id = re_gate_ids[0]
    for index, resume_cfg in enumerate(resume_chain):
        resume_step = str(resume_cfg.get("step_name") or "")
        default_dep = "$re_gate.id" if index == 0 else "$previous.id"
        resume_depends_on = _resolve_depends_on(
            list(resume_cfg.get("depends_on") or [default_dep]),
            tasks=tasks,
            workflow=workflow,
            reject_comments=ctx.reject_comments,
            source_steps=source_steps,
            extras={
                "revise": {"id": revise_task_id},
                "re_gate": {"id": re_gate_ids[0]},
                "previous": {"id": previous_id},
            },
        )
        created = create_tasks_bulk(
            ctx.memory_client,
            workflow_id=ctx.workflow_id,
            workflow_type=ctx.workflow_type,
            tenant_id=ctx.tenant_id,
            tasks=[
                _task_body_from_stage(
                    ctx,
                    resume_step,
                    agent_id=str(resume_cfg.get("agent_id") or "orchestrator"),
                    depends_on=resume_depends_on,
                    input_data={},
                    approval_required=bool(resume_cfg.get("approval_required", False)),
                ),
            ],
            actor=ctx.actor,
        )
        resume_ids.extend(created)
        if created:
            previous_id = created[0]

    result: dict[str, Any] = {
        "workflow_id": ctx.workflow_id,
        "revise_task_id": revise_task_id,
        "re_gate_task_ids": re_gate_ids,
        "revision_number": revision_number,
    }
    if block_loop:
        result["status"] = "revising"
        if guided_plan is not None:
            result["guided"] = True
            result["diagnosis"] = guided_plan["diagnosis"]
        result["max_revisions"] = max_revisions if budget or position is not None else 1 + max_revisions
        result["resume_after_task_ids"] = resume_ids
        result["cancelled_task_ids"] = cancelled
    return result
