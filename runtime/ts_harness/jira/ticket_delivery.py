"""Read a Jira ticket and get it ready to code (the first step of ``implement_jira_ticket``).

``prepare_ticket_delivery`` is a system step (no agent) and the workflow's first step. The
person confirmed when they started the program; this step posts what it resolved and the
coding job starts:

- **The ticket**: summary and description from Jira become the work order body.
- **Repository and branch**: the description's ``Repo: org/repo@branch`` line, else the
  runtime's ``implement_jira_ticket`` repository; a branch named in the request
  ("implement THD-33 on branch release/2") overrides the branch.
- **Design references**: those of the program the ticket was filed from (``Program:`` line),
  else of the program running it, plus the ticket's image attachments, named on its Design
  line. The coding job and QA see the references the body names.
- **Onboarding**: a repository whose CI runs an e2e or integration suite but has no
  ``.ts-harness.yml`` gets the onboarding instructions added, so the ticket's PR sets it up.

A ticket that can't be read, or names no repository when none is configured, blocks the step
with what to fix. Every other lookup is best effort and listed in ``warnings``.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from ts_harness.intake.design import MAX_DESIGN_REFS, number_refs, store_design_image

logger = logging.getLogger(__name__)

MAX_ATTACHMENT_BYTES = 8_000_000
ONBOARDING_ID = "WO-0000"
_NAMED_BRANCH = re.compile(r"\bbranch\s+([A-Za-z0-9][A-Za-z0-9._/-]*)", re.IGNORECASE)


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


def _workflow_input(memory: Any, task: dict[str, Any]) -> dict[str, Any]:
    from ts_harness.workflows.hooks._helpers import load_workflow_payload

    workflow, _tasks = load_workflow_payload(memory, str(task.get("workflow_id") or ""))
    found = workflow.get("input") if isinstance(workflow.get("input"), dict) else {}
    return {**found, "_external_ref": workflow.get("external_ref")}


def _repo_url(repo_id: str) -> str:
    repo_id = repo_id.strip()
    if "://" in repo_id:
        return repo_id.rstrip("/")
    return f"https://github.com/{repo_id.strip('/')}"


def _configured_repo() -> tuple[str, str]:
    try:
        from ts_harness.implementation.config import repo_for_workflow

        repo = repo_for_workflow("implement_jira_ticket")
        return str(repo.url or ""), str(repo.base_branch or "")
    except Exception:  # noqa: BLE001
        return "", ""


def resolve_target(pointer: Any, requested_branch: str = "", configured: tuple[str, str] = ("", "")) -> tuple[str, str, str]:
    """``(repo_url, base_branch, where it came from)`` for a ticket.

    The repository is the ticket's ``Repo:`` line, else the runtime's ``implement_jira_ticket``
    repository. The branch is the ``Repo:`` line's ``@branch``, else the configured branch for
    that repository, else the program's default, else ``dev``. A branch named in the request
    overrides it (``prepare_ticket_delivery``).
    """
    configured_url, configured_branch = configured
    if pointer.repo_id:
        url = _repo_url(pointer.repo_id)
        same = configured_url and configured_url.rstrip("/").lower() == url.lower()
        branch = pointer.base_branch or (configured_branch if same else "") or requested_branch or "dev"
        return url, branch, "the ticket's Repo line"
    if configured_url:
        return configured_url, configured_branch or requested_branch or "dev", "the runtime config"
    return "", "", ""


def _program_artifacts(memory: Any, program_id: str) -> dict[str, Any]:
    status = memory.get_program_status(program_id) or {}
    return status.get("artifacts") if isinstance(status.get("artifacts"), dict) else {}


def prepare_ticket_delivery(
    task: dict[str, Any],
    config: dict[str, Any],
    *,
    memory: Any,
    jira_factory: Callable[[], Any] | None = None,
    store_factory: Callable[[], Any] | None = None,
    inspect: Callable[..., Any] | None = None,
    configured: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Read the ticket from Jira and resolve everything the coding job needs.

    Raises ``ValueError`` (the step blocks with the message) when the ticket can't be read
    or names no repository and none is configured.
    """
    from ts_harness.integrations.adf import adf_to_text
    from ts_harness.jira.pointer_block import parse_pointer_block

    workflow_input = _workflow_input(memory, task)
    task_input = task.get("input") if isinstance(task.get("input"), dict) else {}
    key = str(task_input.get("jira_issue_key") or config.get("jira_issue_key") or workflow_input.get("jira_issue_key") or workflow_input.get("_external_ref") or "").strip()
    if not key:
        raise ValueError("no Jira issue key on this workflow")
    tenant_id = str(task.get("tenant_id") or "default")
    warnings: list[str] = []

    from ts_harness.integrations.jira_client import JiraClient

    try:
        jira = (jira_factory or JiraClient.from_env)()
        issue = jira.get_issue(key)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"could not read {key} from Jira: {exc}") from exc
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    summary = str(fields.get("summary") or "").strip()
    raw = fields.get("description")
    description = (adf_to_text(raw) if isinstance(raw, dict) else str(raw or "")).strip()
    if not description:
        warnings.append(f"{key} has no description; the coding job works from its summary alone")
    pointer = parse_pointer_block(description)
    requested = str(task_input.get("base_branch") or workflow_input.get("base_branch") or "")
    repo_url, base_branch, repo_source = resolve_target(pointer, requested, configured if configured is not None else _configured_repo())
    # A branch named in the request ("implement THD-33 on branch release/2") wins: there is no
    # kickoff gate to name it at, the request is the confirmation.
    named = _NAMED_BRANCH.search(str(task_input.get("summary") or workflow_input.get("summary") or ""))
    if named and repo_url:
        base_branch, repo_source = named.group(1), f"{repo_source}; branch from your request"
    if not repo_url:
        raise ValueError(
            f"{key} names no repository and none is configured: add a line `Repo: org/repo@branch` "
            "to its description (or set implementation.repos.implement_jira_ticket), then run it again"
        )
    body = f"# {key} {summary}\n\n{description}\n" if description else f"# {key} {summary}\n"

    # The program the ticket was filed from (Program: line), else the program running it.
    owner = str(pointer.program_id or "")
    planning: dict[str, Any] = {}
    refs: list[dict[str, Any]] = []
    try:
        if owner:
            artifacts = _program_artifacts(memory, owner)
            refs = [r for r in artifacts.get("design_refs") or [] if isinstance(r, dict) and r.get("id")]
            planning = {k: artifacts[k] for k in ("refinery_artifacts", "foundry_artifacts", "planner_artifacts") if artifacts.get(k)}
        if not refs:
            refs = _program_design_refs(memory, str(task.get("id") or ""))
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"program design references unavailable: {exc}")
    attached: list[dict[str, Any]] = []
    try:
        from ts_harness.intake.gather import _design_store

        store = (store_factory or _design_store)()
        if store is not None:
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

    repo_name = repo_url.split("github.com/")[-1]
    lines = [f"{key}: {summary}", f"PR against `{base_branch}` on `{repo_name}` (from {repo_source})."]
    named = [str(r["id"]) for r in refs if str(r["id"]) in body]
    if refs:
        lines.append("Design: " + ", ".join(f"{r['id']} ({r.get('title') or r.get('source')})" for r in refs if str(r["id"]) in named or not named))
    if onboarding.get("needed"):
        lines.append("The repository has no .ts-harness.yml yet; this PR also adds it.")
    lines += [f"Note: {w}" for w in warnings]
    lines.append("Starting the coding job now.")

    return {
        "summary": "\n".join(lines),
        "work_order_id": key,
        "work_order_body": body,
        "jira_issue_key": key,
        "repo_url": repo_url,
        "base_branch": base_branch,
        "owner_program_id": owner or None,
        "design_refs": refs,
        "onboarding": onboarding,
        "warnings": warnings,
        **planning,
    }
