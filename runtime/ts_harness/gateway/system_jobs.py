"""Deterministic system cron handlers (no LLM)."""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

from ts_harness.integrations.github_client import GitHubClient, parse_github_pr_url
from ts_harness.memory.client import get_memory_client
from ts_harness.workflows.engine import get_workflow_engine
from ts_harness.workflows.pr_rejection import REJECTION_REASONS, detect_rejection

logger = logging.getLogger(__name__)

__all__ = [
    "parse_github_pr_url",
    "fetch_pull_request",
    "poll_github_merges",
    "escalate_stale_waits",
    "dispatch_system_job",
]


def fetch_pull_request(
    owner: str,
    repo: str,
    number: int,
    *,
    token: str,
) -> dict[str, Any]:
    return GitHubClient(token=token).get_pull(owner, repo, number)


def _resume_merged_pr(
    *,
    workflow_id: str,
    pr_url: str,
    merge_sha: str,
    engine: Any,
    actor: str = "github-merge-poller",
    tenant_id: str = "default",
    merged_by: str = "",
) -> dict[str, Any]:
    payload = {
        "pr_url": pr_url,
        "merge_sha": merge_sha,
        "merge_commit_sha": merge_sha,
    }
    if merged_by:
        payload["merged_by"] = merged_by
    task_output = {
        "pr_url": pr_url,
        "merge_sha": merge_sha,
        "event_type": "github.merge",
        "external_event": payload,
    }
    if merged_by:
        task_output["merged_by"] = merged_by
    return engine.resume_external(
        workflow_id,
        event_type="github.merge",
        payload=payload,
        actor=actor,
        tenant_id=tenant_id,
        task_output=task_output,
    )


def poll_github_merges(*, tenant_id: str = "default") -> dict[str, Any]:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        return {"status": "error", "error": "GITHUB_TOKEN is required", "checked": 0, "resumed": 0}

    memory = get_memory_client()
    engine = get_workflow_engine()
    waits_payload = memory.find_external_waits(
        event_type="github.merge",
        tenant_id=tenant_id,
    )
    waits = list(waits_payload.get("waits") or [])

    checked = 0
    resumed = 0
    rejected = 0
    skipped = 0
    errors = 0

    for wait in waits:
        pr_url = str(wait.get("match_pr_url") or "").strip()
        workflow_id = str(wait.get("workflow_id") or "").strip()
        if not pr_url or not workflow_id:
            skipped += 1
            continue
        parsed = parse_github_pr_url(pr_url)
        if parsed is None:
            skipped += 1
            continue
        owner, repo, number = parsed
        checked += 1
        try:
            pr = fetch_pull_request(owner, repo, number, token=token)
            if not pr.get("merged"):
                task_id = str(wait.get("task_id") or "").strip()
                handled = engine.block_case_prefixes(workflow_id, task_id) if task_id else []
                rejection = None
                if any(prefix in handled for prefix in REJECTION_REASONS):
                    rejection = detect_rejection(GitHubClient(token=token), owner, repo, number, pr)
                if rejection is None or not any(rejection["reason"].startswith(prefix) for prefix in handled):
                    skipped += 1
                    continue
                engine.block_wait(
                    workflow_id,
                    task_id,
                    rejection["reason"],
                    output={"pr_url": pr_url, "rejected_by": rejection["actor"], "review_feedback": rejection["feedback"]},
                    actor="github-merge-poller",
                )
                rejected += 1
                logger.info("github_merge_poller rejection workflow_id=%s pr_url=%s reason=%s", workflow_id, pr_url, rejection["reason"])
                continue
            merge_sha = str(pr.get("merge_commit_sha") or "").strip()
            _resume_merged_pr(
                workflow_id=workflow_id,
                pr_url=pr_url,
                merge_sha=merge_sha,
                engine=engine,
                tenant_id=tenant_id,
                merged_by=str((pr.get("merged_by") or {}).get("login") or ""),
            )
            resumed += 1
            logger.info(
                "github_merge_poller resumed workflow_id=%s pr_url=%s",
                workflow_id,
                pr_url,
            )
            try:
                from ts_harness.workflows.merge_readiness import fan_out_base_advanced

                fan_out_base_advanced(
                    {"pr_url": pr_url, "base_branch": str((pr.get("base") or {}).get("ref") or "")},
                    tenant_id=tenant_id,
                    memory=memory,
                    engine=engine,
                )
            except Exception as exc:  # noqa: BLE001 - fan-out is best effort
                logger.warning("github_merge_poller base_advanced fan-out failed pr_url=%s error=%s", pr_url, exc)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.warning(
                "github_merge_poller wait failed workflow_id=%s pr_url=%s error=%s",
                workflow_id,
                pr_url,
                exc,
            )

    return {
        "status": "ok",
        "checked": checked,
        "resumed": resumed,
        "rejected": rejected,
        "skipped": skipped,
        "errors": errors,
        "checks": poll_github_checks(tenant_id=tenant_id, token=token),
        "deployments": poll_github_deployments(tenant_id=tenant_id, token=token),
        "joins": poll_wave_joins(tenant_id=tenant_id),
    }


