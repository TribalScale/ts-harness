"""Admin BFF helpers: inbox aggregate, cron CRUD, auth config, sessions."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

from ts_harness.gateway.auth import AuthContext
from ts_harness.gateway.cron_store import (
    cron_config_path,
    load_cron_file,
    save_cron_file,
    validate_cron_job,
    validate_skill_hints_against_catalog,
)
from ts_harness.tools.approvals import get_approval_queue


def auth_config_payload(runtime: Any) -> dict[str, Any]:
    identity = runtime.config.identity
    allow_static = os.environ.get("TS_HARNESS_ALLOW_STATIC_TOKEN", "true").lower() in {
        "1",
        "true",
        "yes",
    }
    if getattr(identity, "provider", "static") == "oidc":
        from ts_harness.gateway.identity_presets import identity_setting, uses_legacy_endpoints
        from ts_harness.gateway.oidc_discovery import discover

        issuer = (identity.issuer or "").rstrip("/")
        client_id = (
            getattr(identity, "client_id", None)
            or os.environ.get("OIDC_CLIENT_ID")
            or ""
        )
        authz = getattr(identity, "authorization_endpoint", None) or os.environ.get(
            "OIDC_AUTHORIZATION_ENDPOINT"
        )
        token_ep = getattr(identity, "token_endpoint", None) or os.environ.get("OIDC_TOKEN_ENDPOINT")
        jwks_url = identity.jwks_url
        legacy = uses_legacy_endpoints(identity)
        if issuer and not legacy and not (authz and token_ep and jwks_url):
            doc = discover(identity.issuer)
            authz = authz or doc.get("authorization_endpoint")
            token_ep = token_ep or doc.get("token_endpoint")
            jwks_url = jwks_url or doc.get("jwks_uri")
        if legacy and issuer:
            authz = authz or f"{issuer}/authorize"
            token_ep = token_ep or f"{issuer}/oauth/token"
        return {
            "mode": "oidc",
            "issuer": identity.issuer,
            "client_id": client_id,
            "authorization_endpoint": authz or "",
            "token_endpoint": token_ep or "",
            "audience": identity.audience,
            "token_type": identity_setting(identity, "token_type"),
            "scopes": (os.environ.get("OIDC_SCOPES") or "openid profile email").split(),
            "jwks_url": jwks_url,
            "allow_static_fallback": allow_static
            and os.environ.get("TS_HARNESS_OIDC_STATIC_FALLBACK", "false").lower()
            in {"1", "true", "yes"},
        }
    if not allow_static:
        return {
            "mode": "disabled",
            "error": "static token auth disabled; configure identity.provider=oidc",
            "scopes": [],
        }
    return {"mode": "static", "scopes": [], "allow_static_fallback": True}


def auth_config_for_request(runtime: Any, auth: Any | None) -> dict[str, Any]:
    payload = auth_config_payload(runtime)
    payload["authenticated"] = auth is not None
    if auth is None:
        payload["permissions"] = []
        return payload
    from ts_harness.policy.permissions import actor_permissions

    payload["permissions"] = sorted(actor_permissions(auth, getattr(runtime, "config", None)))
    payload["roles"] = [str(role) for role in (getattr(auth, "roles", None) or [])]
    return payload


def is_admin(auth: AuthContext, runtime: Any) -> bool:
    from ts_harness.policy.permissions import actor_permissions

    return "edit_config" in actor_permissions(auth, getattr(runtime, "config", None))


def build_inbox(
    runtime: Any,
    auth: AuthContext,
    *,
    memory: Any,
    skills_root: str,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []

    if is_admin(auth, runtime):
        try:
            from ts_harness.skills.authoring import list_drafts

            drafts = list_drafts(skills_root)
        except Exception:
            drafts = []
        for draft in drafts:
            skill_id = str(draft.get("id") or draft.get("skill_id") or "")
            items.append(
                {
                    "type": "skill_draft",
                    "id": skill_id,
                    "title": skill_id,
                    "subtitle": f"Proposed skill · {draft.get('approval_status') or 'draft'}",
                    "created_at": draft.get("updated_at") or draft.get("created_at"),
                    "href": f"/#/inbox/skill_draft/{quote(skill_id, safe='')}",
                    "meta": draft,
                }
            )

    for approval in get_approval_queue().list_pending():
        data = approval.__dict__ if hasattr(approval, "__dict__") else dict(approval)
        approval_id = str(data.get("approval_id") or data.get("id") or "")
        if data.get("status") not in {None, "pending"}:
            continue
        items.append(
            {
                "type": "tool_approval",
                "id": approval_id,
                "title": str(data.get("tool_name") or "tool"),
                "subtitle": f"{data.get('agent_id') or 'agent'} · tool approval",
                "created_at": data.get("requested_at"),
                "href": f"/#/inbox/tool_approval/{quote(approval_id, safe='')}",
                "meta": data,
            }
        )

    from ts_harness.gateway.program_access import list_actor_params

    try:
        listed = memory.list_programs(
            tenant_id=auth.tenant_id, status=None, limit=100, offset=0, actor_params=list_actor_params(auth)
        )
    except Exception:
        listed = {"programs": []}
    approvable = _approvable_program_filter(memory, auth)
    for program in list(listed.get("programs") or []):
        program_id = str(program.get("id") or "")
        if not program_id:
            continue
        if program.get("status") not in {"running", "blocked"}:
            continue
        try:
            status = memory.get_program_status(program_id)
        except Exception:
            continue
        waiting = status.get("waiting_on") or {}
        if waiting.get("kind") != "approval":
            continue
        if not approvable(program_id):
            continue
        match = waiting.get("match") or {}
        task_id = str(match.get("task_id") or "")
        step = str(match.get("step_name") or status.get("active_step") or "gate")
        items.append(
            {
                "type": "workflow_gate",
                "id": task_id or program_id,
                "title": step,
                "subtitle": f"Program {program.get('display_name') or program.get('summary') or program_id}",
                "created_at": waiting.get("since"),
                "href": f"/#/inbox/workflow_gate/{quote(task_id or program_id, safe='')}",
                "meta": {
                    "program_id": program_id,
                    "task_id": task_id,
                    "step_name": step,
                    "waiting_on": waiting,
                    "recommended_action": status.get("recommended_action"),
                },
            }
        )

    items.extend(_stuck_step_items(memory, auth, approvable))
    items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {"items": items, "total": len(items)}


_STUCK_REASONS = {"revision_limit": "Revision limit reached"}


def _approvable_program_filter(memory: Any, auth: AuthContext, need: str = "approve") -> Any:
    """Predicate: does this actor have *need* on the program? Always true unless enforcing."""
    from ts_harness.gateway.program_access import (
        ProgramAccessDenied,
        access_mode,
        actor_context,
        check_program_access,
    )

    actor = actor_context(auth)
    if actor is None or access_mode() != "enforce":
        return lambda program_id: True
    cache: dict[str, bool] = {}

    def approvable(program_id: str | None) -> bool:
        if not program_id:
            return True
        if program_id not in cache:
            try:
                check_program_access(
                    memory, program_id, actor, need, tenant_id=str(auth.tenant_id or "default"), route="inbox"
                )
                cache[program_id] = True
            except ProgramAccessDenied:
                cache[program_id] = False
        return cache[program_id]

    return approvable


def _stuck_step_items(memory: Any, auth: AuthContext, approvable: Any = None) -> list[dict[str, Any]]:
    """Steps parked for a human to revise again, accept or stop."""
    try:
        stuck = list(memory.list_stuck_tasks(tenant_id=auth.tenant_id) or [])
    except Exception:
        return []
    from ts_harness.workflows.engine import get_workflow_engine
    from ts_harness.workflows.human_decisions import stuck_summary

    engine = get_workflow_engine()
    items: list[dict[str, Any]] = []
    for task in stuck:
        task_id = str(task.get("id") or "")
        workflow_id = str(task.get("workflow_id") or "")
        if not task_id or not workflow_id:
            continue
        try:
            summary = stuck_summary(engine, workflow_id, task_id)
        except Exception:
            continue
        if approvable is not None and not approvable(summary.get("program_id")):
            continue
        reason = _STUCK_REASONS.get(summary["reason"], "Blocked")
        items.append(
            {
                "type": "stuck_step",
                "id": task_id,
                "title": f"{summary['work_order']} · {summary['step_name']}",
                "subtitle": f"{reason} · waiting for your decision",
                "created_at": task.get("updated_at") or task.get("created_at"),
                "href": f"/#/inbox/stuck_step/{quote(task_id, safe='')}",
                "meta": {**summary, "status": "blocked", "updated_at": task.get("updated_at")},
            }
        )
    return items


def build_portfolio(
    memory: Any,
    auth: AuthContext,
    *,
    status: str | None = None,
    phase: str | None = None,
    owner: str | None = None,
    waiting_hours: float | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Root programs with their wait, escalation and recommended action for the portfolio board."""
    from ts_harness.gateway.program_access import list_actor_params

    listed = memory.list_programs(
        tenant_id=auth.tenant_id,
        status=status,
        limit=limit,
        offset=offset,
        roots_only=True,
        phase=phase,
        owner=owner,
        actor_params=list_actor_params(auth),
    )
    rows: list[dict[str, Any]] = []
    for program in list(listed.get("programs") or []):
        program_id = str(program.get("id") or "")
        try:
            program_status = memory.get_program_status(program_id) if program_id else {}
        except Exception:
            program_status = {}
        waiting = program_status.get("waiting_on") or {}
        age = waiting.get("age_seconds")
        rows.append(
            {
                **program,
                "owner": program.get("requester_actor_id"),
                "phase": program.get("current_phase"),
                "status": program_status.get("status") or program.get("status"),
                "waiting_on_kind": waiting.get("kind"),
                "waiting_on_event": waiting.get("event_type"),
                "wait_age_seconds": age,
                "escalated": bool(waiting.get("escalated")),
                "recommended_action": program_status.get("recommended_action"),
                "compensation_status": (program.get("artifacts") or {}).get("compensation_status"),
            }
        )
    if waiting_hours is not None and waiting_hours > 0:
        threshold = waiting_hours * 3600
        rows = [row for row in rows if (row.get("wait_age_seconds") or 0) >= threshold]
    return {
        "programs": rows,
        "total": listed.get("total", len(rows)),
        "limit": listed.get("limit", limit),
        "offset": listed.get("offset", offset),
    }


