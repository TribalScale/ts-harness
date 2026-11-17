"""Resolve the repo a ``repo_pick`` gate records when a human approves it.

Approval gates never run an agent, so the gate output is the only place
``repo`` and ``default_branch`` can come from. They are taken from the
program's effective repo allowlist, never from free text alone.
"""

from __future__ import annotations

import re
from typing import Any

REPO_PICK_CONTRACT = "repo_pick"


def is_repo_pick_stage(stage: Any) -> bool:
    return str(getattr(stage, "contract_ref", "") or "") == REPO_PICK_CONTRACT


def allowlisted_repos(program_id: str | None) -> list[dict[str, str]]:
    from ts_harness.tools.workflow_tools import _load_program_snapshot, _resolved_config_for_snapshot

    snapshot = _load_program_snapshot(program_id=program_id or None)
    picker = _resolved_config_for_snapshot(snapshot).get("repo_picker") or {}
    return [dict(item) for item in picker.get("repos") or [] if isinstance(item, dict) and item.get("id")]


def _mentions(text: str, repo_id: str) -> bool:
    pattern = rf"(?<![\w./-]){re.escape(repo_id)}(?![\w-])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def mentioned_repos(text: str, repos: list[dict[str, str]]) -> list[dict[str, str]]:
    return [repo for repo in repos if _mentions(str(text or ""), str(repo["id"]))]


def resolve_repo_pick(repos: list[dict[str, str]], texts: list[str]) -> dict[str, str] | None:
    """The one allowlisted repo the texts name (earliest text wins), else the only allowlisted repo."""
    for text in texts:
        found = mentioned_repos(text, repos)
        if len(found) == 1:
            return {"repo": found[0]["id"], "default_branch": found[0]["default_branch"]}
    if len(repos) == 1:
        return {"repo": repos[0]["id"], "default_branch": repos[0]["default_branch"]}
    return None


def repo_pick_prompt(repos: list[dict[str, str]]) -> str:
    if not repos:
        return "No repos are on the allowlist. Ask an operator to add one in System Settings, then reply approve."
    if len(repos) == 1:
        repo = repos[0]
        return f"Reply approve to use {repo['id']} (branch {repo['default_branch']}), or no to cancel."
    return "Which repo is this for? Reply with one of: " + ", ".join(repo["id"] for repo in repos) + "."


def repo_pick_output(
    *,
    program_id: str | None,
    reply_text: str = "",
    request_text: str = "",
) -> dict[str, str]:
    repos = allowlisted_repos(program_id)
    resolved = resolve_repo_pick(repos, [reply_text, request_text])
    if resolved is None:
        raise ValueError(repo_pick_prompt(repos))
    return resolved
