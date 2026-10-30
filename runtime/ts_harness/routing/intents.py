"""Deterministic channel intent classification for orchestrator routing."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TYPE_CHECKING

# The request becomes the workflow's input; only confirmation messages shorten it.
REQUEST_TEXT_LIMIT = 8000

if TYPE_CHECKING:
    from ts_harness.config.schema import RuntimeConfig

IntentKind = str

_MUTATING_RE = re.compile(
    r"\b("
    r"write|exec|deploy|post|create|update|delete|merge|push|apply|rotate|notify|"
    r"fix|run|open|file|implement|refactor"
    r")\b",
    re.IGNORECASE,
)
_DIRECT_RESPOND_RE = re.compile(
    r"(?:"
    r"what agents do we have|"
    r"what did .+ do|"
    r"did my last request finish|"
    r"session status|"
    r"list (?:my )?sessions|"
    r"who are you|"
    r"what(?:'s| is) (?:the )?status"
    r")",
    re.IGNORECASE,
)


@dataclass
class ChannelIntent:
    kind: IntentKind
    workflow_type: str | None = None
    program_type: str | None = None
    entry_phase: str | None = None
    delegate_agent: str | None = None
    summary: str = ""
    rule_id: str | None = None


def classify_channel_intent(
    user_text: str,
    session_metadata: dict | None,
    *,
    config: RuntimeConfig | None = None,
) -> ChannelIntent:
    metadata = session_metadata or {}
    text = (user_text or "").strip()
    summary = text[:REQUEST_TEXT_LIMIT]

    bridge_intent = _match_bridge_intent(text, metadata)
    if bridge_intent is not None:
        return bridge_intent

    if metadata.get("pending_bridge") or metadata.get("pending_continue_delivery"):
        return ChannelIntent(kind="continue_bridge", summary=summary)

    if metadata.get("pending_workflow") or metadata.get("pending_delegate"):
        if config is not None and getattr(config, "routing", None) is not None:
            from ts_harness.routing.matcher import match_routing_rule

            matched = match_routing_rule(text, config)
            if matched is not None and matched.workflow_type:
                return matched
        return ChannelIntent(kind="workflow_gate", summary=summary)

    mode = str(metadata.get("mode") or "")
    linked_modes = {"workflow_linked", "bridge_linked", "prd_linked", "delivery_linked"}
    if metadata.get("workflow_id") and mode in linked_modes:
        return ChannelIntent(kind="workflow_gate", summary=summary)

    if config is not None and getattr(config, "routing", None) is not None:
        from ts_harness.routing.matcher import match_routing_rule

        matched = match_routing_rule(text, config)
        if matched is not None:
            return matched

    if _DIRECT_RESPOND_RE.search(text) and not _MUTATING_RE.search(text):
        return ChannelIntent(kind="direct_respond", summary=summary)

    return ChannelIntent(kind="ad_hoc_delegate", summary=summary)


def _match_bridge_intent(text: str, metadata: dict) -> ChannelIntent | None:
    from ts_harness.workflows.session_compat import read_pending_bridge, read_workflow_phase

    pending = read_pending_bridge(metadata)
    if pending and (_is_confirm(text) or _signals_match_text(text, _bridge_signals(metadata))):
        bridge_id = _bridge_intent_id(metadata)
        return ChannelIntent(kind=bridge_id or "continue_bridge", summary=text[:REQUEST_TEXT_LIMIT])

    phase = read_workflow_phase(metadata)
    workflow_type = str(metadata.get("workflow_type") or "")
    if phase == "approved" and workflow_type:
        signals = _bridge_signals(metadata)
        if signals and _signals_match_text(text, signals):
            bridge_id = _bridge_intent_id(metadata)
            return ChannelIntent(kind=bridge_id or "continue_bridge", summary=text[:REQUEST_TEXT_LIMIT])
    return None


def _bridge_signals(metadata: dict) -> list[str]:
    workflow_type = str(metadata.get("workflow_type") or "")
    if not workflow_type:
        return []
    try:
        from ts_harness.workflows.materialize import load_skeleton

        skeleton = load_skeleton(workflow_type)
        bridge = dict(getattr(skeleton, "bridge", None) or {})
        return [str(signal) for signal in bridge.get("signals") or []]
    except FileNotFoundError:
        return []


def _bridge_intent_id(metadata: dict) -> str | None:
    workflow_type = str(metadata.get("workflow_type") or "")
    if not workflow_type:
        return None
    try:
        from ts_harness.workflows.materialize import load_skeleton

        skeleton = load_skeleton(workflow_type)
        bridge = dict(getattr(skeleton, "bridge", None) or {})
        value = bridge.get("bridge_intent_id")
        return str(value) if value else None
    except FileNotFoundError:
        return None


def _signals_match_text(text: str, signals: list[str]) -> bool:
    lowered = text.lower()
    for signal in signals:
        if signal.lower() in lowered:
            return True
        try:
            if re.search(signal, lowered, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def _is_confirm(text: str) -> bool:
    normalized = text.strip().lower()
    return normalized in {
        "yes",
        "y",
        "yeah",
        "yep",
        "confirm",
        "confirmed",
        "proceed",
        "go ahead",
        "do it",
        "ok",
        "okay",
        "sure",
        "please do",
    }