def list_cron_jobs_admin(runtime: Any) -> dict[str, Any]:
    jobs = runtime.scheduler.list_cron_jobs()
    return {"jobs": jobs, "total": len(jobs), "path": str(cron_config_path())}


def create_cron_job_admin(runtime: Any, body: dict[str, Any]) -> dict[str, Any]:
    jobs = load_cron_file()
    names = {str(j.get("name")) for j in jobs}
    job = validate_cron_job(body, existing_names=names)
    hints = list(job.get("skill_hints") or [])
    validate_skill_hints_against_catalog(hints, getattr(runtime, "skill_catalog", None))
    jobs.append(job)
    save_cron_file(jobs)
    runtime.scheduler.replace_cron_jobs(jobs)
    return {"job": job}


def update_cron_job_admin(runtime: Any, name: str, body: dict[str, Any]) -> dict[str, Any]:
    jobs = load_cron_file()
    idx = next((i for i, j in enumerate(jobs) if str(j.get("name")) == name), -1)
    if idx < 0:
        raise KeyError(f"cron job not found: {name}")
    merged = {**jobs[idx], **body, "name": name}
    job = validate_cron_job(merged, existing_names=None)
    hints = list(job.get("skill_hints") or [])
    validate_skill_hints_against_catalog(hints, getattr(runtime, "skill_catalog", None))
    jobs[idx] = job
    save_cron_file(jobs)
    runtime.scheduler.replace_cron_jobs(jobs)
    return {"job": job}


