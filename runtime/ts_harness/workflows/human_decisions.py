"""Human decisions on a step that stopped for good, e.g. after the automatic revision limit.

``revise`` sends the work back with a fresh revision budget (the stage's own
``max_revisions``), ``accept`` completes the step with the output it was blocked
with, and ``stop`` ends the work order so its wave reports it as failed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ts_harness.workflows.work_order_waves import (
    HUMAN_DECISION_KEY,
    _is_terminal_block,
    awaiting_human_decision,
)

ACCEPT_KEY = "_human_accept"
STOPPED_PREFIX = "stopped_by_human:"
DECISIONS = ("retry", "revise", "accept", "stop")
# Input key a retry with guidance sets; the agent loop shows it in the step's prompt.
HUMAN_GUIDANCE_KEY = "human_guidance"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def display_actor(actor: str) -> str:
    """Name for chat text; a shared gateway-token sign-in has no personal identity."""
    actor = str(actor or "").strip()
    return "An admin" if actor.lower() in {"", "system", "admin", "human"} else actor


def _input(task: dict[str, Any] | None) -> dict[str, Any]:
    raw = (task or {}).get("input")
    return dict(raw) if isinstance(raw, dict) else {}


def stuck_steps(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Steps parked for a human decision."""
    return [task for task in tasks if awaiting_human_decision(task)]


def decidable(task: dict[str, Any] | None) -> bool:
    if task is None:
        return False
    if str(task.get("error") or "").startswith(STOPPED_PREFIX):
        return False
    return awaiting_human_decision(task) or _is_terminal_block(task)


def _reruns_on_revise(stage: Any) -> bool:
    """A coding stage with no revise loop of its own (``deliver_work_order``): revise re-runs it.

    Its blocks are failed coding runs (OpenCode killed, crashed or left no PR), so the useful
    choices are running it again with guidance, or stopping. Accepting it would accept no PR.
    """
    if stage is None or getattr(stage, "wait_external", None):
        return False
    on_block = dict(getattr(stage, "on_block", None) or {})
    return on_block.get("hook") != "revise_on_reject" and getattr(stage, "agent_id", "") == "dev-agent"


def available_decisions(stage: Any) -> list[str]:
    """Decisions that make sense for the stage; accepting an external wait would fake its event."""
    # Retry runs the same step again with the same input: the fix for a cause outside the
    # code (a lost coding job, a missing permission, a service that was down).
    decisions: list[str] = ["retry"]
    on_block = dict(getattr(stage, "on_block", None) or {})
    if on_block.get("hook") == "revise_on_reject" or _reruns_on_revise(stage):
        decisions.append("revise")
    if _reruns_on_revise(stage):
        decisions.append("stop")
        return decisions
    if stage is not None and not getattr(stage, "wait_external", None):
        decisions.append("accept")
    decisions.append("stop")
    return decisions


def work_order_label(tasks: list[dict[str, Any]], workflow: dict[str, Any] | None = None) -> str:
    for task in tasks:
        work_order_id = str(_input(task).get("work_order_id") or "").strip()
        if work_order_id:
            return work_order_id
    return str((workflow or {}).get("external_ref") or "this work order")


def mark_awaiting_human_decision(client: Any, task_id: str, tasks: list[dict[str, Any]], *, reason: str) -> None:
    stored = next((task for task in tasks if str(task.get("id")) == task_id), None)
    client.patch_task_input(task_id, {**_input(stored), HUMAN_DECISION_KEY: {"reason": reason, "since": _now()}})


def revision_limit_text(
    *,
    label: str,
    cause: str,
    feedback: str,
    max_revisions: int,
    decisions: list[str],
    diagnosis: dict[str, Any] | None = None,
    triage: dict[str, Any] | None = None,
) -> str:
    lines = [
        f"{label}: {cause}. OpenCode used all {max_revisions} automatic revisions, so the work order is waiting for you."
    ]
    if triage and triage.get("reason"):
        lines.append(f"The harness did not continue on its own because {triage['reason']}.")
        guidance = str((triage.get("triage") or {}).get("guidance") or "").strip()
        if guidance and "revise" in decisions:
            lines.append(f"Its suggested fix, to send as is:\n/revise {label} {guidance[:1500]}")
    if feedback.strip():
        lines.append(feedback.strip()[:1500])
    if diagnosis and str(diagnosis.get("approach") or "").strip():
        from ts_harness.workflows.revision_guidance import suggested_revise

        lines.append(
            "A guided revision already tried this diagnosis and the finding came back: "
            f"{str(diagnosis.get('why_attempts_failed') or '').strip()}"
        )
        if diagnosis.get("out_of_scope"):
            lines.append("The diagnosis thinks the finding asks for more than the work order covers; accepting it may be right.")
        if "revise" in decisions:
            lines.append("To try once more with that guidance, send:\n" + suggested_revise(label, diagnosis)[:1500])
    options: list[str] = []
    if "retry" in decisions:
        options.append(f"`/retry {label}` to run the step again once its cause is fixed")
    if "revise" in decisions:
        options.append(f"`/revise {label} <guidance>` to send it back with {max_revisions} more automatic revisions")
    if "accept" in decisions:
        options.append(f"`/accept {label}` to accept it as it is")
    options.append(f"`/stop {label}` to stop the work order")
    lines.append("Reply " + ", ".join(options[:-1]) + (", or " if len(options) > 1 else "") + options[-1] + ". You can also decide from the admin Inbox.")
    return "\n".join(lines)


