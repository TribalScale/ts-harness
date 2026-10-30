"""Reusable confirmation classification for yes/no-style channel decisions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from ts_harness.agent.provider import ModelProvider

ConfirmationDecision = Literal["approve", "reject", "ambiguous", "none"]
ConfirmationSource = Literal["deterministic", "llm", "none"]
ConfirmationContext = Literal[
    "workflow_start",
    "bridge_start",
    "approval_gate",
    "delegate_start",
    "tool_approval",
]


@dataclass(frozen=True)
class ConfirmationClassification:
    decision: ConfirmationDecision
    feedback: str = ""
    source: ConfirmationSource = "none"


_APPROVE_RE = re.compile(
    r"^(?:"
    r"yes|y|yeah|yep|approve|approved|confirm|confirmed|proceed|ok|okay|sure|"
    r"looks good|lgtm|ship it|go ahead|good to go|this works|"
    r"please proceed|let'?s do it|let'?s go|do it"
    r")\s*\.?$",
    re.IGNORECASE,
)
_REJECT_PREFIX_RE = re.compile(
    r"^(?:no|n|nope|reject|rejected|cancel|cancelled|needs? changes?|revise|not yet)"
    r"(?:[\s:,-]+(?P<feedback>.+))?\s*\.?$",
    re.IGNORECASE,
)
_REJECT_ACTION_RE = re.compile(
    r"^(?P<feedback>(?:change|add|remove|update|fix|include|exclude)\b.+)\s*\.?$",
    re.IGNORECASE,
)
_AMBIGUOUS_RE = re.compile(
    r"^(?:maybe|perhaps|hmm+|i guess|not sure|unsure|kind of|sort of)\s*\.?$",
    re.IGNORECASE,
)
_READY_TO_DRAFT_RE = re.compile(
    r"(?:\bready\b|draft the bug|draft the plan|\bdraft it\b)",
    re.IGNORECASE,
)
_NOT_READY_RE = re.compile(r"\bnot\s+ready\b", re.IGNORECASE)
_REVISION_FEEDBACK_RE = re.compile(
    r"(?:"
    r"more verbose|make (?:it |the draft )?(?:more )?verbose|"
    r"expand(?:\s+the)?(?:\s+draft)?|add more detail|make it longer|"
    r"can you revise|please revise|change the draft|update the draft|"
    r"revise (?:the|this) draft|needs? (?:more|to be) (?:detail|verbose|longer)|"
    r"make (?:it|the prd) (?:longer|more detailed)"
    r")",
    re.IGNORECASE,
)

_CONTEXT_PROMPTS: dict[ConfirmationContext, str] = {
    "workflow_start": "The user is replying to a pending workflow start confirmation.",
    "bridge_start": "The user is replying to a pending bridge/continue-delivery confirmation.",
    "approval_gate": "The user is replying to a pending PRD/workflow approval gate.",
    "delegate_start": "The user is replying to a pending delegation confirmation.",
    "tool_approval": "The user is replying to a pending tool action that requires approval (e.g. send email).",
}


def confirmation_clarification_message(context: ConfirmationContext) -> str:
    if context == "approval_gate":
        return (
            "Do you want to approve this PRD, or request revisions? "
            "Reply approve, or reply no with the changes you want."
        )
    if context == "bridge_start":
        return (
            "Do you want to continue with the downstream workflow? "
            "Reply yes to proceed, or no to cancel."
        )
    if context == "delegate_start":
        return (
            "Do you want me to delegate this task? "
            "Reply yes to proceed, or no to cancel."
        )
    if context == "tool_approval":
        return (
            "Do you want me to run the pending action (for example send the email)? "
            "Reply yes to proceed, or no to cancel."
        )
    return "Do you want to start this workflow? Reply yes to start, or no to cancel."


def looks_like_ready_to_draft(user_text: str) -> bool:
    """True when an intake reply asks to draft, even if it also contains answers.

    A leading no/reject, or "not ready", stays a rejection. The deterministic
    result wins over the LLM fallback, which was marking long answers that
    ended in "ready, draft the bug" as revision requests.
    """
    text = (user_text or "").strip()
    if not text or _NOT_READY_RE.search(text):
        return False
    if _REJECT_PREFIX_RE.match(text) or _REJECT_ACTION_RE.match(text):
        return False
    return bool(_READY_TO_DRAFT_RE.search(text))


def looks_like_revision_feedback(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    return bool(_REVISION_FEEDBACK_RE.search(text))


def classify_confirmation_deterministic(
    user_text: str,
    *,
    context: ConfirmationContext,
) -> ConfirmationClassification:
    text = (user_text or "").strip()
    if not text:
        return ConfirmationClassification(decision="none")

    if _AMBIGUOUS_RE.match(text):
        return ConfirmationClassification(decision="ambiguous", source="deterministic")

    if _APPROVE_RE.match(text):
        return ConfirmationClassification(decision="approve", source="deterministic")

    rejected = _REJECT_PREFIX_RE.match(text)
    if rejected:
        feedback = (rejected.group("feedback") or "").strip()
        return ConfirmationClassification(
            decision="reject",
            feedback=feedback or "revision requested",
            source="deterministic",
        )

    action_reject = _REJECT_ACTION_RE.match(text)
    if action_reject:
        feedback = (action_reject.group("feedback") or text).strip()
        return ConfirmationClassification(
            decision="reject",
            feedback=feedback or text,
            source="deterministic",
        )

    if context == "approval_gate" and _REVISION_FEEDBACK_RE.search(text):
        return ConfirmationClassification(
            decision="reject",
            feedback=text,
            source="deterministic",
        )

    return ConfirmationClassification(decision="none")


def _parse_llm_classification(raw: str) -> ConfirmationClassification | None:
    text = (raw or "").strip()
    if not text:
        return None
    payload: dict[str, object] | None = None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                payload = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return None
    if not isinstance(payload, dict):
        return None
    decision = str(payload.get("decision") or "").strip().lower()
    if decision not in {"approve", "reject", "ambiguous"}:
        return None
    feedback = str(payload.get("feedback") or "").strip()
    return ConfirmationClassification(
        decision=decision,  # type: ignore[arg-type]
        feedback=feedback,
        source="llm",
    )


def classify_confirmation_with_llm(
    user_text: str,
    *,
    context: ConfirmationContext,
    provider: ModelProvider,
) -> ConfirmationClassification:
    text = (user_text or "").strip()
    if not text:
        return ConfirmationClassification(decision="none")

    system_prompt = (
        "You classify a Slack reply for a workflow confirmation gate.\n"
        f"{_CONTEXT_PROMPTS[context]}\n"
        'Return only JSON: {"decision":"approve|reject|ambiguous","feedback":"..."}\n'
        "Do not perform the workflow action.\n"
        "Do not answer the user.\n"
        "If unsure, use ambiguous.\n"
        "For reject decisions, put revision notes in feedback."
    )
    response = provider.chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        tools=None,
        temperature=0.0,
    )
    message = ((response.get("choices") or [{}])[0].get("message") or {})
    content = str(message.get("content") or "")
    parsed = _parse_llm_classification(content)
    if parsed is None:
        return ConfirmationClassification(decision="none")
    return parsed


def classify_confirmation(
    user_text: str,
    *,
    context: ConfirmationContext,
    provider: ModelProvider | None = None,
    use_llm_fallback: bool = True,
) -> ConfirmationClassification:
    deterministic = classify_confirmation_deterministic(user_text, context=context)
    if deterministic.decision != "none":
        return deterministic
    if use_llm_fallback and provider is not None:
        llm_result = classify_confirmation_with_llm(
            user_text,
            context=context,
            provider=provider,
        )
        if llm_result.decision != "none":
            return llm_result
    return ConfirmationClassification(decision="none")