def poll_github_checks(*, tenant_id: str = "default", token: str | None = None) -> dict[str, Any]:
    """Fallback for missed check webhooks: resume SHA-pinned checks waits that are terminal."""
    from ts_harness.workflows.merge_readiness import (
        CHECKS_EVENT,
        TERMINAL_STATES,
        aggregate_checks,
        checks_payload,
        pinned_heads,
    )

    token = token or os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        return {"checked": 0, "resumed": 0, "errors": 0}
    memory = get_memory_client()
    engine = get_workflow_engine()
    github = GitHubClient(token=token)
    waits = list((memory.find_external_waits(event_type=CHECKS_EVENT, tenant_id=tenant_id) or {}).get("waits") or [])
    checked = resumed = errors = 0
    for wait in waits:
        heads = pinned_heads(wait)
        workflow_id = str(wait.get("workflow_id") or "")
        if not heads or not workflow_id:
            continue
        checked += 1
        try:
            aggregate = aggregate_checks(github, heads)
            if aggregate["state"] not in TERMINAL_STATES:
                continue
            payload = checks_payload(aggregate, warnings=(wait.get("options") or {}).get("warnings"))
            engine.resume_external(
                workflow_id,
                event_type=CHECKS_EVENT,
                payload=payload,
                actor="github-merge-poller",
                tenant_id=tenant_id,
                task_output={**payload, "event_type": CHECKS_EVENT, "external_event": payload},
            )
            resumed += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.warning("github_checks_poller wait failed workflow_id=%s error=%s", workflow_id, exc)
    return {"checked": checked, "resumed": resumed, "errors": errors}


def poll_github_deployments(*, tenant_id: str = "default", token: str | None = None) -> dict[str, Any]:
    """Fallback for missed deployment_status webhooks: resume terminal deployment waits."""
    from ts_harness.workflows.release import (
        DEPLOYMENT_EVENT,
        TERMINAL_DEPLOYMENT_STATES,
        deployment_payload,
        deployment_state,
    )

    token = token or os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        return {"checked": 0, "resumed": 0, "errors": 0}
    memory = get_memory_client()
    engine = get_workflow_engine()
    github = GitHubClient(token=token)
    waits = list((memory.find_external_waits(event_type=DEPLOYMENT_EVENT, tenant_id=tenant_id) or {}).get("waits") or [])
    checked = resumed = errors = 0
    for wait in waits:
        match = dict(wait.get("match") or {})
        options = dict(wait.get("options") or {})
        workflow_id = str(wait.get("workflow_id") or "")
        repo = str(options.get("repo") or "")
        if not workflow_id or not repo or not match.get("sha") or not match.get("environment"):
            continue
        checked += 1
        try:
            state = deployment_state(
                github,
                repo=repo,
                sha=str(match["sha"]),
                environment=str(match["environment"]),
                deployment_id=options.get("deployment_id"),
            )
            if not state or state["state"] not in TERMINAL_DEPLOYMENT_STATES:
                continue
            payload = deployment_payload(match, repo, state)
            engine.resume_external(
                workflow_id,
                event_type=DEPLOYMENT_EVENT,
                payload=payload,
                actor="github-merge-poller",
                tenant_id=tenant_id,
                task_output={**payload, "event_type": DEPLOYMENT_EVENT, "external_event": payload},
            )
            resumed += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.warning("github_deployments_poller wait failed workflow_id=%s error=%s", workflow_id, exc)
    return {"checked": checked, "resumed": resumed, "errors": errors}


def poll_wave_joins(*, tenant_id: str = "default") -> dict[str, Any]:
    """Fallback for missed child notifications: re-evaluate every waiting wave join."""
    from ts_harness.workflows.work_order_waves import CHILD_EVENT, wave_result

    memory = get_memory_client()
    engine = get_workflow_engine()
    waits = list((memory.find_external_waits(event_type=CHILD_EVENT, tenant_id=tenant_id) or {}).get("waits") or [])
    checked = resumed = errors = 0
    for wait in waits:
        children = [dict(c) for c in (wait.get("options") or {}).get("children") or [] if isinstance(c, dict)]
        workflow_id = str(wait.get("workflow_id") or "")
        if not children or not workflow_id:
            continue
        checked += 1
        try:
            aggregate = wave_result(memory, children)
            if aggregate["state"] == "pending":
                continue
            engine.resume_external(
                workflow_id,
                event_type=CHILD_EVENT,
                payload={"wave_state": aggregate["state"], "source": "poller"},
                actor="github-merge-poller",
                tenant_id=tenant_id,
            )
            resumed += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.warning("wave_join_poller wait failed workflow_id=%s error=%s", workflow_id, exc)
    return {"checked": checked, "resumed": resumed, "errors": errors}