def _decision_options(label: str, decisions: list[str], *, revise_hint: str) -> str:
    options: list[str] = []
    if "retry" in decisions:
        options.append(f"`/retry {label}` to run the step again once its cause is fixed")
    if "revise" in decisions:
        options.append(f"`/revise {label} <guidance>` {revise_hint}")
    if "accept" in decisions:
        options.append(f"`/accept {label}` to accept it as it is")
    options.append(f"`/stop {label}` to stop the work order")
    return "Reply " + ", ".join(options[:-1]) + (", or " if len(options) > 1 else "") + options[-1] + ". You can also decide from the admin Inbox."


def conflicting_feedback_text(*, label: str, conflict: list[dict[str, Any]], decisions: list[str]) -> str:
    """Review and QA asked for opposite changes in turn; show each verdict so a person can settle it."""
    lines = [
        f"{label}: code review and QA keep asking for opposite changes, so OpenCode stopped revising. "
        "This usually means the work order asks for two things that cannot both hold."
    ]
    for item in conflict:
        who = "QA" if item.get("step") == "qa_gate" else "Code review"
        feedback = str(item.get("feedback") or "").strip()
        lines.append(f"{who}: {feedback[:700]}" if feedback else f"{who}: (no feedback text)")
    lines.append(
        _decision_options(label, decisions, revise_hint="with the guidance that settles it (it overrides the work order)")
    )
    return "\n".join(lines)


def stuck_summary(engine: Any, workflow_id: str, task_id: str) -> dict[str, Any]:
    """What a person needs to decide a stuck step: work order, PR, feedback and the allowed decisions."""
    from ts_harness.workflows.engine import _qa_feedback_text

    payload = engine.client.get_workflow(workflow_id)
    workflow = dict(payload.get("workflow") or {})
    tasks = list(payload.get("tasks") or [])
    task = next((item for item in tasks if str(item.get("id")) == str(task_id)), None) or {}
    workflow_type = str(task.get("workflow_type") or workflow.get("workflow_type") or "")
    try:
        stage = engine._skeleton_for_state(workflow_type, task=task, payload=payload).stage(str(task.get("step_name") or ""))
    except (KeyError, LookupError, FileNotFoundError):
        stage = None
    pr_url = ""
    for item in tasks:
        output = item.get("output") if isinstance(item.get("output"), dict) else {}
        if item.get("status") == "succeeded" and output.get("pr_url"):
            pr_url = str(output["pr_url"])
    program_id = str(workflow.get("program_id") or "")
    parent_id = str(workflow.get("parent_workflow_id") or "")
    if not program_id and parent_id:
        try:
            program_id = str((engine.client.get_workflow(parent_id).get("workflow") or {}).get("program_id") or "")
        except Exception:  # noqa: BLE001 - the program link is a convenience
            program_id = ""
    output = task.get("output") if isinstance(task.get("output"), dict) else {}
    return {
        "workflow_id": workflow_id,
        "task_id": str(task_id),
        "step_name": str(task.get("step_name") or ""),
        "work_order": work_order_label(tasks, workflow),
        "decisions": available_decisions(stage),
        "feedback": _qa_feedback_text(task, {"output": output}, str(task.get("error") or ""))[:4000],
        "error": str(task.get("error") or ""),
        "reason": str((_input(task).get(HUMAN_DECISION_KEY) or {}).get("reason") or ""),
        "pr_url": pr_url,
        "program_id": program_id,
        "arbiter": _arbiter_view(task),
    }


def _arbiter_view(task: dict[str, Any]) -> dict[str, Any] | None:
    """The arbiter's verdict on this stop, for the Inbox: label, diagnosis, recommendation, actions."""
    from ts_harness.workflows.stuck_arbiter import record, seen

    rec = record(task)
    if not rec:
        return None
    view = {key: rec.get(key) for key in ("label", "category", "reason", "why", "question", "recommendation", "actions", "at")}
    view["current"] = seen(task)
    return view


