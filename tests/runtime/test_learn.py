from pathlib import Path

from ts_harness.config.schema import load_runtime_config
from ts_harness.skills.learn import (
    blocked_learn_payload,
    parse_learn,
    select_authoring_agent_id,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_plain_text_is_not_learn():
    parsed = parse_learn("check Artemis")
    assert parsed.is_learn is False
    assert parsed.source == ""


def test_learn_with_url():
    parsed = parse_learn("/learn https://example.com/api")
    assert parsed.is_learn is True
    assert parsed.source == "https://example.com/api"


def test_learn_ignores_leading_mentions():
    parsed = parse_learn("<@U123> /learn https://example.com/api")
    assert parsed.is_learn is True
    assert parsed.source == "https://example.com/api"

    parsed = parse_learn("@claw /learn https://example.com/api")
    assert parsed.is_learn is True
    assert parsed.source == "https://example.com/api"


def test_learn_case_insensitive_empty_source():
    parsed = parse_learn("/Learn")
    assert parsed.is_learn is True
    assert parsed.source == ""


def test_learning_is_not_learn():
    parsed = parse_learn("/learning notes")
    assert parsed.is_learn is False


def test_learn_after_other_token_is_not_learn():
    parsed = parse_learn("/jira-to-pr /learn notes")
    assert parsed.is_learn is False


def test_select_ops_when_session_is_orchestrator():
    config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
    agent_id = select_authoring_agent_id(
        config.get_agent("orchestrator"),
        config,
        registry_tool_names={"propose_skill"},
    )
    assert agent_id == "ops-agent"


def test_select_dev_when_session_is_dev():
    config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
    agent_id = select_authoring_agent_id(
        config.get_agent("dev-agent"),
        config,
        registry_tool_names={"propose_skill"},
    )
    assert agent_id == "dev-agent"


def test_select_none_when_tool_unregistered():
    config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
    assert (
        select_authoring_agent_id(
            config.get_agent("orchestrator"), config, registry_tool_names=set()
        )
        is None
    )


def test_blocked_payload_shape():
    payload = blocked_learn_payload(
        session_id="s1",
        error="skill_learn_missing_source",
        summary="Need a source after /learn (URL, path, or notes).",
    )
    assert payload["status"] == "blocked"
    assert payload["error"] == "skill_learn_missing_source"
    assert "source" in payload["output"]["summary"]
