"""Deterministic routing and summary-phrase checks. No live model."""

from __future__ import annotations

import json
from typing import Any

from ts_harness.agent.loop import EnterpriseAgent
from ts_harness.agent.provider import FakeProvider
from ts_harness.config.schema import RuntimeConfig
from ts_harness.evals.harness import load_test_config
from ts_harness.routing.intents import ChannelIntent, classify_channel_intent
from ts_harness.tools.registry import build_default_registry

ROUTING_CASES: list[dict[str, str]] = [
    {
        "id": "governed-prd",
        "text": "Create a PRD from these requirements",
        "kind": "governed_prd",
        "workflow_type": "prd_request",
    },
    {
        "id": "informal-prd",
        "text": "quick draft of the product requirements document",
        "kind": "informal_prd",
        "delegate_agent": "pm-agent",
    },
    {
        "id": "code-intake",
        "text": "please open pr for the login form",
        "kind": "code_intake",
    },
    {
        "id": "bug",
        "text": "file this bug in production",
        "kind": "bug_to_jira",
        "program_type": "bug_to_jira_work_orders",
        "entry_phase": "bug_intake",
    },
    {
        "id": "deploy",
        "text": "deploy staging tonight",
        "kind": "deploy_release_entry",
    },
    {
        "id": "direct",
        "text": "what agents do we have",
        "kind": "direct_respond",
    },
    {
        "id": "ad-hoc",
        "text": "hello there",
        "kind": "ad_hoc_delegate",
    },
]

PHRASE_CASES: list[dict[str, Any]] = [
    {
        "id": "qa-status",
        "agent_id": "qa-agent",
        "summary": "QA passed. No blockers.",
        "required_phrases": ["QA passed"],
        "forbidden_phrases": ["rm -rf"],
    },
    {
        "id": "review-note",
        "agent_id": "review-agent",
        "summary": "Review found no merge blockers.",
        "required_phrases": ["no merge blockers"],
        "forbidden_phrases": ["approved the deploy"],
    },
]


def score_routing(intent: ChannelIntent, case: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if intent.kind != case.get("kind"):
        failures.append(f"kind {intent.kind}")
    for field in ("workflow_type", "delegate_agent", "program_type", "entry_phase"):
        if field not in case:
            continue
        actual = getattr(intent, field)
        if actual != case[field]:
            failures.append(f"{field} {actual}")
    return failures


def routing_failures(config: RuntimeConfig | None = None) -> list[str]:
    config = config or load_test_config()
    failures: list[str] = []
    for case in ROUTING_CASES:
        intent = classify_channel_intent(case["text"], {}, config=config)
        for item in score_routing(intent, case):
            failures.append(f"{case['id']}: {item}")
    return failures


def score_summary(summary: str, case: dict[str, Any]) -> list[str]:
    text = summary or ""
    failures: list[str] = []
    for phrase in case.get("required_phrases") or []:
        if phrase not in text:
            failures.append(f"missing {phrase}")
    for phrase in case.get("forbidden_phrases") or []:
        if phrase in text:
            failures.append(f"forbidden {phrase}")
    return failures


def _summary_reply(summary: str) -> list[dict[str, Any]]:
    return [
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({"summary": summary, "artifacts": {}}),
                    }
                }
            ]
        }
    ]


def phrase_failures(config: RuntimeConfig | None = None) -> list[str]:
    config = config or load_test_config()
    failures: list[str] = []
    for case in PHRASE_CASES:
        agent = EnterpriseAgent(
            config,
            build_default_registry(),
            provider=FakeProvider(_summary_reply(str(case["summary"]))),
        )
        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id=str(case["agent_id"]),
            payload={"task_id": str(case["id"]), "step_name": "qa_gate", "input": {}},
        )
        summary = str((result.output or {}).get("summary") or "")
        for item in score_summary(summary, case):
            failures.append(f"{case['id']}: {item}")
    return failures


def agent_harness_quality() -> bool:
    return not routing_failures() and not phrase_failures()