def _bound_channel(memory: Any, workflow_id: str) -> tuple[str, str] | None:
    """(channel, thread_ts) of the workflow's Slack binding, falling back to its parent."""
    seen: set[str] = set()
    while workflow_id and workflow_id not in seen:
        seen.add(workflow_id)
        workflow = (memory.get_workflow(workflow_id) or {}).get("workflow") or {}
        source = str(workflow.get("source_channel") or "")
        if source.count(":") >= 2:
            _, channel, thread_ts = source.split(":", 2)
            return channel, thread_ts
        workflow_id = str(workflow.get("parent_workflow_id") or "")
    return None


def _role_slack_mentions(role: str) -> list[str]:
    if not role:
        return []
    try:
        from ts_harness.gateway.server import get_runtime

        identity = get_runtime().config.identity
    except Exception:  # noqa: BLE001
        return []
    emails = {email for email, roles in (identity.roles or {}).items() if role in {r.lower() for r in roles}}
    return sorted(f"<@{slack_id}>" for slack_id, email in (identity.slack_user_emails or {}).items() if email.lower() in emails)


def _escalation_text(wait: dict[str, Any], *, kind: str, role: str, action: str, mentions: list[str]) -> str:
    step = wait.get("step_name") or "wait"
    if kind == "expired":
        text = f"`{step}` passed its deadline and stays blocked. Recommended action: `{action}`."
    else:
        text = f"`{step}` has been waiting since before {wait.get('escalate_at')} and needs attention."
    if role:
        text += f" Escalated to {role}" + (f": {' '.join(mentions)}" if mentions else ".")
    return text


def _notify_escalation(memory: Any, wait: dict[str, Any], *, kind: str, role: str, action: str) -> bool:
    binding = _bound_channel(memory, str(wait.get("workflow_id") or ""))
    if binding is None:
        return False
    from ts_harness.notifications.slack import SlackNotifier

    text = _escalation_text(wait, kind=kind, role=role, action=action, mentions=_role_slack_mentions(role))
    SlackNotifier().post_thread_text(text=text, channel=binding[0], thread_ts=binding[1])
    return True


def escalate_stale_waits(
    *,
    tenant_id: str = "default",
    memory: Any = None,
    notify: Callable[..., bool] | None = None,
) -> dict[str, Any]:
    """Escalate waits past escalate_at; mark waits past expires_at with a recommended action."""
    from ts_harness.workflows.wait_deadlines import recommended_action

    memory = memory or get_memory_client()
    notify = notify or _notify_escalation
    waits = list((memory.list_due_waits(tenant_id=tenant_id) or {}).get("waits") or [])
    escalated = expired = errors = 0
    for wait in waits:
        kind = "expired" if wait.get("due") == "expired" else "escalated"
        action = recommended_action(wait) if kind == "expired" else ""
        role = str((wait.get("approval_policy") or {}).get("escalate_to_role") or "")
        try:
            notified = bool(notify(memory, wait, kind=kind, role=role, action=action))
        except Exception as exc:  # noqa: BLE001
            logger.warning("wait escalation notify failed task_id=%s error=%s", wait.get("task_id"), exc)
            notified = False
        try:
            memory.mark_wait_deadline(
                str(wait["task_id"]),
                kind=kind,
                recommended_action=action,
                details={
                    "workflow_id": wait.get("workflow_id"),
                    "step_name": wait.get("step_name"),
                    "escalate_to_role": role or None,
                    "notified": notified,
                },
            )
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.warning("wait deadline not recorded task_id=%s error=%s", wait.get("task_id"), exc)
            continue
        if kind == "expired":
            expired += 1
        else:
            escalated += 1
    return {"checked": len(waits), "escalated": escalated, "expired": expired, "errors": errors}


OPEN_PROGRAM_STATUSES = frozenset({"running", "blocked"})


def scheduled_binding_key(job_name: str) -> str:
    return f"cron:{job_name}"


def _is_not_found(exc: Exception) -> bool:
    from ts_harness.memory.client import ProgramNotFound

    return isinstance(exc, ProgramNotFound) or "program not found" in str(exc).lower()


def _open_scheduled_run(memory: Any, binding_key: str, tenant_id: str) -> dict[str, Any] | None:
    """Newest run for this schedule when its derived status is still open.

    The stored program status stays ``running`` after completion, so the derived
    status from ``get_program_status`` decides whether the run is still open.
    """
    try:
        found = memory.find_program_by_binding(binding_key, tenant_id=tenant_id)
    except Exception as exc:
        if _is_not_found(exc):
            return None
        raise
    program = (found or {}).get("program") or {}
    program_id = str(program.get("id") or "")
    if not program_id:
        return None
    status = memory.get_program_status(program_id) or {}
    derived = str(status.get("status") or "")
    if derived not in OPEN_PROGRAM_STATUSES:
        return None
    return {
        "program_id": program_id,
        "derived_status": derived,
        "waiting_on": status.get("waiting_on") or None,
    }


