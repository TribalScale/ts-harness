"""Triage at the revision limit: decide whether to keep going, accept, or ask a person.

A work order gets three automatic revisions. When they run out, a person used to decide every
time. Most of those decisions are mechanical: the PR is improving and the open findings are
specific and in scope (keep going), or only minor findings are left (accept and file them).
Here a strong model classifies the open findings and the trend; the policy below, not the
model, makes the decision:

* conservative projects always ask;
* the 8-revision ceiling always asks;
* a security-sensitive finding always asks;
* progress with specific, in-scope findings continues, on the stronger coding model;
* autonomous projects accept when only minor or out-of-scope findings remain, filing each as a
  follow-up ticket;
* anything else asks, with the triage attached.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from ts_harness.workflows import revision_guidance as rg

logger = logging.getLogger(__name__)

MAX_TOTAL_REVISIONS = 8
AUTONOMY_LEVELS = ("autonomous", "standard", "conservative")
DEFAULT_AUTONOMY = "autonomous"

# Security-sensitive work always goes to a person, whatever the model says.
_SECURITY = re.compile(
    r"\b(auth\w*|authori[sz]\w*|permission\w*|privilege\w*|secret\w*|password\w*|credential\w*|token\w*|jwt|jwks|"
    r"oauth|entra|saml|sso|encrypt\w*|decrypt\w*|injection|xss|csrf|ssrf|rbac|access control|session hijack|"
    r"pii|data exposure|leak\w*)\b",
    re.IGNORECASE,
)


def security_sensitive(finding: dict[str, Any]) -> bool:
    text = " ".join(str(finding.get(key) or "") for key in ("message", "description", "file"))
    return bool(_SECURITY.search(text))


def total_revisions(tasks: list[dict[str, Any]], implement_step: str) -> int:
    """Revisions of the implement step so far (every grant counted), excluding the first run."""
    runs = [
        task for task in tasks
        if task.get("step_name") == implement_step and task.get("status") != "cancelled"
        and not str((task.get("input") or {}).get("_revision_budget") or "")
    ]
    return max(0, len(runs) - 1)


PROMPT = """You triage an automated code review loop. A coding agent implemented a work order;
code review (or QA) has sent it back several times and the automatic revision budget is used up.
Decide how the remaining findings should be handled.

Work order:
{work_order}

Review findings, oldest round first (the last round is still open):
{history}

What each attempt reported:
{attempts}

Reply with JSON only:
{{"progress": true if earlier rounds' findings were fixed and the open ones are new, false if the attempts are not converging,
  "open_findings": [{{"file": "...", "line": 0, "summary": "one line",
                      "in_scope": true if the work order requires it,
                      "severity": "major" or "minor",
                      "security": true if it touches authentication, authorization, permissions, secrets, tokens or data exposure,
                      "clear": true if the fix is specific enough to implement without a decision}}],
  "guidance": "one consolidated instruction covering every open finding: what to change, where, and the test that proves each",
  "follow_ups": [{{"title": "...", "description": "..."}}] for findings that can wait (minor or out of scope),
  "reason": "one sentence"}}
