"""Read-only admin view of the live skill catalog plus derived used-by."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ts_harness.gateway.cron_store import load_cron_file
from ts_harness.repo_paths import workflows_dir


def _meta_visible(meta: Any) -> bool:
    category = str(getattr(meta, "category", "") or "")
    source = str(getattr(meta, "source_path", "") or "")
    if category == "drafts":
        return False
    if category == "coding-backend" or "coding-backend" in Path(source).parts:
        return False
    return True


def _iter_workflow_documents() -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    root = workflows_dir()
    if root.exists():
        for path in sorted(root.glob("*.json")):
            if path.name in {"schema.json", "manifest.json"}:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                documents.append(payload)
    try:
        from ts_harness.gateway.workflow_definitions_bff import workflow_definition_store

        store = workflow_definition_store()
        for item in store.list_active():
            identity = str((item or {}).get("workflow_type") or "")
            definition = store.get_active(identity) if identity else None
            if isinstance(definition, dict):
                documents.append(definition)
        for item in store.list_definitions():
            definition = item.get("definition") if isinstance(item, dict) else None
            if isinstance(definition, dict) and definition.get("stages"):
                documents.append(definition)
    except Exception:
        pass
    return documents


def _workflow_usages(skill_id: str, documents: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
    usages: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for document in documents if documents is not None else _iter_workflow_documents():
        workflow_type = str(document.get("workflow_type") or "")
        for stage in document.get("stages") or []:
            if not isinstance(stage, dict):
                continue
            hints = [str(item) for item in (stage.get("skill_hints") or [])]
            if skill_id not in hints:
                continue
            key = (workflow_type, str(stage.get("name") or ""))
            if key in seen:
                continue
            seen.add(key)
            usages.append({"workflow_type": workflow_type, "stage": key[1]})
    return usages


def _cron_usages(skill_id: str, jobs: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
    usages: list[dict[str, str]] = []
    for job in jobs if jobs is not None else load_cron_file():
        hints = [str(item) for item in (job.get("skill_hints") or [])]
        skill = str(job.get("skill_id") or "")
        if skill_id == skill or skill_id in hints:
            usages.append({"name": str(job.get("name") or "")})
    return usages


def _inbox_draft_href(skill_id: str, skills_root: str | Path | None) -> str | None:
    if not skills_root:
        return None
    try:
        from ts_harness.skills.authoring import list_drafts

        for draft in list_drafts(skills_root):
            draft_id = str(draft.get("id") or draft.get("skill_id") or "")
            if draft_id == skill_id:
                return f"#/inbox/skill_draft/{quote(skill_id, safe='')}"
    except Exception:
        return None
    return None


def _doctor_map(runtime: Any, skill_id: str) -> dict[str, list[str]]:
    catalog = getattr(runtime, "skill_catalog", None)
    config = getattr(runtime, "config", None)
    if catalog is None or config is None:
        return {}
    tenant_id = str(getattr(getattr(config, "gateway", None), "default_tenant_id", "default") or "default")
    out: dict[str, list[str]] = {}
    for agent in list(getattr(config, "agents", []) or []):
        agent_id = str(getattr(agent, "id", "") or "")
        if not agent_id:
            continue
        try:
            lines = catalog.doctor(agent, config, tenant_id=tenant_id)
        except Exception:
            lines = []
        matched = [line for line in lines if skill_id in str(line)]
        if matched:
            out[agent_id] = matched
    return out


def _agents_for_skill(runtime: Any, skill_id: str) -> list[str]:
    catalog = getattr(runtime, "skill_catalog", None)
    config = getattr(runtime, "config", None)
    if catalog is None or config is None:
        return []
    tenant_id = str(getattr(getattr(config, "gateway", None), "default_tenant_id", "default") or "default")
    agents: list[str] = []
    for agent in list(getattr(config, "agents", []) or []):
        agent_id = str(getattr(agent, "id", "") or "")
        if not agent_id:
            continue
        try:
            resolved, _ = catalog.resolve_for_agent(agent, config, tenant_id=tenant_id)
        except Exception:
            continue
        if any(getattr(skill.metadata, "id", "") == skill_id for skill in resolved):
            agents.append(agent_id)
    return agents


def _catalog_items(runtime: Any) -> list[Any]:
    catalog = getattr(runtime, "skill_catalog", None)
    items = list((getattr(catalog, "index", {}) or {}).values())
    visible = [meta for meta in items if _meta_visible(meta)]
    visible.sort(key=lambda meta: str(getattr(meta, "id", "")))
    return visible


def _row(runtime: Any, meta: Any, *, workflows=None, cron_jobs=None) -> dict[str, Any]:
    skill_id = str(getattr(meta, "id", "") or "")
    workflow_uses = _workflow_usages(skill_id, workflows)
    cron_uses = _cron_usages(skill_id, cron_jobs)
    doctor = _doctor_map(runtime, skill_id)
    blocked = [
        line
        for lines in doctor.values()
        for line in lines
        if "block" in line.lower()
    ]
    return {
        "id": skill_id,
        "category": str(getattr(meta, "category", "") or ""),
        "version": str(getattr(meta, "version", "") or ""),
        "trust_level": str(getattr(meta, "trust_level", "") or ""),
        "approval_status": str(getattr(meta, "approval_status", "") or ""),
        "origin": str(getattr(meta, "origin", "") or "builtin"),
        "description": str(getattr(meta, "description", "") or ""),
        "agents": _agents_for_skill(runtime, skill_id),
        "doctor_ok": not blocked,
        "used_by_workflows": len(workflow_uses),
        "used_by_cron": len(cron_uses),
    }


def list_skill_catalog(
    runtime: Any,
    *,
    workflows: list[dict[str, Any]] | None = None,
    cron_jobs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    skills = [
        _row(runtime, meta, workflows=workflows, cron_jobs=cron_jobs)
        for meta in _catalog_items(runtime)
    ]
    return {"skills": skills}


def get_skill_catalog_item(
    runtime: Any,
    skill_id: str,
    *,
    workflows: list[dict[str, Any]] | None = None,
    cron_jobs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    wanted = str(skill_id or "").strip()
    match = next((meta for meta in _catalog_items(runtime) if str(getattr(meta, "id", "")) == wanted), None)
    if match is None:
        raise KeyError(f"unknown skill: {wanted}")
    row = _row(runtime, match, workflows=workflows, cron_jobs=cron_jobs)
    from ts_harness.skills.roots import managed_root

    skills_root = managed_root() if getattr(runtime, "skill_catalog", None) is not None else None
    detail = {
        **row,
        "required_toolsets": list(getattr(match, "required_toolsets", []) or []),
        "required_connectors": list(getattr(match, "required_connectors", []) or []),
        "source_path": str(getattr(match, "source_path", "") or ""),
        "doctor": _doctor_map(runtime, wanted),
        "used_by": {
            "workflows": _workflow_usages(wanted, workflows),
            "cron": _cron_usages(wanted, cron_jobs),
        },
    }
    if str(getattr(match, "source_path", "") or "").endswith("SKILL.md"):
        from ts_harness.skills.catalog import skill_files

        detail["files"] = skill_files(match)
    href = _inbox_draft_href(wanted, skills_root)
    if href:
        detail["inbox_draft_href"] = href
    return detail
