"""Infer and normalize a program's single project_id."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


def normalize_project_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("project_id is required")
    if "/" in text:
        return text.strip("/")
    if text.replace("-", "").replace("_", "").isalnum() and text.isascii():
        return text.upper()
    return text


def _jira_key(artifacts: dict[str, Any]) -> str | None:
    for key in ("jira_project_key", "project_key"):
        raw = artifacts.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if text:
            return normalize_project_id(text)
    return None


def _repo_project(artifacts: dict[str, Any]) -> str | None:
    raw = artifacts.get("repo_url")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    parsed = urlparse(text if "://" in text else f"https://{text}")
    path = (parsed.path or "").strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return None


def infer_project_id(artifacts: dict[str, Any] | None) -> tuple[str | None, str | None]:
    bag = dict(artifacts or {})
    jira = _jira_key(bag)
    if jira:
        return jira, "inferred_jira"
    repo = _repo_project(bag)
    if repo:
        return repo, "inferred_repo"
    return None, None


def resolve_program_project(
    artifacts: dict[str, Any] | None,
    *,
    project_id: str | None = None,
) -> tuple[str | None, str | None]:
    if project_id is not None and str(project_id).strip():
        return normalize_project_id(project_id), "user"
    return infer_project_id(artifacts)
