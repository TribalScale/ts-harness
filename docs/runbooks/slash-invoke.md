# Slash-invoke

Pin approved catalog skills onto one Slack, Discord, Telegram, or Admin Chat
turn.

## How to pin

Type leading `/skill-id` tokens (up to five), then the instruction:

```text
/jira-to-pr open a PR for the auth refactor
```

Admin Chat typeahead lists skills the current session agent can resolve
(orchestrator on a new thread). Pick a row to insert `/id` at the start.

No slash keeps today’s category dump.

## Fail-closed

If a token is a catalog id but does not resolve (draft, unassigned, missing
toolset, channel-chat exclusion), the turn is blocked. No model call. The
thread shows a banner naming the id and reason.

Unknown paths such as `/tmp/scan.pdf` are instruction, not errors.

## `/learn`

`/learn` is reserved and is not a skill pin. It now starts governed skill
authoring and routes the source text to the draft workflow instead of treating
the turn as a normal message. See [`learn-to-drafts.md`](learn-to-drafts.md).
