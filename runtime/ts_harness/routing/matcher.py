"""Config-driven routing rule matcher."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ts_harness.routing.intents import REQUEST_TEXT_LIMIT, ChannelIntent

if TYPE_CHECKING:
    from ts_harness.config.schema import RuntimeConfig, RoutingIntentRule


def find_routing_rule(config: RuntimeConfig, intent_kind: str) -> RoutingIntentRule | None:
    routing = getattr(config, "routing", None)
    if routing is None:
        return None
    for rule in routing.intents:
        if rule.id == intent_kind:
            return rule
    return None


def match_routing_rule(text: str, config: RuntimeConfig) -> ChannelIntent | None:
    routing = getattr(config, "routing", None)
    if routing is None:
        return None

    normalized = (text or "").strip()
    lowered = normalized.lower()
    summary = normalized[:REQUEST_TEXT_LIMIT]

    for rule in routing.intents:
        if not _signals_match(lowered, rule):
            continue
        if _exclude_match(lowered, rule):
            continue
        return _with_named_ticket(rule, _intent_from_rule(rule, summary=summary), normalized)

    default_kind = routing.default or "ad_hoc_delegate"
    if default_kind == "ad_hoc_delegate":
        return None
    return ChannelIntent(kind=default_kind, summary=summary)


def _signals_match(text: str, rule: RoutingIntentRule) -> bool:
    for signal in rule.signals:
        if signal.lower() in text:
            return True
        try:
            if re.search(signal, text, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def _exclude_match(text: str, rule: RoutingIntentRule) -> bool:
    for phrase in rule.exclude:
        if phrase.lower() in text:
            return True
    return False


def _with_named_ticket(rule: RoutingIntentRule, intent: ChannelIntent, text: str) -> ChannelIntent:
    if rule.program_type != "jira_to_pr" and rule.id != "jira_to_pr":
        return intent
    from ts_harness.jira.ticket_queue import named_ticket_key

    key = named_ticket_key(text)
    if not key:
        return intent
    intent.entry_phase = "implement"
    intent.artifacts = {"jira_issue_key": key}
    return intent


def _intent_from_rule(rule: RoutingIntentRule, *, summary: str) -> ChannelIntent:
    return ChannelIntent(
        kind=rule.id,
        workflow_type=rule.workflow_type,
        program_type=rule.program_type,
        entry_phase=rule.entry_phase,
        delegate_agent=rule.delegate_agent,
        summary=summary,
        rule_id=rule.id,
    )
