"""Deterministic project briefs and related-program attach payloads."""

from __future__ import annotations

from typing import Any

BRIEF_CONTENT_LIMIT = 2000
RELATED_CONTEXT_KEY = "related_program_context"
LIVE_STATUSES = {"running", "blocked"}


def _label(value: Any, fallback: str) -> str:
    if isinstance(value, dict):
        for key in ("title", "prd_title", "name", "summary"):
            text = str(value.get(key) or "").strip()
            if text:
                return text[:BRIEF_CONTENT_LIMIT]
        return fallback
    text = str(value or "").strip()
    return (text or fallback)[:BRIEF_CONTENT_LIMIT]


def _as_text_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, dict):
        return [f"{key}: {item}" for key, item in value.items() if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def extract_brief_entries(*, phase: str, artifacts: dict[str, Any] | None) -> list[dict[str, Any]]:
    bag = dict(artifacts or {})
    phase_name = str(phase or "").strip()
    if not phase_name:
        raise ValueError("phase is required")
    entries: list[dict[str, Any]] = []

    if bag.get("approved_prd") is not None:
        entries.append(
            {
                "phase": phase_name,
                "kind": "pointer",
                "topic": "approved_prd",
                "content": _label(bag.get("approved_prd"), "Approved PRD"),
                "artifact_ref": "approved_prd",
            }
        )
    if bag.get("work_orders_markdown"):
        entries.append(
            {
                "phase": phase_name,
                "kind": "pointer",
                "topic": "work_orders_markdown",
                "content": "Work orders pack",
                "artifact_ref": "work_orders_markdown",
            }
        )
    pr_urls = bag.get("pr_urls") if isinstance(bag.get("pr_urls"), list) else []
    pr_url = bag.get("pr_url") or (pr_urls[0] if pr_urls else None)
    if pr_url:
        entries.append(
            {
                "phase": phase_name,
                "kind": "pointer",
                "topic": "pr_url",
                "content": str(pr_url),
                "artifact_ref": "pr_url",
            }
        )
    if bag.get("jira_issue_key"):
        entries.append(
            {
                "phase": phase_name,
                "kind": "pointer",
                "topic": "jira_issue_key",
                "content": str(bag.get("jira_issue_key")),
                "artifact_ref": "jira_issue_key",
            }
        )
    for topic in ("repo_url", "jira_project_key", "base_branch"):
        if bag.get(topic):
            entries.append(
                {
                    "phase": phase_name,
                    "kind": "fact",
                    "topic": topic,
                    "content": str(bag.get(topic)),
                    "artifact_ref": topic,
                }
            )
    if bag.get("constraints"):
        for index, item in enumerate(_as_text_list(bag.get("constraints"))):
            entries.append(
                {
                    "phase": phase_name,
                    "kind": "directive",
                    "topic": f"constraint:{index}",
                    "content": item[:BRIEF_CONTENT_LIMIT],
                    "artifact_ref": "constraints",
                }
            )
    decisions = bag.get("decisions") or bag.get("architecture_decisions_markdown")
    if decisions:
        for index, item in enumerate(_as_text_list(decisions)):
            entries.append(
                {
                    "phase": phase_name,
                    "kind": "decision",
                    "topic": f"decision:{index}",
                    "content": item[:BRIEF_CONTENT_LIMIT],
                    "artifact_ref": "decisions"
                    if bag.get("decisions") is not None
                    else "architecture_decisions_markdown",
                }
            )
    return entries


def _program_record(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    program = payload.get("program") if isinstance(payload.get("program"), dict) else payload
    return dict(program or {})


def _empty_context(project_id: str | None, warning: str | None = None) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "sources": [],
        "warnings": [warning] if warning else [],
    }


def _source_payload(
    program: dict[str, Any],
    *,
    via: str,
    brief_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    status = str(program.get("status") or "")
    phase = str(program.get("current_phase") or program.get("entry_phase") or "")
    live_status = None
    if status in LIVE_STATUSES:
        live_status = {"phase": phase, "state": status}
    return {
        "program_id": str(program.get("id") or program.get("program_id") or ""),
        "project_id": program.get("project_id"),
        "status": status,
        "current_phase": phase,
        "via": via,
        "live_status": live_status,
        "brief_entries": brief_entries,
    }


def resolve_related_program_context(
    memory_client: Any,
    *,
    tenant_id: str,
    program_id: str | None,
    project_id: str | None,
) -> dict[str, Any]:
    try:
        return _resolve_related_program_context(
            memory_client,
            tenant_id=tenant_id,
            program_id=program_id,
            project_id=project_id,
        )
    except Exception:
        return _empty_context(project_id, "related_program_context_unavailable")


def _resolve_related_program_context(
    memory_client: Any,
    *,
    tenant_id: str,
    program_id: str | None,
    project_id: str | None,
) -> dict[str, Any]:
    self_id = str(program_id or "")
    sources: dict[str, dict[str, Any]] = {}

    if project_id:
        listed = memory_client.list_programs(tenant_id=tenant_id, project_id=project_id, limit=100)
        programs = listed.get("programs") if isinstance(listed, dict) else None
        for row in programs if isinstance(programs, list) else []:
            other_id = str(row.get("id") or "")
            if not other_id or other_id == self_id:
                continue
            if row.get("excluded_from_autoshare"):
                continue
            if str(row.get("tenant_id") or tenant_id) != tenant_id:
                continue
            sources[other_id] = _source_payload(row, via="autoshare", brief_entries=[])

    if self_id and hasattr(memory_client, "list_program_links"):
        links = memory_client.list_program_links(self_id)
        outbound = links.get("outbound") if isinstance(links, dict) else None
        for link in outbound if isinstance(outbound, list) else []:
            target_id = str(link.get("to_program_id") or "")
            if not target_id or target_id == self_id:
                continue
            payload = memory_client.get_program(target_id)
            target = _program_record(payload)
            if str(target.get("tenant_id") or "") != tenant_id:
                continue
            existing = sources.get(target_id)
            via = "link" if existing is None else existing.get("via") or "link"
            if existing is None:
                via = "link"
            elif existing.get("via") == "autoshare":
                via = "link"
            merged = {**target, "id": target_id}
            sources[target_id] = _source_payload(merged, via=via, brief_entries=[])

    for other_id, source in list(sources.items()):
        briefs = memory_client.list_program_briefs(other_id) if hasattr(memory_client, "list_program_briefs") else {}
        raw_entries = briefs.get("entries") if isinstance(briefs, dict) else None
        entries = raw_entries if isinstance(raw_entries, list) else []
        source["brief_entries"] = [
            {
                "kind": item.get("kind"),
                "topic": item.get("topic"),
                "content": item.get("content"),
                "artifact_ref": item.get("artifact_ref"),
                "phase": item.get("phase"),
            }
            for item in entries
        ]

    return {
        "project_id": project_id,
        "sources": list(sources.values()),
        "warnings": [],
    }


def attach_related_program_context(
    memory_client: Any,
    *,
    program_id: str,
    tenant_id: str,
    project_id: str | None,
    actor: str = "orchestrator",
    extra_warnings: list[str] | None = None,
) -> dict[str, Any]:
    context = resolve_related_program_context(
        memory_client,
        tenant_id=tenant_id,
        program_id=program_id,
        project_id=project_id,
    )
    if extra_warnings:
        context.setdefault("warnings", []).extend(extra_warnings)
    try:
        memory_client.patch_program_artifacts(
            program_id,
            {RELATED_CONTEXT_KEY: context},
            merge=True,
            actor=actor,
        )
    except Exception:
        context.setdefault("warnings", []).append("related_program_context_persist_failed")
    return context


def rebuild_related_program_context(
    memory_client: Any,
    *,
    program_id: str,
    tenant_id: str,
    actor: str = "orchestrator",
) -> dict[str, Any]:
    try:
        payload = memory_client.get_program(program_id)
        program = _program_record(payload)
        project_id = program.get("project_id")
    except Exception:
        project_id = None
    return attach_related_program_context(
        memory_client,
        program_id=program_id,
        tenant_id=tenant_id,
        project_id=project_id,
        actor=actor,
    )


def publish_program_brief(
    memory_client: Any,
    *,
    program_id: str,
    tenant_id: str,
    project_id: str | None,
    phase: str,
    artifacts: dict[str, Any] | None,
    actor: str = "orchestrator",
) -> dict[str, Any]:
    entries = extract_brief_entries(phase=phase, artifacts=artifacts)
    if not entries:
        return {"program_id": program_id, "entries": []}
    result = memory_client.upsert_program_briefs(
        program_id,
        entries,
        tenant_id=tenant_id,
        project_id=project_id,
        actor=actor,
    )
    if project_id:
        try:
            summary = "; ".join(
                f"{item['kind']}:{item['topic']}" for item in entries[:8]
            )
            memory_client.remember(
                tenant_id=tenant_id,
                agent_id="workflow-continuity",
                content=f"Program {program_id} {phase} brief: {summary}",
                memory_type="semantic",
                tags=["program-brief", f"program:{program_id}"],
                scope=f"project:{project_id}",
            )
        except Exception:
            pass
    return result if isinstance(result, dict) else {"program_id": program_id, "entries": entries}