def delete_cron_job_admin(runtime: Any, name: str) -> dict[str, Any]:
    jobs = load_cron_file()
    kept = [j for j in jobs if str(j.get("name")) != name]
    if len(kept) == len(jobs):
        raise KeyError(f"cron job not found: {name}")
    save_cron_file(kept)
    runtime.scheduler.replace_cron_jobs(kept)
    return {"deleted": name}


def _memory_client() -> Any:
    from ts_harness.memory.client import get_memory_client

    return get_memory_client()


def list_sessions_admin(runtime: Any, auth: AuthContext, *, channel: str | None = "web") -> dict[str, Any]:
    from ts_harness.agent.sessions import get_session_store

    store = get_session_store()
    sessions = store.list_for_tenant(auth.tenant_id)
    if channel:
        sessions = [
            s
            for s in sessions
            if s.source == channel or (s.routing_key or "").startswith(f"{channel}:")
        ]
    readable = _approvable_program_filter(_memory_client(), auth, "read")
    sessions = [s for s in sessions if readable(str((s.metadata or {}).get("program_id") or "").strip() or None)]
    # Prefer actor-owned web sessions when available
    actor = auth.actor_id
    if actor:
        owned = [s for s in sessions if actor in (s.routing_key or "") or s.agent_id]
        if owned:
            sessions = sessions
    payload = [
        {
            "session_id": s.session_id,
            "tenant_id": s.tenant_id,
            "agent_id": s.agent_id,
            "source": s.source,
            "routing_key": s.routing_key,
            "binding_key": str((s.metadata or {}).get("binding_key") or ""),
            "started_at": getattr(s, "started_at", None),
        }
        for s in sorted(
            sessions,
            key=lambda s: str(getattr(s, "started_at", None) or ""),
            reverse=True,
        )[:100]
    ]
    return {"sessions": payload, "total": len(payload)}


def search_sessions_admin(
    auth: AuthContext, *, q: str, limit: int = 20
) -> dict[str, Any]:
    from dataclasses import asdict

    from ts_harness.agent.transcript import get_transcript_store

    hits = get_transcript_store().search(
        tenant_id=auth.tenant_id, query=q, limit=limit
    )
    return {"hits": [asdict(hit) for hit in hits]}


def session_messages_admin(session_id: str, auth: AuthContext) -> dict[str, Any]:
    from ts_harness.agent.sessions import get_session_store
    from ts_harness.agent.transcript import get_transcript_store

    session = get_session_store().get(session_id)
    # A cross-tenant session is reported as missing so callers cannot probe for
    # session existence in other tenants.
    if session is None or session.tenant_id != auth.tenant_id:
        raise KeyError("session not found")
    metadata = session.metadata or {}
    program_id = str(metadata.get("program_id") or "").strip()
    if program_id and not _approvable_program_filter(_memory_client(), auth, "read")(program_id):
        raise KeyError("session not found")
    messages = get_transcript_store().load_messages(session_id)
    payload: dict[str, Any] = {
        "session_id": session_id,
        "agent_id": session.agent_id,
        "source": session.source,
        "messages": messages,
    }
    if program_id:
        payload["program_id"] = program_id
    return payload
