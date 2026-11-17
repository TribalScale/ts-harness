"""Which design references (images of what to build) each workflow step sees.

The workflow runner adds design input to a task just before it runs; no workflow definition
changes are needed:

- PRD and planning steps (``PLANNING_STEPS``) see every reference: ``design_refs`` (ids,
  titles, summaries) plus the images as ``_images``.
- A work order's coding job (``CODING_STEPS``) and QA (``qa_gate``) see the references the work
  order's ``Design`` row names (``| Design | D1, D3 |``). The coding job gets the images inside
  ``design_refs`` (data URLs) because workers have no access to the bucket; QA gets them as
  ``_images`` after its Playwright screenshots, to compare the two.

References come from the ``gather_sources`` step's output in a PRD workflow, and from the
workflow input (carried in program artifacts) everywhere else.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from ts_harness.intake.design import (
    AGENT_IMAGE_BYTES,
    AGENT_IMAGE_LIMIT,
    load_images,
    public_ref,
    refs_named_by_work_order,
    render_design_refs,
)

logger = logging.getLogger(__name__)

PLANNING_STEPS = frozenset(
    {
        "intake",
        "draft_prd",
        "revise_prd",
        "refine_requirements",
        "design_blueprints",
        "plan_work_orders",
        # The PRD -> Jira path: stories name the frames each one builds (`Design: D1`).
        "review_prd",
        "plan_stories",
    }
)
CODING_STEPS = frozenset({"deliver_work_order", "revise_work_order"})
QA_STEPS = frozenset({"qa_gate"})
# QA already carries up to 6 MB of screenshots; designs get the rest of the 10 MB body.
QA_IMAGE_LIMIT = 4
QA_IMAGE_BYTES = 3_000_000


def _refs_from(value: Any) -> list[dict[str, Any]]:
    return [ref for ref in value if isinstance(ref, dict) and ref.get("id")] if isinstance(value, list) else []


def design_refs_for_task(task: dict[str, Any], tasks: list[dict[str, Any]], task_input: dict[str, Any]) -> list[dict[str, Any]]:
    """The workflow's design references: a gather step's output, else the workflow input."""
    gathered = [
        item
        for item in tasks
        if item.get("status") == "succeeded"
        and isinstance(item.get("output"), dict)
        and isinstance(item["output"].get("design_refs"), list)
    ]
    if gathered:
        latest = sorted(gathered, key=lambda item: str(item.get("created_at") or ""))[-1]
        return _refs_from(latest["output"]["design_refs"])
    if _refs_from(task_input.get("design_refs")):
        return _refs_from(task_input.get("design_refs"))
    for item in sorted(tasks, key=lambda row: str(row.get("created_at") or "")):
        refs = _refs_from((item.get("input") or {}).get("design_refs") if isinstance(item.get("input"), dict) else None)
        if refs:
            return refs
    return []


def _work_order_body(task_input: dict[str, Any], tasks: list[dict[str, Any]]) -> str:
    body = str(task_input.get("work_order_body") or "")
    if body:
        return body
    for item in tasks:
        value = (item.get("input") or {}).get("work_order_body") if isinstance(item.get("input"), dict) else None
        if value:
            return str(value)
    return ""


def design_input(
    task: dict[str, Any],
    tasks: list[dict[str, Any]],
    task_input: dict[str, Any],
    *,
    store_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Keys to merge into ``task_input`` for this step; empty when it gets no design input."""
    step = str(task.get("step_name") or "")
    if step not in PLANNING_STEPS | CODING_STEPS | QA_STEPS:
        return {}
    refs = design_refs_for_task(task, tasks, task_input)
    if not refs:
        return {}
    if step not in PLANNING_STEPS:
        refs = refs_named_by_work_order(refs, _work_order_body(task_input, tasks))
        if not refs:
            return {}
    try:
        store = (store_factory or _intake_store)()
    except Exception as exc:  # noqa: BLE001 - the text references still go to the agent
        logger.warning("design images unavailable: %s", exc)
        store = None

    if step in CODING_STEPS:
        images = load_images(refs, store) if store is not None else []
        by_name = {image["name"].split(" ", 1)[0]: image["url"] for image in images}
        return {
            "design_refs": [
                {**public_ref(ref), "mime": ref.get("mime") or "image/png", "data_url": by_name.get(str(ref.get("id")), "")}
                for ref in refs
            ]
        }

    out: dict[str, Any] = {
        "design_refs": [public_ref(ref) for ref in refs],
        "design_refs_text": render_design_refs(refs),
    }
    if store is None:
        return out
    if step in QA_STEPS:
        images = load_images(refs, store, limit=QA_IMAGE_LIMIT, max_bytes=QA_IMAGE_BYTES)
        existing = list(task_input.get("_images") or [])
        out["_images"] = existing + images
        out["design_refs_text"] = render_design_refs(
            refs, heading="## Design references (compare the screenshots against these)"
        )
    else:
        out["_images"] = load_images(refs, store, limit=AGENT_IMAGE_LIMIT, max_bytes=AGENT_IMAGE_BYTES)
    return out


def _intake_store() -> Any:
    from ts_harness.intake.blobs import intake_store

    return intake_store()
