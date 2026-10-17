"""Format and parse the Jira description pointer block."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_BASE_BRANCH = "dev"

_LABELS = ("WO", "Repo", "Program", "Library", "Manifest")


@dataclass(frozen=True)
class PointerBlock:
    work_order_id: str | None = None
    repo_id: str | None = None
    base_branch: str | None = None
    program_id: str | None = None
    library_url: str | None = None
    manifest_url: str | None = None

    @property
    def has_spec_pointers(self) -> bool:
        return bool(self.library_url or self.manifest_url or self.program_id)


def format_pointer_block(
    *,
    work_order_id: str,
    repo_id: str,
    base_branch: str = DEFAULT_BASE_BRANCH,
    program_id: str,
    library_url: str = "",
    manifest_url: str = "",
) -> str:
    repo = str(repo_id).strip()
    branch = str(base_branch or DEFAULT_BASE_BRANCH).strip() or DEFAULT_BASE_BRANCH
    lines = [
        f"WO: {str(work_order_id).strip()}",
        f"Repo: {repo}@{branch}",
        f"Program: {str(program_id).strip()}",
    ]
    if library_url:
        lines.append(f"Library: {str(library_url).strip()}")
    if manifest_url:
        lines.append(f"Manifest: {str(manifest_url).strip()}")
    return "\n".join(lines)


def _split_repo(raw: str) -> tuple[str | None, str | None]:
    value = raw.strip()
    if not value:
        return None, None
    if "@" in value:
        repo_id, branch = value.rsplit("@", 1)
        repo_id = repo_id.strip() or None
        branch = branch.strip() or None
        return repo_id, branch
    return value, None


def parse_pointer_block(text: str | None) -> PointerBlock:
    if not text:
        return PointerBlock()
    found: dict[str, str] = {}
    for line in str(text).splitlines():
        stripped = line.strip()
        if ":" not in stripped:
            continue
        label, value = stripped.split(":", 1)
        key = label.strip()
        if key in _LABELS:
            found[key] = value.strip()
    repo_id, branch = _split_repo(found.get("Repo") or "")
    return PointerBlock(
        work_order_id=found.get("WO") or None,
        repo_id=repo_id,
        base_branch=branch,
        program_id=found.get("Program") or None,
        library_url=found.get("Library") or None,
        manifest_url=found.get("Manifest") or None,
    )


def resolve_pr_base_branch(
    *,
    engineer_branch: str | None = None,
    pointer_branch: str | None = None,
    allowlist_default_branch: str | None = None,
    runtime_base_branch: str | None = None,
    runtime_repo_matches: bool = False,
) -> str:
    for candidate in (engineer_branch, pointer_branch, allowlist_default_branch):
        value = str(candidate or "").strip()
        if value:
            return value
    if runtime_repo_matches:
        runtime = str(runtime_base_branch or "").strip()
        if runtime:
            return runtime
    return DEFAULT_BASE_BRANCH


def pointer_from_mapping(data: dict[str, Any] | None) -> PointerBlock:
    payload = data if isinstance(data, dict) else {}
    repo = str(payload.get("repo_id") or payload.get("repo") or "").strip()
    branch = str(payload.get("base_branch") or payload.get("default_branch") or "").strip()
    return PointerBlock(
        work_order_id=str(payload.get("work_order_id") or "").strip() or None,
        repo_id=repo or None,
        base_branch=branch or None,
        program_id=str(payload.get("program_id") or "").strip() or None,
        library_url=str(payload.get("library_url") or "").strip() or None,
        manifest_url=str(payload.get("manifest_url") or "").strip() or None,
    )
