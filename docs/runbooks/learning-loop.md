# Learning loop

After hard channel or Admin Chat work, TS Harness nudges operators to capture reusable
playbooks as skills. Revisions go through governed drafts and Inbox approval —
never a direct live overwrite.

## Nudge

After a channel/Chat turn with ≥3 tool calls and no `propose_skill`, TS Harness appends:

```text
This looks reusable. `/learn` a new skill, or ask to revise an existing one.
```

The line is fixed copy (not a second model call). It appears at most **once per
session** on the user-visible summary and persisted assistant message. TS Harness does
not nudge on blocked turns, `/learn` returns, workflow dispatch, or cron.

Session metadata records `skill_nudge_sent: true`; audit logs `skill_nudge_sent`.

## New skills via `/learn`

Use `/learn` to create a **new** draft only. See [`learn-to-drafts.md`](learn-to-drafts.md).

`/learn` still cannot overwrite a live skill. If the source names an id that is
already approved in the catalog, the turn blocks with `skill_learn_use_revision`
and does not write.

## Revise a live skill

There is no `/revise` slash command in v1.

To revise an existing live skill, say an imperative `revise` clause with
exactly one live catalog id in that clause (for example,
`revise jira-to-pr with what we just did`).
Slack/Discord `@mention` prefixes are ignored. Polite imperatives such as
`could you revise <id>` work; modal statements such as `we could revise <id>`
do not.

The revise trigger does **not** fire when `revise` is incidental, negated, or
modal, such as `check if the revise toggle works`, `do not revise ...`, or
`we should revise ...`. Slash-pinned turns such as
`/jira-to-pr revise the deploy steps` skip the revise hop and run as normal
pinned skill invocations.

- On an **orchestrator** session, the gateway hops the turn to `ops-agent` (same
  pattern as `/learn` authoring). The authoring agent calls `propose_skill` with
  `revision_of` set to the live id. The live `SKILL.md` bytes stay unchanged
  until Inbox approval.
- On a session already bound to a skill-authoring agent (`ops-agent` /
  `dev-agent`), no hop — the session agent authors the revision draft directly.

Revision drafts must keep the same skill id and category as the live skill, and
must use a strictly greater dotted numeric version. Inbox approval replaces the
actual live source path, including directory-form skills such as
`skills/<category>/<id>/SKILL.md`.

The draft is non-injectable until approved. Inbox detail shows **Revision of
`id`** and a text diff against the live file.

### Inbox

- **Approve** a revision draft: community scan, atomic replace of the live path,
  draft deleted, catalog reload. Audit `skill_revised`.
- **Reject** a revision draft: draft deleted; live file unchanged. Audit
  `skill_rejected`.

New-skill approve still errors if the destination file already exists.

## Related

- Draft authoring: [`learn-to-drafts.md`](learn-to-drafts.md)
- Governed flow: [`skill-authoring.md`](skill-authoring.md)