def output_shape_problem(task: dict[str, Any] | None, output: dict[str, Any] | None) -> str:
    """The first way ``output`` breaks the step's output schema, or "" (no schema, or it fits)."""
    contract = (task or {}).get("contract")
    schema = contract.get("output_schema") if isinstance(contract, dict) else None
    if not isinstance(schema, dict) or not schema:
        return ""
    try:
        from jsonschema import Draft7Validator
    except ImportError:  # pragma: no cover - jsonschema ships with the runtime
        return ""
    errors = sorted(Draft7Validator(schema).iter_errors(output or {}), key=str)
    if not errors:
        return ""
    where = "/" + "/".join(str(part) for part in errors[0].absolute_path)
    return f"{where}: {errors[0].message}"[:300]


def accepted(task: dict[str, Any]) -> dict[str, Any] | None:
    accept = _input(task).get(ACCEPT_KEY)
    return dict(accept) if isinstance(accept, dict) and accept else None


def accepted_output(task: dict[str, Any], *, github: Any = None) -> dict[str, Any]:
    """Output an accepted step completes with: the output it was blocked with, marked as a human override."""
    accept = accepted(task) or {}
    step = str(task.get("step_name") or "")
    stored = accept.get("output") if isinstance(accept.get("output"), dict) else task.get("output")
    output = dict(stored) if isinstance(stored, dict) else {}
    actor = str(accept.get("actor") or "human")
    output["human_override"] = {"actor": actor, "at": accept.get("at"), "step": step}
    if step == "finalize_code_review":
        from ts_harness.workflows.code_review import publish_override_status

        output["verdict"] = "accepted"
        warnings = publish_override_status(list(output.get("prs") or []), actor, github=github)
        output["warnings"] = [*list(output.get("warnings") or []), *warnings]
    elif step == "qa_gate":
        from ts_harness.workflows.qa_status import qa_override

        warnings = qa_override(_input(task), actor, github=github)
        output["warnings"] = [*list(output.get("warnings") or []), *warnings]
    return output


def _note_after_arbiter(tasks: list[dict[str, Any]], step: str, action: str, actor: str) -> None:
    """Log a person's decision on a step the arbiter already acted on: how often it had to step in."""
    if actor == "harness-arbiter":
        return
    from ts_harness.workflows.stuck_arbiter import record

    acted = [a for t in tasks if str(t.get("step_name") or "") == step for a in record(t).get("actions") or [] if isinstance(a, dict)]
    if acted:
        import logging

        logging.getLogger(__name__).info(
            "human_decision_after_arbiter step=%s arbiter=%s human=%s", step, acted[-1].get("action"), action
        )


