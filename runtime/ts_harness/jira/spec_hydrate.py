"""Hydrate a spec pack from a Jira issue plus optional program Drive library."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ts_harness.jira.pointer_block import (
    DEFAULT_BASE_BRANCH,
    PointerBlock,
    parse_pointer_block,
    resolve_pr_base_branch,
)

GOVERNED_DOCS_MISSING = "governed docs missing"


class MemoryProgramClient(Protocol):
    def get_program(self, program_id: str) -> dict[str, Any]: ...


@dataclass
class SpecPack:
    source: str
    pointer: PointerBlock
    base_branch: str = DEFAULT_BASE_BRANCH
    warning: str | None = None
    drive_library: dict[str, Any] = field(default_factory=dict)
    issue_body: str = ""


def _issue_description(issue: dict[str, Any]) -> str:
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    raw = fields.get("description")
    if isinstance(raw, dict):
        return str(raw.get("content") or raw.get("text") or "")
    return str(raw or issue.get("description") or "")


def _program_drive_library(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    program = payload.get("program") if isinstance(payload.get("program"), dict) else payload
    metadata = program.get("metadata") if isinstance(program.get("metadata"), dict) else {}
    artifacts = metadata.get("artifacts") if isinstance(metadata.get("artifacts"), dict) else {}
    library = artifacts.get("drive_library") or program.get("drive_library")
    return dict(library) if isinstance(library, dict) else {}


def open_blockers(issuelinks: list[dict[str, Any]] | None) -> list[str]:
    keys: list[str] = []
    for link in issuelinks or []:
        if not isinstance(link, dict):
            continue
        link_type = link.get("type") if isinstance(link.get("type"), dict) else {}
        inward = str(link_type.get("inward") or "").strip().lower()
        outward = str(link_type.get("outward") or "").strip().lower()
        blocked_by = None
        if inward == "is blocked by" and isinstance(link.get("inwardIssue"), dict):
            blocked_by = link["inwardIssue"]
        elif outward == "is blocked by" and isinstance(link.get("outwardIssue"), dict):
            blocked_by = link["outwardIssue"]
        elif str(link.get("type") or "").strip().lower() == "is blocked by":
            blocked_by = link.get("inwardIssue") or link.get("outwardIssue")
        if not isinstance(blocked_by, dict):
            continue
        status = ""
        fields = blocked_by.get("fields") if isinstance(blocked_by.get("fields"), dict) else {}
        raw_status = fields.get("status") if isinstance(fields.get("status"), dict) else {}
        status = str(raw_status.get("name") or blocked_by.get("status") or "").strip().lower()
        category = ""
        if isinstance(raw_status.get("statusCategory"), dict):
            category = str(raw_status["statusCategory"].get("key") or "").strip().lower()
        if status in {"done", "cancelled", "canceled", "won't do"} or category == "done":
            continue
        key = str(blocked_by.get("key") or "").strip()
        if key:
            keys.append(key)
    return keys


def hydrate_spec_pack(
    issue: dict[str, Any],
    memory_client: MemoryProgramClient | None = None,
    *,
    engineer_branch: str | None = None,
    allowlist_default_branch: str | None = None,
    runtime_base_branch: str | None = None,
    runtime_repo_url: str | None = None,
) -> SpecPack:
    body = _issue_description(issue)
    pointer = parse_pointer_block(body)
    drive_library: dict[str, Any] = {}
    source = "jira_body"
    warning: str | None = None

    if pointer.library_url or pointer.manifest_url:
        source = "pointer"
        drive_library = {
            "library_url": pointer.library_url,
            "manifest_url": pointer.manifest_url,
        }
    elif pointer.program_id and memory_client is not None:
        try:
            drive_library = _program_drive_library(memory_client.get_program(pointer.program_id))
        except Exception:
            drive_library = {}
        if drive_library:
            source = "drive_library"
        else:
            warning = GOVERNED_DOCS_MISSING
    else:
        if not pointer.has_spec_pointers:
            warning = GOVERNED_DOCS_MISSING

    runtime_repo_matches = False
    if runtime_repo_url and pointer.repo_id:
        runtime_repo_matches = pointer.repo_id in str(runtime_repo_url)

    base_branch = resolve_pr_base_branch(
        engineer_branch=engineer_branch,
        pointer_branch=pointer.base_branch,
        allowlist_default_branch=allowlist_default_branch,
        runtime_base_branch=runtime_base_branch,
        runtime_repo_matches=runtime_repo_matches,
    )
    return SpecPack(
        source=source,
        pointer=pointer,
        base_branch=base_branch,
        warning=warning,
        drive_library=drive_library,
        issue_body=body,
    )
