"""Release one track per repository and order tracks by work-order dependencies.

``deploy_release`` forks a ``release_track`` child per repository that merged work in
the program. A track whose work orders depend on work in another repository waits at
each environment until that upstream track has recorded a successful release there.
A failed upstream blocks only the tracks that depend on it.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from ts_harness.config.bindings import check_repo_allowed, tenant_bindings
from ts_harness.implementation.work_order_context import (
    parse_work_order_dependencies,
    parse_work_order_repositories,
    resolve_repository,
)
from ts_harness.integrations.github_client import parse_github_pr_url
from ts_harness.workflows.hooks._helpers import load_workflow_payload
from ts_harness.workflows.hooks.fork_subworkflow import fork_child
from ts_harness.workflows.release import (
    ReleaseBlocked,
    _live_program_defaults,
    _latest_step,
    resolve_repo,
)
from ts_harness.workflows.work_order_waves import child_state

logger = logging.getLogger(__name__)

UPSTREAM_EVENT = "release.upstream_recorded"
TRACK_WORKFLOW_TYPE = "release_track"
TRACK_PLAN_STEP = "plan_track"
FORK_STEP = "plan_release_tracks"


def _task_input(task: dict[str, Any]) -> dict[str, Any]:
    raw = task.get("input")
    return dict(raw) if isinstance(raw, dict) else {}


def _output(task: dict[str, Any] | None) -> dict[str, Any]:
    raw = (task or {}).get("output")
    return dict(raw) if isinstance(raw, dict) else {}


def _pr_repo(url: str) -> tuple[str, str] | None:
    """``(owner/name, host)`` of a GitHub PR URL."""
    parsed = parse_github_pr_url(str(url or ""))
    if not parsed:
        return None
    return f"{parsed[0]}/{parsed[1]}".lower(), (urlparse(str(url)).hostname or "github.com")


def _merged_by_repo(merged: list[Any]) -> dict[str, dict[str, Any]]:
    """Merged work orders grouped by repository; the latest merge per repository is released."""
    by_repo: dict[str, dict[str, Any]] = {}
    for item in merged:
        if not isinstance(item, dict):
            continue
        found = _pr_repo(str(item.get("pr_url") or ""))
        sha = str(item.get("merge_sha") or "").strip()
        if not found or not sha:
            continue
        repo, host = found
        entry = by_repo.setdefault(repo, {"host": host, "sha": "", "merged_at": "", "work_order_ids": [], "pr_urls": []})
        merged_at = str(item.get("merged_at") or "")
        if not entry["sha"] or merged_at >= entry["merged_at"]:
            entry["sha"], entry["merged_at"] = sha, merged_at
        work_order_id = str(item.get("work_order_id") or "").strip().upper()
        if work_order_id and work_order_id not in entry["work_order_ids"]:
            entry["work_order_ids"].append(work_order_id)
        if item.get("pr_url") and item["pr_url"] not in entry["pr_urls"]:
            entry["pr_urls"].append(str(item["pr_url"]))
    return by_repo


def _upstream_edges(markdown: str, work_order_repo: dict[str, str]) -> dict[str, set[str]]:
    """``{repo: {repos it depends on}}`` from work-order ``Depends on`` rows across repositories."""
    declared = {wo: repo.lower() for wo, repo in parse_work_order_repositories(markdown).items()}
    located = {**declared, **work_order_repo}
    edges: dict[str, set[str]] = {}
    for wo_id, deps in parse_work_order_dependencies(markdown).items():
        source = located.get(wo_id)
        for dep in deps:
            target = located.get(dep)
            if source and target and source != target:
                edges.setdefault(source, set()).add(target)
    return edges


def _ordered(repos: list[str], edges: dict[str, set[str]]) -> list[str]:
    """Repositories with upstreams first; a dependency cycle cannot be released."""
    ordered: list[str] = []
    state: dict[str, int] = {}

    def visit(repo: str, path: list[str]) -> None:
        if state.get(repo) == 2:
            return
        if state.get(repo) == 1:
            cycle = path[path.index(repo):] + [repo]
            raise ReleaseBlocked(f"repositories depend on each other: {' -> '.join(cycle)}")
        state[repo] = 1
        for upstream in sorted(edges.get(repo, ())):
            visit(upstream, path + [repo])
        state[repo] = 2
        ordered.append(repo)

    for repo in sorted(repos):
        visit(repo, [])
    return ordered


def plan_tracks(task_input: dict[str, Any]) -> list[dict[str, Any]]:
    """One track per merged repository with its SHA and upstream repositories, upstreams first."""
    repositories = [r for r in task_input.get("project_repositories") or [] if isinstance(r, dict)]
    by_repo = _merged_by_repo(list(task_input.get("merged_work_orders") or []))
    base_branch = str(task_input.get("base_branch") or "").strip() or None
    if not by_repo:
        repo = resolve_repo(task_input)
        sha = str(task_input.get("merge_sha") or task_input.get("sha") or "").strip()
        if not sha:
            raise ReleaseBlocked("release needs merge_sha")
        return [
            {
                "repo": repo,
                "repo_url": str(task_input.get("repo_url") or "") or None,
                "sha": sha,
                "base_branch": base_branch,
                "pr_urls": list(task_input.get("pr_urls") or []),
                "work_order_ids": [],
                "upstream": [],
            }
        ]

    work_order_repo = {wo: repo for repo, info in by_repo.items() for wo in info["work_order_ids"]}
    edges = {
        repo: {up for up in upstream if up in by_repo}
        for repo, upstream in _upstream_edges(str(task_input.get("work_orders_markdown") or ""), work_order_repo).items()
        if repo in by_repo
    }
    tracks = []
    for repo in _ordered(list(by_repo), edges):
        info = by_repo[repo]
        registered = resolve_repository(repo, repositories) or {}
        tracks.append(
            {
                "repo": repo,
                "repo_url": str(registered.get("repo_url") or f"https://{info['host']}/{repo}"),
                "sha": info["sha"],
                "base_branch": str(registered.get("default_branch") or "") or base_branch,
                "pr_urls": sorted(info["pr_urls"]),
                "work_order_ids": sorted(info["work_order_ids"]),
                "upstream": sorted(edges.get(repo, ())),
            }
        )
    return tracks


def plan_release_tracks(
    task: dict[str, Any],
    config: dict[str, Any],
    *,
    memory: Any,
    program_defaults: dict[str, Any] | None = None,
    actor: str = "workflow-runner",
) -> dict[str, Any]:
    """Fork a ``release_track`` child per repository and return them for the join."""
    workflow_id = str(task.get("workflow_id") or "")
    workflow, tasks = load_workflow_payload(memory, workflow_id)
    stored = next((item for item in tasks if str(item.get("id")) == str(task.get("id"))), {})
    task_input = {**_task_input(stored), **_task_input(task)}
    tracks = plan_tracks(task_input)
    defaults = program_defaults if program_defaults is not None else _live_program_defaults()
    bindings = tenant_bindings(defaults)
    for track in tracks:
        error = check_repo_allowed(track["repo"], bindings)
        if error:
            raise ReleaseBlocked(error)

    child_type = str(config.get("workflow_type") or TRACK_WORKFLOW_TYPE)
    children = [
        {"workflow_id": str(child["workflow_id"]), "label": str(child["label"])}
        for child in task_input.get("_forked_children") or []
        if isinstance(child, dict) and child.get("workflow_id")
    ]
    already = {child["label"] for child in children}
    for track in tracks:
        if track["repo"] in already:
            continue
        intake = {
            "repo_url": track["repo_url"] or f"https://github.com/{track['repo']}",
            "merge_sha": track["sha"],
            "base_branch": track["base_branch"],
            "pr_urls": track["pr_urls"],
            "upstream": track["upstream"],
            "track_repo": track["repo"],
        }
        child_id = fork_child(
            memory,
            parent_workflow_id=workflow_id,
            tenant_id=str(task.get("tenant_id") or workflow.get("tenant_id") or "default"),
            child_type=child_type,
            intake_input=intake,
            actor=actor,
            definition_version=task_input.get("_child_version"),
        )
        children.append({"workflow_id": child_id, "label": track["repo"]})
        task_input["_forked_children"] = list(children)
        memory.patch_task_input(str(task.get("id")), task_input, actor=actor)

    return {
        "tracks": tracks,
        "order": [track["repo"] for track in tracks],
        "children": children,
        "child_workflow_ids": [child["workflow_id"] for child in children],
        "child_workflow_type": child_type,
    }


def track_plan(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    return _output(_latest_step(tasks, TRACK_PLAN_STEP))


def sibling_tracks(memory: Any, workflow: dict[str, Any]) -> dict[str, str]:
    """``{repo: workflow_id}`` of every track the parent release started."""
    parent_id = str(workflow.get("parent_workflow_id") or "")
    if not parent_id:
        return {}
    _, parent_tasks = load_workflow_payload(memory, parent_id)
    plan = _output(_latest_step(parent_tasks, FORK_STEP))
    return {
        str(child.get("label") or "").lower(): str(child.get("workflow_id") or "")
        for child in plan.get("children") or []
        if isinstance(child, dict) and child.get("workflow_id")
    }


def upstream_status(memory: Any, workflow: dict[str, Any], tasks: list[dict[str, Any]], slot: str) -> dict[str, Any]:
    """``ready`` when every upstream track recorded ``slot``; ``failed`` when one of them failed."""
    upstream = [str(repo).lower() for repo in track_plan(tasks).get("upstream") or []]
    siblings = sibling_tracks(memory, workflow) if upstream else {}
    pending: list[str] = []
    failed: list[str] = []
    for repo in upstream:
        sibling_id = siblings.get(repo)
        if not sibling_id:
            failed.append(repo)
            continue
        sibling, sibling_tasks = load_workflow_payload(memory, sibling_id)
        if _latest_step(sibling_tasks, f"record_release_{slot}") is not None:
            continue
        if child_state(sibling, sibling_tasks) == "failed" or sibling.get("status") in {"failed", "cancelled"}:
            failed.append(repo)
        else:
            pending.append(repo)
    state = "failed" if failed else ("pending" if pending else "ready")
    return {"state": state, "upstream": upstream, "pending": pending, "failed": failed}


def upstream_slot(config: dict[str, Any]) -> str:
    spec = config.get("upstream")
    return str((spec or {}).get("environment") or "") if isinstance(spec, dict) else ""


def prepare_upstream_wait(
    task: dict[str, Any],
    config: dict[str, Any],
    *,
    memory: Any,
    actor: str = "workflow-runner",
) -> dict[str, Any]:
    """Pin the wait to this track's upstreams; ``ready`` when they released here already."""
    slot = upstream_slot(config)
    if not slot:
        raise ValueError("upstream wait needs upstream.environment")
    workflow, tasks = load_workflow_payload(memory, str(task.get("workflow_id") or ""))
    status = upstream_status(memory, workflow, tasks, slot)
    if status["state"] == "failed":
        raise ReleaseBlocked(f"upstream release failed: {', '.join(status['failed'])} {slot}")
    repo = str(track_plan(tasks).get("repo") or "")
    match = {"repo": repo, "environment": slot}
    options = {"environment": slot, "upstream": status["upstream"], "pending": status["pending"]}
    memory.patch_task_input(
        str(task["id"]),
        {**_task_input(task), "_wait_match": match, "_wait_options": options},
        actor=actor,
    )
    result: dict[str, Any] = {"match": match, "options": options}
    if status["state"] == "ready":
        result["ready"] = {"repo": repo, "environment": slot, "upstream": status["upstream"]}
    return result