def decide(
    engine: Any,
    workflow_id: str,
    task_id: str,
    action: str,
    *,
    actor: str,
    guidance: str = "",
    tenant_id: str = "default",
) -> dict[str, Any]:
    from ts_harness.workflows.engine import _qa_feedback_text

    action = str(action or "").strip().lower()
    if action not in DECISIONS:
        raise ValueError(f"unknown decision: {action or '(empty)'}")
    payload = engine.client.get_workflow(workflow_id)
    workflow = dict(payload.get("workflow") or {})
    tasks = list(payload.get("tasks") or [])
    task = next((item for item in tasks if str(item.get("id")) == str(task_id)), None)
    if not decidable(task):
        raise ValueError("step is not waiting for a decision")
    assert task is not None
    error = str(task.get("error") or "")
    workflow_type = str(task.get("workflow_type") or workflow.get("workflow_type") or "")
    ctx = engine._context(
        workflow_id=workflow_id,
        workflow_type=workflow_type,
        tenant_id=tenant_id,
        task=task,
        actor=actor,
        runner_event="blocked",
        extras={"error": error},
    )
    step = str(task.get("step_name") or "step")
    if action not in available_decisions(ctx.stage):
        raise ValueError(f"{action} is not available for step {step}")
    label = work_order_label(tasks, workflow)
    output = task.get("output") if isinstance(task.get("output"), dict) else {}
    base = {"decision": action, "workflow_id": workflow_id, "task_id": str(task_id), "step_name": step, "work_order": label}
    _note_after_arbiter(tasks, step, action, actor)
    who = display_actor(actor)

    if action == "revise" and _reruns_on_revise(ctx.stage):
        note = f"The previous run failed: {error}"[:2000]
        if guidance.strip():
            note += f"\n\nGuidance from {'an admin' if who == 'An admin' else who}:\n{guidance.strip()}"
        engine.client.resume_blocked_task(
            str(task_id),
            actor=actor,
            input_patch={"qa_feedback": note, HUMAN_DECISION_KEY: None},
            reason=f"re-run by {actor}",
        )
        ctx.extras["channel_text"] = f"{who} asked to run {label} again; sending it back to OpenCode."
        engine._maybe_notify(ctx, "blocked")
        engine._sync_session(workflow_id, workflow_type, tenant_id, task=task)
        return {**base, "status": "revising", "rerun": True}

    if action == "revise":
        feedback = _qa_feedback_text(task, {"output": output}, error)
        if guidance.strip():
            author = "an admin" if who == "An admin" else who
            feedback = f"{feedback}\n\nGuidance from {author}:\n{guidance.strip()}"
        ctx.reject_comments = feedback
        grant = {"actor": actor, "at": _now()}
        waited = (_input(task).get(HUMAN_DECISION_KEY) or {}) if isinstance(_input(task).get(HUMAN_DECISION_KEY), dict) else {}
        if waited.get("since"):
            grant["waiting_since"] = waited["since"]  # the ticket story shows how long it waited
        if guidance.strip():
            grant["guidance"] = guidance.strip()
        ctx.extras["grant"] = grant
        result = engine.registry.invoke("revise_on_reject", ctx)
        if result.get("status") != "revising":
            raise ValueError(f"could not start a revision: {result.get('status') or 'unknown'}")
        ctx.extras["channel_text"] = (
            f"{who} asked for another round on {label}; sending it back to OpenCode "
            f"(revision {result.get('revision_number')}/{result.get('max_revisions')})."
        )
        engine._maybe_notify(ctx, "blocked")
        engine._sync_session(workflow_id, workflow_type, tenant_id, task=task)
        return {**result, **base, "status": "revising"}

    if action == "retry":
        patch: dict[str, Any] = {HUMAN_DECISION_KEY: None}
        if guidance.strip():
            # The rerun's agent sees why the step stopped and what the person wants changed.
            author = "an admin" if who == "An admin" else who
            patch[HUMAN_GUIDANCE_KEY] = (
                (f"The previous run stopped: {error}"[:2000] + "\n\n" if error else "")
                + f"Guidance from {author}:\n{guidance.strip()}"
            )
        engine.client.resume_blocked_task(
            str(task_id),
            actor=actor,
            input_patch=patch,
            reason=f"retried by {actor}" + (" with guidance" if guidance.strip() else ""),
        )
        ctx.extras["channel_text"] = f"{who} retried {label} at `{step}`" + (
            " with guidance; running it again." if guidance.strip() else "; running it again."
        )
        ctx.runner_event = "retried"
        try:
            from ts_harness.workflows.hooks.notify_bound_channel import handle as notify_bound

            notify_bound(ctx)
        except Exception:  # noqa: BLE001 - the retry stands without the message
            pass
        return {**base, "status": "retried"}
    if action == "accept":
        shape = output_shape_problem(task, output)
        if shape:
            # Accepting keeps the output as it is; with the wrong shape the next steps can't find
            # what they need (a plan nested under `artifacts` left the program with no approved
            # plan). A check that judged the content (an endpoint rule, a size limit) can still be
            # overridden; the shape can't.
            raise ValueError(
                f"Accept isn't possible here: the last output of {step} doesn't have the shape the next steps "
                f"need ({shape}). Retry it instead, with guidance if it keeps failing."
            )
        engine.client.resume_blocked_task(
            str(task_id),
            actor=actor,
            input_patch={ACCEPT_KEY: {"actor": actor, "at": _now(), "output": output}, HUMAN_DECISION_KEY: None},
            reason=f"accepted by {actor}",
        )
        ctx.extras["channel_text"] = f"{who} accepted {label} as it is at `{step}`; continuing the delivery."
        engine._maybe_notify(ctx, "blocked")
        engine._sync_session(workflow_id, workflow_type, tenant_id, task=task)
        return {**base, "status": "accepted"}

    stopped_error = f"{STOPPED_PREFIX} {actor}: {error}"[:2000]
    stopped_input = {**_input(task), HUMAN_DECISION_KEY: None}
    engine.client.patch_task_input(str(task_id), stopped_input)
    engine.client.block_task_api(str(task_id), stopped_error, output=output or None, actor=actor)
    ctx.task = {**task, "error": stopped_error, "input": stopped_input}
    ctx.extras["channel_text"] = f"{who} stopped {label}; its wave will report it as failed."
    engine._maybe_notify(ctx, "blocked")
    engine._maybe_complete_program_phase(ctx)
    engine._sync_session(workflow_id, workflow_type, tenant_id, task=task)
    return {**base, "status": "stopped"}
