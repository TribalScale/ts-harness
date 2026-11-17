"""What an approval gate asks a human to approve, for the console's inbox and program page.

A gate step has no content of its own: it approves what the steps before it produced. For a
planning gate that is a document (the PRD draft, the FRDs, the blueprints, the work orders)
with the agent's summary and warnings. For a release track's promote gate it is the release:
the repository, the commit, the PRs it contains, what CI said, the commit it rolls back to,
and for production, how the staging release went.
"""

from __future__ import annotations

from typing import Any

_DOC_TITLES = {
    "prd_markdown": "PRD",
    "frds_markdown": "Feature requirements (FRDs)",
    "blueprints_markdown": "Blueprints",
    "work_orders_markdown": "Work orders",
}
MAX_DOC_CHARS = 200_000


def _title(key: str) -> str:
    return _DOC_TITLES.get(key) or key.removesuffix("_markdown").replace("_", " ").capitalize()


# Plain fields that are documents: a Jira kickoff approves the ticket as the coding agent sees it.
_BODY_FIELDS = {"work_order_body": "Ticket as the coding agent will see it"}


def _documents(output: dict[str, Any]) -> list[dict[str, str]]:
    docs: list[dict[str, str]] = []
    for key, title in _BODY_FIELDS.items():
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            docs.append({"key": key, "title": title, "markdown": value[:MAX_DOC_CHARS]})
    for holder in (output.get("result"), output.get("artifacts"), output):
        if not isinstance(holder, dict):
            continue
        for key, value in holder.items():
            if key.endswith("_markdown") and isinstance(value, str) and value.strip():
                if any(doc["key"] == key for doc in docs):
                    continue
                docs.append({"key": key, "title": _title(key), "markdown": value[:MAX_DOC_CHARS]})
    return docs


def _warnings(output: dict[str, Any]) -> list[str]:
    warnings = output.get("warnings")
    return [str(w) for w in warnings if str(w).strip()] if isinstance(warnings, list) else []


def _ancestors(gate: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Steps before the gate, nearest first."""
    seen: set[str] = set()
    queue = [str(dep) for dep in gate.get("depends_on") or []]
    out: list[dict[str, Any]] = []
    while queue:
        task_id = queue.pop(0)
        if task_id in seen or task_id not in by_id:
            continue
        seen.add(task_id)
        task = by_id[task_id]
        out.append(task)
        queue.extend(str(dep) for dep in task.get("depends_on") or [])
    return out


def _latest(tasks: list[dict[str, Any]], step: str) -> dict[str, Any]:
    matches = [t for t in tasks if t.get("step_name") == step and isinstance(t.get("output"), dict)]
    if not matches:
        return {}
    return max(matches, key=lambda t: str(t.get("updated_at") or t.get("created_at") or ""))["output"]


def _release(gate: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    step = str(gate.get("step_name") or "")
    environment = step.removeprefix("approve_promote_") if step.startswith("approve_promote_") else ""
    plan = _latest(tasks, "plan_track")
    ci = _latest(tasks, "await_ci_on_sha")
    repo = str(plan.get("repo") or ci.get("repo") or "")
    sha = str(plan.get("sha") or ci.get("head_sha") or "")
    slot = ((plan.get("slots") or {}).get(environment) or {}) if environment else {}
    rollback = str(slot.get("rollback_sha") or "")
    release: dict[str, Any] = {
        "environment": environment,
        "repo": repo,
        "sha": sha,
        "base_branch": plan.get("base_branch"),
        "pr_urls": [str(u) for u in plan.get("pr_urls") or [] if u],
        "ci": {"state": ci.get("checks_state"), "checks": ci.get("checks") or {}, "failed": ci.get("failed_checks") or []},
        "rollback_sha": rollback or None,
        "required_roles": slot.get("required_roles") or [],
    }
    if repo and sha:
        release["commit_url"] = f"https://github.com/{repo}/commit/{sha}"
        if rollback and rollback != sha:
            release["compare_url"] = f"https://github.com/{repo}/compare/{rollback}...{sha}"
    environments = [str(e) for e in plan.get("environments") or []]
    if environment in environments and environments.index(environment) > 0:
        previous = environments[environments.index(environment) - 1]
        recorded = _latest(tasks, f"record_release_{previous}")
        if recorded:
            release["previous"] = {
                "environment": previous,
                "status": recorded.get("status"),
                "evidence_url": recorded.get("evidence_url"),
            }
    return release


def build_gate_review(gate: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {str(t.get("id")): t for t in tasks}
    review: dict[str, Any] = {
        "task_id": str(gate.get("id") or ""),
        "step_name": gate.get("step_name"),
        "workflow_type": gate.get("workflow_type"),
        "approval_status": gate.get("approval_status"),
    }
    if str(gate.get("workflow_type") or "") == "release_track" or str(gate.get("step_name") or "").startswith(
        "approve_promote_"
    ):
        review["kind"] = "release"
        review["release"] = _release(gate, tasks)
        return review
    # An agent step that is its own gate (a Jira kickoff) approves its own output first.
    for task in [gate, *_ancestors(gate, by_id)]:
        output = task.get("output") if isinstance(task.get("output"), dict) else {}
        docs = _documents(output)
        if not docs and not output.get("summary"):
            continue
        review.update(
            {
                "kind": "documents",
                "from_step": task.get("step_name"),
                "summary": str(output.get("summary") or ""),
                "warnings": _warnings(output),
                "confidence": output.get("confidence"),
                "documents": docs,
            }
        )
        return review
    review["kind"] = "none"
    return review


def gate_review(memory: Any, task_id: str) -> dict[str, Any] | None:
    """The review for gate ``task_id``, or None when the task is not found."""
    workflow_id = str((memory.task_program(task_id) or {}).get("workflow_id") or "")
    if not workflow_id:
        return None
    tasks = list((memory.get_workflow_tasks(workflow_id) or {}).get("tasks") or [])
    gate = next((t for t in tasks if str(t.get("id")) == str(task_id)), None)
    if gate is None:
        return None
    return build_gate_review(gate, tasks)