def notify_dependents(
    memory: Any,
    workflow: dict[str, Any],
    tasks: list[dict[str, Any]],
    *,
    slot: str,
    state: str,
    actor: str = "workflow-runner",
) -> list[str]:
    """Wake sibling tracks that wait on this track at ``slot``; returns the woken workflow ids."""
    repo = str(track_plan(tasks).get("repo") or "").lower()
    if not repo or not workflow.get("parent_workflow_id"):
        return []
    from ts_harness.workflows.engine import get_workflow_engine

    woken: list[str] = []
    own_id = str(workflow.get("id") or "")
    for sibling_repo, sibling_id in sibling_tracks(memory, workflow).items():
        if sibling_id == own_id:
            continue
        _, sibling_tasks = load_workflow_payload(memory, sibling_id)
        if repo not in [str(r).lower() for r in track_plan(sibling_tasks).get("upstream") or []]:
            continue
        payload = {"repo": sibling_repo, "environment": slot, "upstream_repo": repo, "state": state}
        try:
            get_workflow_engine().resume_external(
                sibling_id,
                event_type=UPSTREAM_EVENT,
                payload=payload,
                actor=actor,
                tenant_id=str(workflow.get("tenant_id") or "default"),
            )
            woken.append(sibling_id)
        except ValueError as exc:
            logger.info("track %s is not waiting on %s %s yet: %s", sibling_repo, repo, slot, exc)
    return woken
