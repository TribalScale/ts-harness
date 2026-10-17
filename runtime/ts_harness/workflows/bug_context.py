"""Read-only repo context for a Slack bug report.

``review-agent`` cannot clone or call GitHub. ``gather_bug_context`` fetches a
truncated default-branch tree and the bodies of paths the report names.
"""

from __future__ import annotations

import base64
import re
from typing import Any

from ts_harness.integrations.github_client import GitHubClient, parse_repo_slug
from ts_harness.workflows.expressions import resolve_value
from ts_harness.workflows.hooks._helpers import load_workflow_payload

TREE_PATH_LIMIT = 200
FILE_LIMIT = 8
FILE_CHAR_LIMIT = 20_000

_PATH_RE = re.compile(r"(?<![\w@./-])((?:[\w.-]+/)+[\w.-]+(?:\.[\w.-]+)?)")


def _input_extras(task: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    extras: dict[str, Any] = {}
    for item in sorted(tasks, key=lambda row: str(row.get("created_at") or "")):
        if isinstance(item.get("input"), dict):
            extras.update(
                {key: value for key, value in item["input"].items() if not str(key).startswith("_") and value is not None}
            )
        output = item.get("output")
        if isinstance(output, dict):
            for key in ("repo", "default_branch"):
                if output.get(key) is not None:
                    extras[key] = output[key]
    if isinstance(task.get("input"), dict):
        extras.update(
            {key: value for key, value in task["input"].items() if not str(key).startswith("_") and value is not None}
        )
    return extras


def _report_text(task: dict[str, Any], tasks: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in [task, *tasks]:
        payload = item.get("input")
        if not isinstance(payload, dict):
            continue
        for key in ("summary", "context", "bug_report"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value)
    return "\n".join(parts)


def _path_mentioned(text: str, path: str) -> int:
    pattern = re.compile(r"(?<![\w./-])" + re.escape(path) + r"(?![\w./-])")
    match = pattern.search(text)
    return match.start() if match else -1


def cited_paths(report: str, tree_paths: list[str]) -> list[str]:
    """Paths named in the report that exist in the fetched tree, earliest first."""
    text = str(report or "")
    known = {path for path in tree_paths if path}
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    for match in _PATH_RE.finditer(text):
        path = match.group(1)
        if path in known and path not in seen:
            seen.add(path)
            found.append((match.start(), path))
    for path in tree_paths:
        if path in seen:
            continue
        index = _path_mentioned(text, path)
        if index >= 0:
            seen.add(path)
            found.append((index, path))
    found.sort(key=lambda item: item[0])
    return [path for _index, path in found[:FILE_LIMIT]]


def _tree_paths(payload: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for entry in list(payload.get("tree") or []):
        if not isinstance(entry, dict) or entry.get("type") != "blob":
            continue
        path = str(entry.get("path") or "").strip()
        if not path:
            continue
        paths.append(path)
        if len(paths) >= TREE_PATH_LIMIT:
            break
    return paths


def _decode_file(payload: dict[str, Any]) -> tuple[str, bool]:
    if str(payload.get("encoding") or "") != "base64" or not payload.get("content"):
        return "", True
    raw = base64.b64decode(str(payload.get("content")))
    text = raw.decode("utf-8", errors="replace")
    if len(text) > FILE_CHAR_LIMIT:
        return text[:FILE_CHAR_LIMIT], True
    return text, bool(payload.get("truncated"))


def gather_bug_context(
    task: dict[str, Any],
    config: dict[str, Any],
    *,
    memory: Any,
    github: GitHubClient | None = None,
) -> dict[str, Any]:
    workflow, tasks = load_workflow_payload(memory, str(task.get("workflow_id") or ""))
    extras = _input_extras(task, tasks)
    repo = str(
        resolve_value(config.get("repo"), tasks=tasks, workflow=workflow, extras=extras) or extras.get("repo") or ""
    ).strip()
    default_branch = str(
        resolve_value(config.get("default_branch"), tasks=tasks, workflow=workflow, extras=extras)
        or extras.get("default_branch")
        or ""
    ).strip()
    if not repo:
        raise ValueError("gather_bug_context resolved no repo")
    if not default_branch:
        raise ValueError("gather_bug_context resolved no default_branch")
    parsed = parse_repo_slug(repo)
    if parsed is None:
        raise ValueError(f"gather_bug_context cannot parse repo: {repo}")
    owner, name = parsed
    client = github or GitHubClient.from_env()
    tree_payload = client.get_tree(owner, name, default_branch, recursive=True)
    if not isinstance(tree_payload, dict):
        raise ValueError("gather_bug_context received no tree")  # noqa: TRY004
    paths = _tree_paths(tree_payload)
    report = _report_text(task, tasks)
    files: list[dict[str, Any]] = []
    for path in cited_paths(report, paths):
        payload = client.get_file(owner, name, path, ref=default_branch)
        if not isinstance(payload, dict) or payload.get("type") not in (None, "file"):
            continue
        body, truncated = _decode_file(payload)
        files.append({"path": path, "body": body, "truncated": truncated})
    return {
        "repo": repo,
        "default_branch": default_branch,
        "tree": paths,
        "files": files,
    }
