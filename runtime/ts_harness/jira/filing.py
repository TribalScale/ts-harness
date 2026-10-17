"""Build Jira create payloads for work-order stories. Never set assignee."""

from __future__ import annotations

from typing import Any

from ts_harness.jira.pointer_block import DEFAULT_BASE_BRANCH, format_pointer_block


def issue_type_for_project(
    project_key: str,
    *,
    default_issue_type: str = "Story",
    issue_type_by_project: dict[str, str] | None = None,
) -> str:
    mapping = issue_type_by_project or {}
    configured = str(mapping.get(project_key) or "").strip()
    return configured or str(default_issue_type or "Story").strip() or "Story"


def build_story_payload(
    *,
    project_key: str,
    summary: str,
    acceptance: str,
    pointer: dict[str, str],
    issue_type: str = "Story",
    epic_key: str | None = None,
    allowed_projects: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    allowed = {str(key).strip().upper() for key in (allowed_projects or []) if str(key or "").strip()}
    if allowed and str(project_key or "").strip().upper() not in allowed:
        raise ValueError(f"Jira project {project_key} is outside the tenant binding")
    block = format_pointer_block(
        work_order_id=str(pointer.get("work_order_id") or ""),
        repo_id=str(pointer.get("repo_id") or ""),
        base_branch=str(pointer.get("base_branch") or DEFAULT_BASE_BRANCH),
        program_id=str(pointer.get("program_id") or ""),
        library_url=str(pointer.get("library_url") or ""),
        manifest_url=str(pointer.get("manifest_url") or ""),
    )
    description = "\n\n".join(part for part in (acceptance.strip(), block) if part)
    fields: dict[str, Any] = {
        "project": {"key": project_key},
        "issuetype": {"name": issue_type},
        "summary": summary,
        "description": description,
    }
    if epic_key:
        fields["parent"] = {"key": epic_key}
    if "assignee" in fields:
        del fields["assignee"]
    return {"fields": fields}


def build_dependency_links(story_keys_by_wo: dict[str, str], dependencies: dict[str, list[str]]) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for wo_id, blockers in dependencies.items():
        inward = story_keys_by_wo.get(wo_id)
        if not inward:
            continue
        for blocker_wo in blockers:
            outward = story_keys_by_wo.get(blocker_wo)
            if not outward:
                continue
            links.append(
                {
                    "inward_key": inward,
                    "outward_key": outward,
                    "type": "Blocks",
                }
            )
    return links
