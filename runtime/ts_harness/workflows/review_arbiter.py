"""Settle a disagreement between code review and QA instead of waiting for a person.

When the revisions alternate between the two gates (``revise_on_reject.conflicting_gates``),
the harness used to stop and ask a person every time. Most of those stops are not real
disputes: the gates found different, independent problems, or one finding contradicts the
work order. Here a strong model reads the work order and every round of both gates' feedback
and rules on it; the policy below, not the model, decides whether the ruling is acted on:

* conservative projects always ask;
* the 8-revision ceiling always asks;
* a security-sensitive finding always asks;
* a dispute the work order does not settle (a product decision) asks;
* otherwise the next revision runs with one consolidated instruction that states the ruling.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from ts_harness.workflows import revision_guidance as rg
from ts_harness.workflows import revision_triage as triage

logger = logging.getLogger(__name__)

PROMPT = """You arbitrate between two automated reviewers of one pull request: code review and QA.
They keep sending the work back in turn, and the harness must decide what the next revision does
without asking a person, unless a person is really needed.

The work order is the authority: its acceptance criteria and stated scope decide. Where it is
silent, correctness and data integrity beat style, and a finding that asks to undo something
the work order requires loses.

Work order:
{work_order}

Each reviewer's feedback, oldest first:
{rounds}

Reply with JSON only:
{{"contradiction": true if a finding from one reviewer asks to undo or prevent what the other requires, false if they are independent problems,
  "disputes": [{{"code_review": "the code review point", "qa": "the QA point", "ruling": "which one stands and why, citing the work order"}}],
  "needs_product_decision": true only if the work order does not settle a dispute and either answer changes what the user gets,
  "security": true if any finding touches authentication, authorization, permissions, secrets, tokens or data exposure,
  "guidance": "one consolidated instruction for the coding agent: every finding that stands, what to change and where, the test that proves each, and the points it must not change because the ruling went the other way",
  "reason": "one sentence"}}
"""


def _rounds_text(run: list[dict[str, Any]]) -> str:
    lines = []
    for index, item in enumerate(run, 1):
        who = "QA" if "qa" in str(item.get("step") or "") else "Code review"
        lines.append(f"{index}. {who}:\n{str(item.get('feedback') or '').strip()[:4000]}")
    return "\n\n".join(lines) or "(none recorded)"


def arbitrate(*, run: list[dict[str, Any]], work_order: str, model_call: Callable[[str], str]) -> dict[str, Any] | None:
    """The model's ruling, or None when it gave none."""
    prompt = PROMPT.format(work_order=(work_order or "(not available)")[:6000], rounds=_rounds_text(run))
    try:
        reply = model_call(prompt)
    except Exception as exc:  # noqa: BLE001 - no ruling means a person decides
        logger.warning("review arbitration failed: %s", exc)
        return None
    match = re.search(r"\{.*\}", reply or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def decide(ruling: dict[str, Any] | None, *, autonomy: str, total: int, run: list[dict[str, Any]]) -> dict[str, Any]:
    """The policy decision: ``{"decision": "continue" | "escalate", "reason": ...}``."""

    def escalate(reason: str) -> dict[str, Any]:
        return {"decision": "escalate", "reason": reason, "ruling": ruling}

    if autonomy == "conservative":
        return escalate("this project asks a person when code review and QA disagree")
    if total >= triage.MAX_TOTAL_REVISIONS:
        return escalate(f"the work order has had {total} revisions, the most the harness runs on its own")
    if ruling is None:
        return escalate("the disagreement could not be arbitrated")
    feedback_text = " ".join(str(item.get("feedback") or "") for item in run)
    if ruling.get("security") or triage.security_sensitive({"message": feedback_text}):
        return escalate("a finding touches security (authentication, permissions, secrets or data exposure)")
    if ruling.get("needs_product_decision"):
        return escalate(str(ruling.get("reason") or "the work order does not settle the disagreement"))
    if not str(ruling.get("guidance") or "").strip():
        return escalate("the arbitration gave no instruction to act on")
    return {"decision": "continue", "reason": str(ruling.get("reason") or "the work order settles it"), "ruling": ruling}


def feedback_text(ruling: dict[str, Any]) -> str:
    lines = ["Code review and QA sent this back in turn. An arbiter read both against the work order:"]
    if ruling.get("contradiction"):
        for item in ruling.get("disputes") or []:
            if isinstance(item, dict) and item.get("ruling"):
                lines.append(f"- Ruling: {item['ruling']}")
    else:
        lines.append("- They found different problems, not opposite ones: fix all of them.")
    lines += ["", "Do this in this revision:", str(ruling.get("guidance") or "").strip()]
    return "\n".join(lines)


def plan(
    run: list[dict[str, Any]],
    *,
    tasks: list[dict[str, Any]],
    workflow: dict[str, Any],
    memory: Any,
    implement_step: str,
    model_call: Callable[[str], str] | None = None,
    autonomy: str | None = None,
) -> dict[str, Any]:
    """The decision for a review/QA disagreement, with the revision feedback when it continues."""
    level = autonomy or triage.project_autonomy(memory, workflow)
    total = triage.total_revisions(tasks, implement_step)
    ruling = None
    if level != "conservative" and total < triage.MAX_TOTAL_REVISIONS:
        if model_call is None:
            model = rg.diagnosis_model()
            if model:
                model_call = rg.build_model_call(model)
        if model_call is not None:
            ruling = arbitrate(run=run, work_order=rg.work_order_text(tasks), model_call=model_call)
    decision = decide(ruling, autonomy=level, total=total, run=run)
    decision.update({"autonomy": level, "total_revisions": total})
    if decision["decision"] == "continue":
        decision["feedback"] = feedback_text(ruling or {})
        decision["coding_model"] = rg.guided_coding_model()
    return decision
