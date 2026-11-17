"""Onboard a program's repositories before their first work orders.

When a program enters the implement phase, each repository its work orders target is
checked once. A repository whose CI runs an e2e or integration suite (or that has a
Playwright config) but has no ``.ts-harness.yml`` gets an onboarding work order
(``WO-0000``, ``WO-0001``, ...) that drafts the file from CI and has OpenCode make the
suite pass in a coding job. Every other work order for that repository depends on it, so
feature work starts only after the team merges the config PR. Repositories that are
already set up, or have no suite to run, are left alone.

Dependencies stay in the planner's markdown; the gate is the ``onboarding_gates``
artifact (``{onboarding WO id: "owner/name" or ""}``), which
``dependencies_from_artifacts`` applies, so work orders added later are gated too.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

GATES_KEY = "onboarding_gates"
STATUS_KEY = "repo_onboarding"
ID_PREFIX = "WO-"


@dataclass(frozen=True)
class Target:
    repo_url: str
    base_branch: str
    full_name: str = ""


@dataclass(frozen=True)
class Inspection:
    needed: bool
    reason: str
    body: str = ""


Inspector = Callable[[Target, str], Inspection]


def _full_name(repo_url: str) -> str:
    tail = str(repo_url or "").rstrip("/")
    if tail.endswith(".git"):
        tail = tail[:-4]
    parts = tail.split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else ""


def onboarding_targets(artifacts: dict[str, Any], fallback: Target | None = None) -> list[Target]:
    """Repositories the program's work orders go to, chosen the way the implement intake does.

    The project's repositories; else the repository configured for ``implement_work_orders``;
    else ``repo_url``. A program-level ``default_branch`` overrides the base branch except
    between several project repositories, where each keeps its own.
    """
    override = str(artifacts.get("default_branch") or "")
    repositories = [r for r in artifacts.get("project_repositories") or [] if isinstance(r, dict) and r.get("repo_url")]
    if repositories:
        return [
            Target(
                repo_url=str(r["repo_url"]),
                base_branch=str((override if len(repositories) == 1 else "") or r.get("default_branch") or override or "dev"),
                full_name=str(r.get("full_name") or _full_name(str(r["repo_url"]))),
            )
            for r in repositories
        ]
    repo_url = str((fallback.repo_url if fallback else "") or artifacts.get("repo_url") or "")
    if not repo_url:
        return []
    base = str(override or artifacts.get("base_branch") or (fallback.base_branch if fallback else "") or "dev")
    return [Target(repo_url=repo_url, base_branch=base)]


def configured_target() -> Target | None:
    """The repository ``implement_work_orders`` is configured for, when no project names one."""
    try:
        from ts_harness.implementation.config import repo_for_workflow

        repo = repo_for_workflow("implement_work_orders")
    except Exception:  # noqa: BLE001
        return None
    if not repo.url:
        return None
    return Target(repo_url=str(repo.url), base_branch=str(repo.base_branch or "dev"))


def inspect_repository(target: Target, work_order_id: str) -> Inspection:
    """Clone ``target`` at its base branch and decide whether it needs an onboarding work order."""
    from ts_harness.implementation.repo import github_authenticated_url
    from ts_harness.implementation.repo_onboarding import onboarding_work_order, repo_has_e2e_suite
    from ts_harness.implementation.test_services import CONFIG_NAMES
    from ts_harness.integrations.github_client import repo_token

    with tempfile.TemporaryDirectory() as tmp:
        clone_url = github_authenticated_url(target.repo_url, repo_token(target.repo_url) or "")
        repo = Path(tmp) / "repo"
        cloned = subprocess.run(
            ["git", "clone", "-q", "--depth", "1", "-b", target.base_branch, clone_url, str(repo)],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        if cloned.returncode != 0:
            detail = cloned.stderr.replace(clone_url, target.repo_url).strip()[-300:]
            return Inspection(False, f"clone failed: {detail}")
        if any((repo / name).is_file() for name in CONFIG_NAMES):
            return Inspection(False, "already has .ts-harness.yml")
        if not repo_has_e2e_suite(repo):
            return Inspection(False, "no e2e or integration suite in CI")
        return Inspection(True, "CI runs an e2e suite and .ts-harness.yml is missing", onboarding_work_order(work_order_id, repo))


def _free_ids(taken: set[str], count: int) -> list[str]:
    ids: list[str] = []
    n = 0
    while len(ids) < count:
        candidate = f"{ID_PREFIX}{n:04d}"
        if candidate not in taken:
            ids.append(candidate)
        n += 1
    return ids


def _with_repository_line(body: str, full_name: str) -> str:
    """Route the onboarding work order to its repository in a multi-repo program."""
    if not full_name:
        return body
    heading, _, rest = body.partition("\n")
    return f"{heading}\nRepository: {full_name}\n{rest}"


def should_check(node_id: str, artifacts: dict[str, Any]) -> bool:
    return (
        node_id == "implement"
        and STATUS_KEY not in artifacts
        and bool(artifacts.get("work_orders_markdown"))
        and isinstance(artifacts.get("work_order_ids"), list)
        and bool(artifacts.get("work_order_ids"))
    )


def with_repo_onboarding(
    artifacts: dict[str, Any],
    *,
    inspect: Inspector | None = None,
    fallback: Target | None = None,
    max_parallel: int | None = None,
) -> dict[str, Any]:
    """``artifacts`` with onboarding work orders added for repositories that need one.

    Runs once per program (``repo_onboarding`` records the outcome). A repository that can't
    be checked is recorded and skipped, never blocking the program.
    """
    from ts_harness.implementation.work_order_context import (
        DEFAULT_MAX_PARALLEL_WORK_ORDERS,
        dependencies_from_artifacts,
        select_ready_work_order_ids,
    )

    inspect = inspect or inspect_repository
    targets = onboarding_targets(artifacts, fallback if fallback is not None else configured_target())
    work_order_ids = [str(item).upper() for item in artifacts.get("work_order_ids") or []]
    ids = _free_ids(set(work_order_ids), len(targets))
    multi = len(targets) > 1 or bool(artifacts.get("project_repositories"))

    report: list[dict[str, Any]] = []
    sections: list[str] = []
    gates: dict[str, str] = {}
    for target, wo_id in zip(targets, ids):
        try:
            found = inspect(target, wo_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("onboarding check for %s failed: %s", target.repo_url, exc)
            found = Inspection(False, f"check failed: {exc}")
        entry: dict[str, Any] = {"repo_url": target.repo_url, "base_branch": target.base_branch, "reason": found.reason}
        if found.needed and found.body:
            entry["work_order_id"] = wo_id
            gates[wo_id] = target.full_name if multi else ""
            sections.append(_with_repository_line(found.body.strip(), target.full_name if multi else ""))
        report.append(entry)

    out = {**artifacts, STATUS_KEY: {"repositories": report}}
    if not gates:
        return out
    out["work_orders_markdown"] = str(artifacts["work_orders_markdown"]).rstrip() + "\n\n" + "\n\n".join(sections) + "\n"
    out["work_order_ids"] = [*gates, *work_order_ids]
    out[GATES_KEY] = gates
    wave = select_ready_work_order_ids(
        out["work_order_ids"],
        list(artifacts.get("completed_work_order_ids") or []),
        dependencies_from_artifacts(out),
        max_parallel=max_parallel or DEFAULT_MAX_PARALLEL_WORK_ORDERS,
    )
    if wave:
        out["next_work_order_id"] = wave[0]
        out["next_work_order_ids"] = wave
        out["work_order_ref"] = wave[0]
    return out
