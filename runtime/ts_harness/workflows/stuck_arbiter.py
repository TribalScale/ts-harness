"""Triage every step that stops for a person, and act on it when acting is safe.

The fixed rules run first: a coding run that stalled, hit its time limit, was cut off by the
provider, ran out of memory or was killed from outside is queued again by the runner, and the
review loop has its own guided revisions, triage and review/QA arbiter. What still reaches a
person lands here first. Once a minute (``arbitrate_stuck``) each step parked for a human
decision that the arbiter has not seen yet is read (its error, the coding job's result and file
changes, the review and QA findings, the work order, earlier decisions) and a strong model
classifies it. The policy below, not the model, decides what happens:

* ``retry`` (a cause outside the work) and ``revise`` with the arbiter's guidance (the agent went
  off track) run on their own, at most ``MAX_ACTIONS_PER_STEP`` times per step, through the same
  ``decide`` path as the Inbox, as ``harness-arbiter``;
* ``override`` accepts a code-review step over its blocking findings only when each one
  contradicts the work order or a person's request, or was already fixed in the diff, and never
  for a security finding; the PR still goes through QA and waits for a person to merge, and the
  reasoning is posted on the PR;
* everything else stays in the Inbox with a label (``Harness issue``, ``Needs product decision``,
  ``Arbiter handed off``), the diagnosis and a recommendation a person can apply in one click.

The arbiter never merges, stops a ticket or weakens a check. Conservative projects get labels and
recommendations only. ``TS_HARNESS_STUCK_ARBITER=off`` turns it off.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable

from ts_harness.workflows import revision_guidance as rg
from ts_harness.workflows import revision_triage as triage
from ts_harness.workflows.work_order_waves import HUMAN_DECISION_KEY

logger = logging.getLogger(__name__)

ARBITER_KEY = "_arbiter"
ACTOR = "harness-arbiter"
MAX_ACTIONS_PER_STEP = 2
MAX_PER_SWEEP = 3
MIN_CONFIDENCE = 0.6
ACTIONS = ("retry", "revise", "override", "escalate")
CATEGORIES = (
    "infrastructure",
    "harness_issue",
    "agent_off_track",
    "reviewer_wrong",
    "work_order_unclear",
    "product_decision",
)
LABELS = {
    "harness_issue": "Harness issue",
    "product_decision": "Needs product decision",
    "work_order_unclear": "Needs product decision",
    "handed_off": "Arbiter handed off",
}
OVERRIDE_GROUNDS = ("contradicts_work_order", "contradicts_human_request", "already_addressed")
REVIEW_GATES = frozenset({"finalize_code_review", "code_review"})

PROMPT = """You triage a step of an automated software delivery that stopped and is waiting for a person.
Decide what is wrong and what should happen next. The work order is the authority on what the
ticket must do; a person's change requests amend it.

Step: {step} (work order {work_order}); decisions available: {decisions}
Why it stopped:
{error}

Last attempt / feedback:
{feedback}

Coding job (latest):
{job}

Files the run changed:
{files}

Review and QA findings, oldest round first:
{rounds}

Work order:
{work_order_text}

Earlier decisions on this step:
{earlier}

Categories:
- infrastructure: a cause outside the work (a lost or killed job, a service that was down, a timeout); running it again fixes it
- harness_issue: a bug in the delivery harness or missing access/configuration (a repository it cannot read, a secret it lacks); running it again will not help and a harness operator must act
- agent_off_track: the coding agent did work the ticket does not ask for, weakened a safety check, rewrote tests to pass, or keeps failing the same finding; a revision with precise guidance fixes it
- reviewer_wrong: a blocking review finding contradicts the work order or a person's request, or the diff already fixes it
- work_order_unclear: the work order contradicts itself or leaves a decision open that changes the result
- product_decision: a choice only the product owner can make. Also when the agent found the work already done on the base branch (another ticket delivered it, nothing is left to change): ask whether to close the ticket as already delivered, naming the evidence

