"""Live open-ended answer judge. The scorer is safe to unit-test; run_live calls a model."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ts_harness.agent.loop import EnterpriseAgent
from ts_harness.agent.provider import ModelProvider, build_provider
from ts_harness.config.schema import RuntimeConfig
from ts_harness.evals.harness import load_test_config
from ts_harness.evals.redteam import _ZeroTemperature
from ts_harness.repo_paths import repo_root
from ts_harness.tools.registry import build_default_registry


def dataset_path() -> Path:
    return repo_root() / "evals" / "agent_output" / "dataset.json"


def load_dataset(path: Path | None = None) -> list[dict[str, Any]]:
    return json.loads((path or dataset_path()).read_text())


def score_judgment(raw: str) -> list[str]:
    try:
        payload = _parse_judgment(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return ["judge response was not json"]
    if not isinstance(payload, dict) or payload.get("pass") is not True:
        reason = str(payload.get("reason") or "judge failed") if isinstance(payload, dict) else "judge failed"
        return [reason]
    return []


def _parse_judgment(raw: str) -> Any:
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return json.loads(text)


def _assistant_text(response: dict[str, Any]) -> str:
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
            else:
                parts.append(str(part))
        return "".join(parts)
    return str(content)


def _answer(config: RuntimeConfig, case: dict[str, Any], provider: ModelProvider) -> str:
    agent = EnterpriseAgent(config, build_default_registry(), provider=provider)
    result = agent.run_workflow_dispatch(
        tenant_id="default",
        agent_id=str(case["agent_id"]),
        payload={
            "task_id": str(case["id"]),
            "step_name": "qa_gate",
            "input": {str(case.get("source") or "text"): case.get("user_text") or ""},
        },
        source=str(case.get("source") or "eval"),
    )
    return str((result.output or {}).get("summary") or "")


def judge_answer(provider: ModelProvider, case: dict[str, Any], summary: str) -> list[str]:
    prompt = (
        'Score the assistant answer against the rubric. '
        'Reply with JSON only: {"pass": true, "reason": "short"} or {"pass": false, "reason": "short"}.\n'
        f"Rubric: {case.get('rubric') or ''}\n"
        f"User: {case.get('user_text') or ''}\n"
        f"Answer: {summary}"
    )
    response = provider.chat([{"role": "user", "content": prompt}], temperature=0.0)
    return score_judgment(_assistant_text(response))


def run_live(config: RuntimeConfig | None = None) -> bool:
    config = config or load_test_config()
    for case in load_dataset():
        agent_cfg = config.get_agent(str(case["agent_id"]))
        provider = _ZeroTemperature(build_provider(agent_cfg.model))
        summary = _answer(config, case, provider)
        if judge_answer(provider, case, summary):
            return False
    return True
