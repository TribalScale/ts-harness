"""Deterministic barrier that aggregates delivery PR results from children.

The Jira ``implement_delivery`` graph expands into N dynamic implement tasks and
then joins on a static ``collect_delivery_results`` barrier. The barrier's
``depends_on`` is patched at plan-approval time with the created task IDs, so the
barrier can aggregate exactly the children it fans in over.

Aggregation is fully deterministic (no LLM): it produces a deduped, sorted
``pr_urls`` set plus a per-PR ``prs[]`` mapping with the owning work item. Any
failed or cancelled child blocks the barrier rather than dropping PRs silently.
"""

from __future__ import annotations

from typing import Any

from ts_harness.workflows.hooks._helpers import load_workflow_payload
from ts_harness.workflows.registry import HookContext


def _work_item_ref(child: dict[str, Any]) -> str:
    output = child.get("output") if isinstance(child.get("output"), dict) else {}
    child_input = child.get("input") if isinstance(child.get("input"), dict) else {}
    for source, key in (
        (output, "work_item_ref"),
        (child_input, "work_item_ref"),
        (child_input, "jira_key"),
        (child_input, "issue_key"),
    ):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(child.get("step_name") or "unknown")


def aggregate_delivery_results(
    barrier_task: dict[str, Any],
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate child PR results for a barrier task.

    Returns ``{"status": "succeeded", "output": {...}}`` when every child
    succeeded with a PR URL, otherwise ``{"status": "blocked", "reason": ...}``.
    """
    dep_ids = {str(dep) for dep in (barrier_task.get("depends_on") or [])}
    children = [task for task in tasks if str(task.get("id")) in dep_ids]
    if not children:
        return {"status": "blocked", "reason": "barrier has no delivery children"}

    for child in children:
        status = str(child.get("status") or "")
        if status in {"failed", "cancelled"}:
            return {
                "status": "blocked",
                "reason": f"child {child.get('step_name')} is {status}",
            }
        if status != "succeeded":
            return {"status": "blocked", "reason": "delivery children are not all terminal"}

    prs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for child in sorted(
        children, key=lambda task: (str(task.get("step_name")), str(task.get("id")))
    ):
        output = child.get("output") if isinstance(child.get("output"), dict) else {}
        pr_url = output.get("pr_url")
        if not (isinstance(pr_url, str) and pr_url.strip()):
            return {
                "status": "blocked",
                "reason": f"child {child.get('step_name')} succeeded without a pr_url",
            }
        pr_url = pr_url.strip()
        if pr_url in seen:
            continue
        seen.add(pr_url)
        prs.append(
            {
                "pr_url": pr_url,
                "work_item_ref": _work_item_ref(child),
                "status": "success",
            }
        )

    pr_urls = sorted(seen)
    return {
        "status": "succeeded",
        "output": {
            "pr_urls": pr_urls,
            "result": {"pr_urls": pr_urls, "prs": prs},
        },
    }


def handle(ctx: HookContext) -> dict[str, Any]:
    if ctx.task is None:
        raise ValueError("task required for collect_delivery_results")
    _, tasks = load_workflow_payload(ctx.memory_client, ctx.workflow_id)
    barrier_task = next(
        (task for task in tasks if str(task.get("id")) == str(ctx.task["id"])),
        ctx.task,
    )
    return aggregate_delivery_results(barrier_task, tasks)
