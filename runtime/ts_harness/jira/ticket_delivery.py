"""Get a Jira ticket ready to code: design references and the repository onboarding check.

The ``prepare_ticket_delivery`` system step runs between ``hydrate_ticket`` and the coding job
in ``implement_jira_ticket``, so a ticket gets what a planned work order gets:

- **Design references.** A ticket filed from a PRD points at its program (``Program:`` in the
  description); that program's ``design_refs`` (D1, D2, ...) come along, and the coding job
  and QA see the ones the ticket names (``Design: D1``). Images attached to the ticket are
  stored as further references and named in the work order body, so they are always used.
- **Onboarding.** A repository whose CI runs an e2e or integration suite but has no
  ``.ts-harness.yml`` cannot be verified the way CI runs it. The work order then also asks
  the coding job to add the file (the same instructions as an onboarding work order), so
  the ticket's PR sets the repository up.

Both are best effort: a lookup that fails is reported in ``warnings`` and the ticket runs.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from ts_harness.intake.design import MAX_DESIGN_REFS, number_refs, store_design_image

logger = logging.getLogger(__name__)

MAX_ATTACHMENT_BYTES = 8_000_000
ONBOARDING_ID = "WO-0000"


def _program_design_refs(memory: Any, task_id: str) -> list[dict[str, Any]]:
    """Design references of the ticket's program, or of the program it points at."""
    program_id = str((memory.task_program(task_id) or {}).get("program_id") or "")
    seen: set[str] = set()
    while program_id and program_id not in seen and len(seen) < 5:
        seen.add(program_id)
        status = memory.get_program_status(program_id) or {}
        refs = (status.get("artifacts") or {}).get("design_refs")
        if isinstance(refs, list) and refs:
            return [ref for ref in refs if isinstance(ref, dict) and ref.get("id")]
        payload = memory.get_program(program_id) or {}
        program = payload.get("program") if isinstance(payload.get("program"), dict) else payload
        program_id = str(program.get("parent_program_id") or "")
    return []


def _attachment_refs(jira: Any, key: str, store: Any, tenant_id: str) -> list[dict[str, Any]]:
    refs = []
    for item in jira.image_attachments(key):
        if item["size"] > MAX_ATTACHMENT_BYTES:
            continue
        data = jira.attachment_content(item["id"])
        if not data:
            continue
        refs.append(
            {
                "title": item["filename"] or f"{key} attachment",
                "source": "jira",
                "url": "",
                "mime": item["mime"],
                "object_key": store_design_image(store, tenant_id=tenant_id, data=data, mime=item["mime"]),
                "summary": f"Image attached to {key}",
            }
        )
    return refs


def _with_design_line(body: str, ids: list[str]) -> str:
    """Add ``ids`` to the body's Design row or line (only the first is read), or add a line."""
    from ts_harness.intake.design import _DESIGN_LINE, _DESIGN_ROW

    if not ids:
        return body
    match = _DESIGN_ROW.search(body) or _DESIGN_LINE.search(body)
    if match is None:
        return body.rstrip() + "\n\n## Design\n\n- Design: " + ", ".join(ids) + "\n"
    named = match.group(1).rstrip()
    extra = [i for i in ids if not re.search(rf"\b{i}\b", named)]
    if not extra:
        return body
    return body[: match.start(1)] + named + ", " + ", ".join(extra) + body[match.start(1) + len(named) :]


_TICKET_KEYS = ("work_order_body", "repo_url", "base_branch", "jira_issue_key")


def _ticket_values(task: dict[str, Any], config: dict[str, Any], memory: Any) -> dict[str, Any]:
    """The ticket as ``hydrate_ticket`` read it, and the workflow's issue key."""
    from ts_harness.workflows.hooks._helpers import load_workflow_payload

    values = {k: config.get(k) for k in _TICKET_KEYS if config.get(k)}
    values.update({k: v for k, v in (task.get("input") or {}).items() if k in _TICKET_KEYS and v})
    if all(values.get(k) for k in _TICKET_KEYS):
        return values
    workflow, tasks = load_workflow_payload(memory, str(task.get("workflow_id") or ""))
    hydrated = [t for t in tasks if t.get("step_name") == "hydrate_ticket" and t.get("status") == "succeeded"]
    if hydrated:
        latest = max(hydrated, key=lambda t: str(t.get("updated_at") or t.get("created_at") or ""))
        output = latest.get("output") if isinstance(latest.get("output"), dict) else {}
        for k in _TICKET_KEYS:
            if not values.get(k) and output.get(k):
                values[k] = output[k]
    workflow_input = workflow.get("input") if isinstance(workflow.get("input"), dict) else {}
    if not values.get("jira_issue_key"):
        values["jira_issue_key"] = workflow_input.get("jira_issue_key") or workflow.get("external_ref")
    return values


def prepare_ticket_delivery(
    task: dict[str, Any],
    config: dict[str, Any],
    *,
    memory: Any,
    jira_factory: Callable[[], Any] | None = None,
    store_factory: Callable[[], Any] | None = None,
    inspect: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    values = _ticket_values(task, config, memory)
    body = str(values.get("work_order_body") or "")
    repo_url = str(values.get("repo_url") or "")
    base_branch = str(values.get("base_branch") or "")
    key = str(values.get("jira_issue_key") or "")
    tenant_id = str(task.get("tenant_id") or "default")
    warnings: list[str] = []

    refs: list[dict[str, Any]] = []
    try:
        refs = _program_design_refs(memory, str(task.get("id") or ""))
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"program design references unavailable: {exc}")
    attached: list[dict[str, Any]] = []
    if key:
        try:
            from ts_harness.intake.gather import _design_store
            from ts_harness.integrations.jira_client import JiraClient

            store = (store_factory or _design_store)()
            if store is not None:
                jira = (jira_factory or JiraClient.from_env)()
                attached = _attachment_refs(jira, key, store, tenant_id)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"ticket image attachments unavailable: {exc}")
    if attached:
        # Program references are already D1..Dn in order, so they keep their ids (the ticket
        # names them); attachments follow and are named in the body.
        program_keys = {str(ref.get("object_key")) for ref in refs}
        refs = number_refs([*refs, *attached])[:MAX_DESIGN_REFS]
        body = _with_design_line(body, [str(r["id"]) for r in refs if str(r.get("object_key")) not in program_keys])

    onboarding: dict[str, Any] = {"checked": False}
    if repo_url and base_branch:
        try:
            from ts_harness.workflows.repo_onboarding_gate import Target, inspect_repository

            found = (inspect or inspect_repository)(Target(repo_url, base_branch), ONBOARDING_ID)
            onboarding = {"checked": True, "needed": bool(found.needed), "reason": found.reason}
            if found.needed and found.body:
                body = (
                    body.rstrip()
                    + "\n\n## Repository setup (also in this PR)\n\n"
                    + "This repository's CI runs a suite the harness cannot run yet. Before the ticket's "
                    "change, do the following, so this PR also sets the repository up:\n\n"
                    + found.body.strip()
                    + "\n"
                )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"onboarding check failed: {exc}")
            onboarding = {"checked": False, "reason": str(exc)}

    return {
        "work_order_body": body,
        "design_refs": refs,
        "onboarding": onboarding,
        "warnings": warnings,
    }
