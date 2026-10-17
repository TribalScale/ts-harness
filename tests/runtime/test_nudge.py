from __future__ import annotations

from ts_harness.skills.nudge import (
    NUDGE_TEXT,
    apply_nudge_to_summary,
    parse_revise_intent,
    should_nudge,
)


LIVE = {"jira-to-pr", "team-runbook"}


def test_parse_revise_intent_finds_one_live_id():
    assert (
        parse_revise_intent("revise jira-to-pr with what we just did", LIVE)
        == "jira-to-pr"
    )
    assert (
        parse_revise_intent(
            "Please revise jira-to-pr with what we just did",
            LIVE,
        )
        == "jira-to-pr"
    )
    assert (
        parse_revise_intent("revise the deploy steps in jira-to-pr", LIVE)
        == "jira-to-pr"
    )
    assert (
        parse_revise_intent("revise our runbook for jira-to-pr", LIVE)
        == "jira-to-pr"
    )
    assert (
        parse_revise_intent(
            "revise jira-to-pr and also jira-to-pr again",
            LIVE,
        )
        == "jira-to-pr"
    )


def test_parse_revise_intent_ignores_leading_mentions():
    assert (
        parse_revise_intent(
            "<@U123> revise jira-to-pr with the new steps",
            LIVE,
        )
        == "jira-to-pr"
    )
    assert (
        parse_revise_intent("@claw revise jira-to-pr with the new steps", LIVE)
        == "jira-to-pr"
    )


def test_parse_revise_intent_accepts_polite_imperative_lead_ins():
    assert (
        parse_revise_intent("please revise jira-to-pr", LIVE)
        == "jira-to-pr"
    )
    assert (
        parse_revise_intent("could you revise jira-to-pr", LIVE)
        == "jira-to-pr"
    )
    assert (
        parse_revise_intent("would you revise jira-to-pr", LIVE)
        == "jira-to-pr"
    )
    assert (
        parse_revise_intent("can you revise jira-to-pr", LIVE)
        == "jira-to-pr"
    )
    assert (
        parse_revise_intent("let's revise jira-to-pr", LIVE)
        == "jira-to-pr"
    )
    assert parse_revise_intent("lets revise jira-to-pr", LIVE) == "jira-to-pr"
    assert (
        parse_revise_intent("let s revise jira-to-pr", LIVE)
        == "jira-to-pr"
    )
    assert parse_revise_intent("yes revise jira-to-pr", LIVE) == "jira-to-pr"
    assert (
        parse_revise_intent("hey claw revise jira-to-pr", LIVE)
        == "jira-to-pr"
    )
    assert parse_revise_intent("hi revise jira-to-pr", LIVE) == "jira-to-pr"
    assert (
        parse_revise_intent("okay claw revise jira-to-pr", LIVE)
        == "jira-to-pr"
    )
    assert (
        parse_revise_intent("just revise jira-to-pr", LIVE)
        == "jira-to-pr"
    )


def test_parse_revise_intent_ignores_learn():
    assert parse_revise_intent("/learn revise jira-to-pr notes", LIVE) is None


def test_parse_revise_intent_requires_revise_word():
    assert parse_revise_intent("update jira-to-pr please", LIVE) is None
    assert parse_revise_intent("I revised jira-to-pr manually", LIVE) is None


def test_parse_revise_intent_ignores_negated_and_incidental_revise():
    assert parse_revise_intent("do NOT revise jira-to-pr yet", LIVE) is None
    assert (
        parse_revise_intent(
            "we should revise jira-to-pr next quarter",
            LIVE,
        )
        is None
    )
    assert (
        parse_revise_intent(
            "we could revise jira-to-pr next quarter",
            LIVE,
        )
        is None
    )
    assert (
        parse_revise_intent(
            "can you check if the revise toggle for jira-to-pr works",
            LIVE,
        )
        is None
    )
    assert (
        parse_revise_intent(
            "is the revise button on jira-to-pr broken?",
            LIVE,
        )
        is None
    )
    assert (
        parse_revise_intent(
            "why did the revise of jira-to-pr fail last week",
            LIVE,
        )
        is None
    )
    assert (
        parse_revise_intent(
            "revise my last answer. by the way, does jira-to-pr still work?",
            LIVE,
        )
        is None
    )
    assert (
        parse_revise_intent(
            "please revise the summary, then tell me when jira-to-pr last ran",
            LIVE,
        )
        is None
    )
    assert (
        parse_revise_intent(
            "I asked you not to revise anything about jira-to-pr",
            LIVE,
        )
        is None
    )
    assert (
        parse_revise_intent(
            "can you check if the revise flag for jira-to-pr works",
            LIVE,
        )
        is None
    )


def test_parse_revise_intent_requires_exactly_one_live_id():
    assert (
        parse_revise_intent("revise jira-to-pr and team-runbook", LIVE)
        is None
    )
    assert parse_revise_intent("revise something-else", LIVE) is None


def test_should_nudge_requires_three_tools_and_success():
    tools = [
        {"tool": "read_file"},
        {"tool": "search_files"},
        {"tool": "read_file"},
    ]
    assert (
        should_nudge(
            status="succeeded", tool_calls=tools, already_sent=False
        )
        is True
    )
    assert (
        should_nudge(
            status="succeeded", tool_calls=tools[:2], already_sent=False
        )
        is False
    )
    assert (
        should_nudge(status="blocked", tool_calls=tools, already_sent=False)
        is False
    )
    assert (
        should_nudge(status="succeeded", tool_calls=tools, already_sent=True)
        is False
    )
    assert (
        should_nudge(
            status="succeeded",
            tool_calls=tools + [{"tool": "propose_skill"}],
            already_sent=False,
        )
        is False
    )


def test_should_nudge_counts_only_allowed_tool_calls():
    denied_tools = [
        {"tool": "read_file", "decision": "deny"},
        {"tool": "search_files", "decision": "deny"},
        {"tool": "read_file", "decision": "deny"},
    ]
    errored_tools = [
        {"tool": "read_file", "decision": "error"},
        {"tool": "search_files", "decision": "error"},
        {"tool": "read_file", "decision": "error"},
    ]
    mixed_tools = [
        {"tool": "read_file", "decision": "allow"},
        {"tool": "search_files", "decision": "deny"},
        {"tool": "read_file"},
        {"tool": "session_status", "decision": "allow"},
    ]

    assert (
        should_nudge(
            status="succeeded", tool_calls=denied_tools, already_sent=False
        )
        is False
    )
    assert (
        should_nudge(
            status="succeeded", tool_calls=errored_tools, already_sent=False
        )
        is False
    )
    assert (
        should_nudge(
            status="succeeded", tool_calls=mixed_tools, already_sent=False
        )
        is True
    )


def test_apply_nudge_to_summary_appends_once():
    text = apply_nudge_to_summary("Done.")
    assert text.endswith(NUDGE_TEXT)
    assert apply_nudge_to_summary(text) == text


def test_apply_nudge_to_summary_skips_blank_summary():
    assert apply_nudge_to_summary("") == ""
    assert apply_nudge_to_summary("   ") == ""