"""


def classify(
    *,
    rounds: list[dict[str, Any]],
    attempts: list[str],
    work_order: str,
    model_call: Callable[[str], str],
) -> dict[str, Any] | None:
    prompt = PROMPT.format(
        work_order=(work_order or "(not available)")[:6000],
        history=rg.history_text(rounds),
        attempts="\n".join(f"- {text[:600]}" for text in attempts if text) or "(none recorded)",
    )
    try:
        reply = model_call(prompt)
    except Exception as exc:  # noqa: BLE001 - no triage means a person decides
        logger.warning("revision triage failed: %s", exc)
        return None
    match = re.search(r"\{.*\}", reply or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("open_findings"), list):
        return None
    return data


def decide(
    triage: dict[str, Any] | None,
    *,
    autonomy: str,
    total: int,
    open_findings: list[dict[str, Any]],
) -> dict[str, Any]:
    """The policy decision: ``{"decision": continue|accept|escalate, "reason": ...}``."""
    def escalate(reason: str) -> dict[str, Any]:
        return {"decision": "escalate", "reason": reason, "triage": triage}

    if autonomy == "conservative":
        return escalate("this project asks a person whenever revisions run out")
    if total >= MAX_TOTAL_REVISIONS:
        return escalate(f"the work order has had {total} revisions, the most the harness runs on its own")
    if triage is None:
        return escalate("the findings could not be triaged")
    classified = [item for item in triage.get("open_findings") or [] if isinstance(item, dict)]
    if any(item.get("security") for item in classified) or any(security_sensitive(item) for item in open_findings):
        return escalate("a finding touches security (authentication, permissions, secrets or data exposure)")
    blocking = [item for item in classified if item.get("in_scope") and item.get("severity") != "minor"]
    unclear = [item for item in blocking if not item.get("clear")]
    if not blocking and autonomy == "autonomous":
        return {"decision": "accept", "reason": "only minor or out-of-scope findings remain; they are filed as follow-up tickets",
                "triage": triage}
    if triage.get("progress") and blocking and not unclear and str(triage.get("guidance") or "").strip():
        return {"decision": "continue", "reason": "the PR is improving and the open findings are specific and in scope",
                "triage": triage}
    if unclear:
        return escalate("a finding needs a decision before it can be fixed")
    if not triage.get("progress"):
        return escalate("the revisions are not converging")
    return escalate(str(triage.get("reason") or "the triage did not find a safe way to continue"))


def guidance_text(triage: dict[str, Any], rounds: list[dict[str, Any]]) -> str:
    lines = [
        "The automatic revisions ran out while the PR was still improving. Fix every open finding in this revision:",
        str(triage.get("guidance") or "").strip(),
        "",
        "All review findings so far:",
        rg.history_text(rounds),
    ]
    return "\n".join(lines)


def project_autonomy(memory: Any, workflow: dict[str, Any]) -> str:
    """The work order's project's ``revision_autonomy`` (default autonomous)."""
    try:
        from ts_harness.workflows.merge_readiness import workflow_program

        program_id = workflow_program(memory, workflow)
        if not program_id:
            return DEFAULT_AUTONOMY
        program = (memory.get_program(program_id) or {}).get("program") or {}
        ref = str(program.get("project_id") or "")
        if not ref:
            return DEFAULT_AUTONOMY
        import uuid

        try:
            uuid.UUID(ref)
            project = memory.get_project(ref) or {}
        except ValueError:
            rows = (memory.list_projects(tenant_id=str(program.get("tenant_id") or "default"), key=ref) or {}).get("projects") or []
            project = next((row for row in rows if isinstance(row, dict) and row.get("key") == ref), {})
        value = str((project.get("project") if isinstance(project.get("project"), dict) else project).get("revision_autonomy") or "")
        return value if value in AUTONOMY_LEVELS else DEFAULT_AUTONOMY
    except Exception:  # noqa: BLE001 - the default applies when the project cannot be read
        return DEFAULT_AUTONOMY


def plan(
    tasks: list[dict[str, Any]],
    *,
    workflow: dict[str, Any],
    memory: Any,
    gate_step: str,
    implement_step: str,
    model_call: Callable[[str], str] | None = None,
    autonomy: str | None = None,
) -> dict[str, Any] | None:
    """The triage decision for a gate at its revision limit, or None when triage does not apply."""
    if gate_step not in rg.GUIDED_GATES:
        return None
    rounds = rg.review_rounds(tasks, gate_step)
    if not rounds:
        return None
    level = autonomy or project_autonomy(memory, workflow)
    total = total_revisions(tasks, implement_step)
    triage = None
    if level != "conservative" and total < MAX_TOTAL_REVISIONS:
        if model_call is None:
            model = rg.diagnosis_model()
            if model:
                model_call = rg.build_model_call(model)
        if model_call is not None:
            triage = classify(
                rounds=rounds,
                attempts=rg.attempt_summaries(tasks, implement_step),
                work_order=rg.work_order_text(tasks),
                model_call=model_call,
            )
    decision = decide(triage, autonomy=level, total=total, open_findings=rounds[-1]["findings"])
    decision.update({"autonomy": level, "total_revisions": total})
    if decision["decision"] == "continue":
        decision["feedback"] = guidance_text(triage, rounds)
        decision["coding_model"] = rg.guided_coding_model()
    return decision


def file_follow_ups(jira: Any, *, ticket_key: str, follow_ups: list[dict[str, Any]]) -> list[str]:
    """Create each follow-up in the ticket's project, under its epic, linked to the ticket."""
    created: list[str] = []
    if not ticket_key or "-" not in ticket_key:
        return created
    project = ticket_key.split("-", 1)[0]
    try:
        issue = jira.get_issue(ticket_key) or {}
        fields = issue.get("fields") or {}
        parent = str(((fields.get("parent") or {}).get("key")) or "")
        issue_type = str(((fields.get("issuetype") or {}).get("name")) or "Story")
    except Exception:  # noqa: BLE001 - file them without a parent
        parent, issue_type = "", "Story"
    for item in follow_ups[:10]:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        body = {
            "project": {"key": project},
            "summary": title[:250],
            "issuetype": {"name": issue_type},
            "description": {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [
                {"type": "text", "text": f"{str(item.get('description') or '').strip()}\n\nFollow-up from {ticket_key}, "
                 "accepted by the harness with this finding left open."}]}]},
        }
        if parent:
            body["parent"] = {"key": parent}
        try:
            key = str((jira.create_issue(body) or {}).get("key") or "")
        except Exception as exc:  # noqa: BLE001 - report what was filed
            logger.warning("follow-up for %s not filed: %s", ticket_key, exc)
            continue
        if key:
            created.append(key)
            try:
                jira.link_issues("Relates", inward_key=key, outward_key=ticket_key)
            except Exception:  # noqa: BLE001 - the ticket exists without the link
                pass
    return created
