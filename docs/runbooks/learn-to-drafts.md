# Learn to drafts

Use `/learn` to turn a URL, workspace path, notes, or just-finished task into a governed skill draft.

## Start a draft

In Slack or Admin Chat, send:

```text
/learn https://example.com/internal-runbook
```

In Admin Chat, operators can also open System > Skills and click **Learn a skill**. The button opens Chat with `/learn ` prefilled so the operator can add the source.

## What happens

`/learn` is authoring, not a normal slash-invoke skill pin. The turn researches the source with the existing agent tools, then calls `propose_skill` to write a draft under `skills/drafts/`.

When the active session agent cannot author skills, the gateway runs the authoring turn through `ops-agent` and posts the summary back on the original thread. The draft is still not live after that hop.

## Approval

Drafts stay non-injectable until an operator approves them from Inbox. Open the skill draft Inbox item, review the generated markdown and diagnostics, then approve or reject it.

## Fail-closed cases

- `/learn` with no source blocks with `skill_learn_missing_source`; no model call is made.
- If no authoring agent can call `propose_skill`, the turn blocks with `skill_learn_authoring_unavailable`.
- If the authoring turn does not call `propose_skill`, the turn blocks with `skill_learn_no_draft`.
- Denied, invalid markdown, and threat-scan failures block and return the existing `propose_skill` error text.
- `/learn` of a live skill id blocks with `skill_learn_use_revision`; use the revision flow in [`learning-loop.md`](learning-loop.md) instead.

## Expected reply

Successful authoring replies name the draft id and point to the Inbox draft. Do not tell operators the skill is live until Inbox approval promotes it.