def _post_run_thread(notify: dict[str, Any], *, summary: str, notifier: Any = None) -> tuple[str | None, dict[str, Any]]:
    from ts_harness.notifications.slack import SlackNotifier

    channel = str(notify.get("channel") or "").strip()
    text = str(notify.get("text") or "").strip() or f"Scheduled run: {summary}"
    try:
        response = (notifier or SlackNotifier()).post_thread_text(channel=channel, text=text)
    except Exception as exc:  # noqa: BLE001
        response = {"ok": False, "error": str(exc)}
    ts = str((response or {}).get("ts") or "")
    if not (response or {}).get("ok") or not ts:
        return None, {"status": "failed", "error": str((response or {}).get("error") or "no ts")}
    return f"slack:{channel}:{ts}", {"status": "posted", "channel": channel, "ts": ts}


def default_entry_node(program_type: str) -> str | None:
    from ts_harness.workflows.program_registry import get_program_definition_registry

    registry = get_program_definition_registry()
    registry.reload_if_changed()
    try:
        definition = registry.load(program_type)
    except (FileNotFoundError, ValueError):
        return None
    startable = list(definition.get("startable_nodes") or [])
    return str(startable[0]) if startable else None


def start_scheduled_program(
    payload: dict[str, Any],
    *,
    memory: Any = None,
    service: Any = None,
    notifier: Any = None,
) -> dict[str, Any]:
    """Start one program run per schedule tick, skipping while the previous run is open."""
    from datetime import UTC, datetime

    from ts_harness.workflows.delivery_program import (
        get_delivery_program_service,
        resolve_program_tenant,
    )

    job_name = str(payload.get("name") or "").strip()
    program_cfg = payload.get("program") if isinstance(payload.get("program"), dict) else {}
    program_type = str(program_cfg.get("program_type") or "").strip()
    summary = str(program_cfg.get("summary") or "").strip()
    if not job_name or not program_type or not summary:
        return {"status": "error", "error": "job name, program.program_type and program.summary are required"}

    memory = memory or get_memory_client()
    tenant_id = resolve_program_tenant(None)
    binding_key = scheduled_binding_key(job_name)
    try:
        open_run = _open_scheduled_run(memory, binding_key, tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("scheduled program guard lookup failed job=%s error=%s", job_name, exc)
        return {"status": "skipped", "reason": "guard_lookup_failed", "error": str(exc)}
    if open_run is not None:
        return {"status": "skipped", "reason": "previous_run_open", **open_run}

    service = service or get_delivery_program_service()
    entry_node = str(program_cfg.get("entry_node") or "").strip() or None
    if entry_node is None:
        entry_node = default_entry_node(program_type)
    if not entry_node:
        return {"status": "error", "error": f"no startable node for program type: {program_type}"}

    result: dict[str, Any] = {}
    source_channel: str | None = None
    notify = program_cfg.get("notify") if isinstance(program_cfg.get("notify"), dict) else None
    if notify and str(notify.get("channel") or "").strip():
        source_channel, result["notify"] = _post_run_thread(notify, summary=summary, notifier=notifier)

    artifacts = dict(program_cfg.get("artifacts") or {})
    artifacts["schedule_name"] = job_name
    artifacts["scheduled_at"] = datetime.now(UTC).isoformat()
    try:
        started = service.create_program(
            entry_node=entry_node,
            program_type=program_type,
            summary=summary,
            tenant_id=tenant_id,
            binding_key=binding_key,
            source_channel=source_channel,
            requester_actor_id="scheduler",
            artifacts=artifacts,
            extra_input=dict(program_cfg.get("input") or {}),
            actor="scheduler",
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc), **result}
    return {
        "status": "started",
        "program_id": getattr(started, "program_id", None),
        "workflow_id": getattr(started, "workflow_id", None),
        **result,
    }


SYSTEM_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "poll_github_merges": poll_github_merges,
    "escalate_stale_waits": escalate_stale_waits,
    "start_program": start_scheduled_program,
}

PAYLOAD_HANDLERS = frozenset({"start_program"})


def dispatch_system_job(payload: dict[str, Any]) -> dict[str, Any]:
    handler_name = str(payload.get("handler") or "").strip()
    handler = SYSTEM_HANDLERS.get(handler_name)
    if handler is None:
        return {"status": "error", "error": f"unknown system handler: {handler_name or '(empty)'}"}
    if handler_name in PAYLOAD_HANDLERS:
        return handler(payload)
    return handler()