Reply with JSON only:
{{"category": one of the categories,
  "action": "retry" | "revise" | "override" | "escalate",
  "guidance": "for revise: the exact instruction for the coding agent, naming files, what to keep, what to undo, and the test that proves it; quote the work order lines it rests on",
  "override": [{{"finding": "the blocking finding", "grounds": "contradicts_work_order" | "contradicts_human_request" | "already_addressed", "evidence": "the work order line, the request, or the file and lines in the diff that show it"}}],
  "question": "for work_order_unclear or product_decision: one specific question for the ticket owner",
  "security": true if any blocking finding touches authentication, authorization, permissions, secrets, tokens or data exposure,
  "confidence": 0.0 to 1.0,
  "reason": "one or two sentences a person can read in the Inbox"}}
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def enabled() -> bool:
    return os.environ.get("TS_HARNESS_STUCK_ARBITER", "on").strip().lower() not in {"0", "off", "false", "no"}


def _input(task: dict[str, Any] | None) -> dict[str, Any]:
    raw = (task or {}).get("input")
    return dict(raw) if isinstance(raw, dict) else {}


def record(task: dict[str, Any] | None) -> dict[str, Any]:
    raw = _input(task).get(ARBITER_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def _since(task: dict[str, Any]) -> str:
    waiting = _input(task).get(HUMAN_DECISION_KEY)
    return str((waiting or {}).get("since") or task.get("updated_at") or "") if isinstance(waiting, dict) else ""


def seen(task: dict[str, Any]) -> bool:
    """Already triaged in this stop (a later stop of the same step is triaged again)."""
    rec = record(task)
    return bool(rec) and rec.get("for_since") == _since(task)


def actions_taken(tasks: list[dict[str, Any]], step_name: str) -> int:
    """Arbiter retries, revisions and overrides on this step, across its re-created tasks."""
    return sum(
        len([a for a in record(t).get("actions") or [] if isinstance(a, dict) and a.get("action") in ("retry", "revise", "override")])
        for t in tasks
        if str(t.get("step_name") or "") == step_name
    )


# --- evidence -----------------------------------------------------------------------------------


def _job_text(memory: Any, task_id: str) -> tuple[str, str]:
    """(the latest coding job's result, its file changes) as short text."""
    try:
        jobs = list(memory.list_coding_jobs(task_id=task_id, limit=5) or [])
    except Exception:  # noqa: BLE001
        return "(none)", "(none)"
    if not jobs:
        return "(none)", "(none)"
    job = jobs[0]
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    parts = [f"status: {job.get('status')}", f"error: {str(result.get('error') or job.get('error') or '')[:1500]}"]
    if result.get("summary"):
        parts.append(f"summary: {str(result['summary'])[:1500]}")
    if result.get("log_tail"):
        parts.append(f"log tail:\n{str(result['log_tail'])[-1500:]}")
    files = "(none)"
    try:
        events = list((memory.list_coding_job_events(str(job.get("id")), after=0, limit=300) or {}).get("events") or [])
        if events:
            files = "\n".join(
                f"- {e.get('kind')} {e.get('path')} (+{e.get('additions', 0)} -{e.get('deletions', 0)})"
                + (f" [{e.get('file_count')} files]" if e.get("kind") == "bulk" else "")
                + (" by the harness" if e.get("actor") == "harness" else "")
                for e in events[-60:]
            )
    except Exception:  # noqa: BLE001 - the feed is extra evidence
        pass
    return "\n".join(parts), files


def _gate_rounds(tasks: list[dict[str, Any]]) -> str:
    texts = []
    for gate in ("finalize_code_review", "qa_gate"):
        rounds = rg.review_rounds(tasks, gate)
        if rounds:
            texts.append(f"{gate}:\n{rg.history_text(rounds)[:5000]}")
    return "\n\n".join(texts) or "(none)"


def _earlier(tasks: list[dict[str, Any]], step_name: str) -> str:
    lines = []
    for task in tasks:
        if str(task.get("step_name") or "") != step_name:
            continue
        for action in record(task).get("actions") or []:
            if isinstance(action, dict):
                lines.append(f"- arbiter {action.get('action')}: {action.get('reason', '')}")
    return "\n".join(lines) or "(none)"


def blocking_findings(task: dict[str, Any]) -> list[dict[str, Any]]:
    output = task.get("output") if isinstance(task.get("output"), dict) else {}
    findings = output.get("findings") or (output.get("result") or {}).get("findings") or []
    from ts_harness.workflows.code_review import is_blocking

    return [f for f in findings if isinstance(f, dict) and is_blocking(f)]


def evidence(summary: dict[str, Any], task: dict[str, Any], tasks: list[dict[str, Any]], memory: Any) -> dict[str, str]:
    job, files = _job_text(memory, str(task.get("id") or ""))
    return {
        "step": summary.get("step_name") or "",
        "work_order": summary.get("work_order") or "",
        "decisions": ", ".join(summary.get("decisions") or []),
        "error": str(summary.get("error") or "")[:3000] or "(none)",
        "feedback": str(summary.get("feedback") or "")[:3000] or "(none)",
        "job": job,
        "files": files,
        "rounds": _gate_rounds(tasks),
        "work_order_text": rg.work_order_text(tasks)[:6000] or "(not available)",
        "earlier": _earlier(tasks, str(summary.get("step_name") or "")),
    }


def judge(facts: dict[str, str], model_call: Callable[[str], str]) -> dict[str, Any] | None:
    try:
        reply = model_call(PROMPT.format(**facts))
    except Exception as exc:  # noqa: BLE001 - no verdict: a person decides
        logger.warning("stuck arbitration failed: %s", exc)
        return None
    match = re.search(r"\{.*\}", reply or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# --- policy -------------------------------------------------------------------------------------


def decide(
    verdict: dict[str, Any] | None,
    *,
    summary: dict[str, Any],
    task: dict[str, Any],
    taken: int,
    autonomy: str,
) -> dict[str, Any]:
    """``{"do": "retry" | "revise" | "accept" | None, "label", "recommendation", "reason"}``."""
    decisions = list(summary.get("decisions") or [])

    def hand_off(why: str, label: str = "handed_off") -> dict[str, Any]:
        return {"do": None, "label": LABELS[label], "why": why, "recommendation": _recommendation(verdict, decisions)}

    if verdict is None:
        return hand_off("the arbiter could not read this step")
    category = str(verdict.get("category") or "")
    action = str(verdict.get("action") or "")
    reason = str(verdict.get("reason") or "").strip()
    if category == "harness_issue":
        return hand_off(reason or "a harness bug or missing access", "harness_issue")
    if category in {"product_decision", "work_order_unclear"}:
        return hand_off(str(verdict.get("question") or reason or "the work order leaves this open"), category)
    if autonomy == "conservative":
        return hand_off("this project asks a person before the harness acts")
    if taken >= MAX_ACTIONS_PER_STEP:
        return hand_off(f"the arbiter already acted {taken} times on this step")
    if str(summary.get("reason") or "") == "run_limit" and starts_coding_run(action, str(summary.get("step_name") or "")):
        # The ticket used all its coding runs: another one is a person's call. The arbiter still
        # diagnoses and recommends, and may re-run a step that doesn't code (QA, a check).
        return hand_off("the ticket has had the most coding runs it gets, so a person decides on another")
    try:
        confidence = float(verdict.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < MIN_CONFIDENCE:
        return hand_off(f"not confident enough to act ({confidence:.0%})")
    if action == "retry" and "retry" in decisions:
        return {"do": "retry", "guidance": "", "why": reason}
    if action == "revise" and "revise" in decisions:
        guidance = str(verdict.get("guidance") or "").strip()
        if not guidance:
            return hand_off("the arbiter gave no instruction to revise with")
        return {"do": "revise", "guidance": guidance, "why": reason}
    if action == "override":
        problem = override_problem(verdict, summary=summary, task=task)
        if problem:
            return hand_off(problem)
        return {"do": "accept", "guidance": override_note(verdict), "why": reason}
    return hand_off(reason or "no safe action")


CODING_STEPS = frozenset({"deliver_work_order", "implement", "implement_delivery"})


def starts_coding_run(action: str, step: str) -> bool:
    """A revise always starts a coding run; a retry does when the stuck step is the coding step."""
    return action == "revise" or (action == "retry" and step in CODING_STEPS)


def override_problem(verdict: dict[str, Any], *, summary: dict[str, Any], task: dict[str, Any]) -> str:
    """Why an override isn't allowed here, or ""."""
    if str(summary.get("step_name") or "") not in REVIEW_GATES or "accept" not in (summary.get("decisions") or []):
        return "only a code review's blocking findings can be overridden"
    blocking = blocking_findings(task)
    if not blocking:
        return "no blocking review finding to override"
    if verdict.get("security") or any(
        str(f.get("category") or "") == "security" or triage.security_sensitive(f) for f in blocking
    ):
        return "a blocking finding touches security, which the arbiter never overrides"
    grounds = [g for g in verdict.get("override") or [] if isinstance(g, dict)]
    if len(grounds) < len(blocking):
        return "the arbiter did not give grounds for every blocking finding"
    for item in grounds:
        if item.get("grounds") not in OVERRIDE_GROUNDS or not str(item.get("evidence") or "").strip():
            return "an override needs grounds (contradicts the work order or a request, or already fixed) and evidence"
    return ""


def override_note(verdict: dict[str, Any]) -> str:
    lines = ["The harness arbiter overrode the blocking review findings:"]
    for item in verdict.get("override") or []:
        if isinstance(item, dict):
            lines.append(f"- {str(item.get('finding') or '')[:300]} ({item.get('grounds')}): {str(item.get('evidence') or '')[:400]}")
    lines.append("QA still runs, and a person merges. Reverse it by requesting changes on the PR.")
    return "\n".join(lines)


def _recommendation(verdict: dict[str, Any] | None, decisions: list[str]) -> dict[str, Any] | None:
    """What a person can apply in one click: a decision the step allows, with the arbiter's text."""
    if not verdict:
        return None
    action = str(verdict.get("action") or "")
    if action == "override" and "accept" in decisions:
        return {"action": "accept", "guidance": override_note(verdict)}
    if action in {"retry", "revise"} and action in decisions:
        return {"action": action, "guidance": str(verdict.get("guidance") or "").strip()}
    return None


# --- acting -------------------------------------------------------------------------------------


def _save(client: Any, task: dict[str, Any], rec: dict[str, Any]) -> None:
    client.patch_task_input(str(task["id"]), {**_input(task), ARBITER_KEY: rec})


def _comment_on_pr(pr_url: str, text: str) -> None:
    from ts_harness.integrations.github_client import github_for, parse_github_pr_url

    parsed = parse_github_pr_url(pr_url)
    if parsed is None:
        return
    try:
        github_for(pr_url).comment(parsed[0], parsed[1], parsed[2], text)
    except Exception as exc:  # noqa: BLE001 - the override stands; the timeline has the reasoning
        logger.warning("override note not posted on %s: %s", pr_url, exc)


def arbitrate_one(
    engine: Any,
    task: dict[str, Any],
    *,
    memory: Any,
    model_call: Callable[[str], str],
    tenant_id: str = "default",
) -> dict[str, Any]:
    from ts_harness.workflows.human_decisions import stuck_summary

    workflow_id = str(task.get("workflow_id") or "")
    task_id = str(task.get("id") or "")
    summary = stuck_summary(engine, workflow_id, task_id)
    payload = engine.client.get_workflow(workflow_id)
    workflow = dict(payload.get("workflow") or {})
    tasks = list(payload.get("tasks") or [])
    stored = next((t for t in tasks if str(t.get("id")) == task_id), task)
    verdict = judge(evidence(summary, stored, tasks, memory), model_call)
    step = str(summary.get("step_name") or "")
    plan = decide(
        verdict,
        summary=summary,
        task=stored,
        taken=actions_taken(tasks, step),
        autonomy=triage.project_autonomy(memory, workflow),
    )
    previous = record(stored)
    rec = {
        "for_since": _since(stored),
        "at": _now(),
        "category": str((verdict or {}).get("category") or ""),
        "reason": str((verdict or {}).get("reason") or plan.get("why") or "")[:1000],
        "question": str((verdict or {}).get("question") or "")[:600],
        "confidence": (verdict or {}).get("confidence"),
        "actions": list(previous.get("actions") or []),
    }
    if plan["do"] is None:
        rec.update({"label": plan["label"], "why": plan["why"][:600], "recommendation": plan.get("recommendation")})
        _save(engine.client, stored, rec)
        logger.info("arbiter handed off workflow=%s step=%s label=%s", workflow_id, step, plan["label"])
        return {"task_id": task_id, "handed_off": plan["label"], "why": plan["why"]}
    done = {"action": "override" if plan["do"] == "accept" else plan["do"], "at": _now(), "reason": rec["reason"][:400]}
    rec["actions"].append(done)
    rec["acted"] = done["action"]
    _save(engine.client, stored, rec)
    guidance = plan.get("guidance") or ""
    if plan["do"] == "revise":
        guidance = f"{guidance}\n\n(From the harness arbiter: {rec['reason']})".strip()
    try:
        engine.decide_blocked_step(workflow_id, task_id, plan["do"], actor=ACTOR, guidance=guidance, tenant_id=tenant_id)
    except Exception as exc:  # noqa: BLE001 - the step stays in the Inbox, labelled
        rec.update({"label": LABELS["handed_off"], "why": f"acting failed: {exc}"[:600], "acted": None})
        rec["actions"] = rec["actions"][:-1]
        _save(engine.client, stored, rec)
        return {"task_id": task_id, "handed_off": LABELS["handed_off"], "why": str(exc)}
    if plan["do"] == "accept" and summary.get("pr_url"):
        _comment_on_pr(str(summary["pr_url"]), guidance)
    logger.info("arbiter acted workflow=%s step=%s action=%s", workflow_id, step, done["action"])
    return {"task_id": task_id, "acted": done["action"]}


def _waiting_for_answers(task: dict[str, Any]) -> bool:
    mark = _input(task).get(HUMAN_DECISION_KEY)
    return isinstance(mark, dict) and mark.get("reason") == "ticket_not_ready"


def arbitrate_stuck(
    *,
    tenant_id: str = "default",
    memory: Any = None,
    engine: Any = None,
    model_call: Callable[[str], str] | None = None,
    limit: int = MAX_PER_SWEEP,
) -> dict[str, Any]:
    """System job: triage the stuck steps the arbiter has not seen yet (a few per run)."""
    if not enabled():
        return {"status": "off"}
    if model_call is None:
        model = rg.diagnosis_model()
        if not model:
            return {"status": "skipped", "reason": "no diagnosis model configured"}
        model_call = rg.build_model_call(model)
    if memory is None:
        from ts_harness.memory.client import get_memory_client

        memory = get_memory_client()
    if engine is None:
        from ts_harness.workflows.engine import get_workflow_engine

        engine = get_workflow_engine()
    # A hand-written ticket waiting for answers is already a product question (jira/ticket_readiness.py).
    stuck = [t for t in list(memory.list_stuck_tasks(tenant_id=tenant_id) or []) if not seen(t) and not _waiting_for_answers(t)]
    results = []
    for task in stuck[:limit]:
        try:
            results.append(arbitrate_one(engine, task, memory=memory, model_call=model_call, tenant_id=tenant_id))
        except Exception as exc:  # noqa: BLE001 - one step must not stop the others
            logger.warning("arbitration of %s failed: %s", task.get("id"), exc)
            results.append({"task_id": str(task.get("id") or ""), "error": str(exc)[:200]})
    return {"status": "ok", "checked": len(results), "results": results, "waiting": max(0, len(stuck) - limit)}
