# Governed skill authoring

Agents may propose new skills as **drafts**. Drafts are never injectable until an
admin approves them via the gateway admin API.

## Flow

0. Start with `/learn <source>` in Chat or Slack, or click **Learn a skill** from
   the Skills catalog to prefill Chat with `/learn `.
1. Agent calls `propose_skill` (toolset `skill-authoring` on `ops-agent` / `dev-agent`).
2. Draft is written under `$SKILLS_DIR/drafts/` with
   `trust_level: agent-created` and `approval_status: draft`.
3. `SkillReviewWorker` records async diagnostics.
4. Admin lists / inspects drafts, then approve or reject.
5. Approve re-scans content, promotes to `skills/<category>/<id>.md` with
   `trust_level: trusted` + `approval_status: approved`, and reloads the catalog.

## Revise a live skill

Do not use `/learn` on a live id — it blocks with `skill_learn_use_revision`.

1. In Chat or Slack, ask to revise a live skill: `revise <live-id> ...` (the
   message must contain `revise` and exactly one live catalog id). On
   orchestrator sessions the gateway hops to `ops-agent`; authoring agents call
   `propose_skill` with `revision_of` set to that id.
2. The draft lands under `skills/drafts/<id>.md` with
   `provenance.revision_of`. The live file is unchanged until Inbox approval.
3. In Inbox, review the **Revision of** badge and text diff, then approve or
   reject. Approve atomically replaces the live skill; reject deletes the draft
   and leaves the live file as-is.

See [`learning-loop.md`](learning-loop.md) for nudge behavior and full details.

## Writability

Drafts are written to `SKILLS_MANAGED_DIR` (the `ts-harness-skills-managed`
volume at `/skills-managed` in Docker), so the repo skills at `SKILLS_DIR` can
stay read-only. When `SKILLS_MANAGED_DIR` is unset, drafts go to `SKILLS_DIR`,
which must then be writable. See [`skills.md`](skills.md) for pasting skills,
folder uploads, and the two roots.

## Admin API

Requires gateway auth and `admin` or `owner` role.

```bash
# List drafts
curl -fsS -H "Authorization: Bearer $GATEWAY_TOKEN" \
  "http://127.0.0.1:${GATEWAY_PORT:-18789}/admin/skills/drafts"

# Inspect
curl -fsS -H "Authorization: Bearer $GATEWAY_TOKEN" \
  "http://127.0.0.1:${GATEWAY_PORT:-18789}/admin/skills/drafts/team-runbook"

# Approve (when using actor roles, include admin or owner)
curl -fsS -X POST -H "Authorization: Bearer $GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"actor":{"actor_id":"ops","roles":["admin"]}}' \
  "http://127.0.0.1:${GATEWAY_PORT:-18789}/admin/skills/drafts/team-runbook/approve"

# Reject
curl -fsS -X POST -H "Authorization: Bearer $GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"actor":{"actor_id":"ops","roles":["admin"]},"reason":"not needed"}' \
  "http://127.0.0.1:${GATEWAY_PORT:-18789}/admin/skills/drafts/team-runbook/reject"
```

When the request carries roles (JWT claim or body `actor.roles`), `admin` or
`owner` is required. Static gateway-token calls with no roles follow the same
operator pattern as other admin routes.

## Non-goals

- No auto-approve of agent-created skills
- No writes into `skills/coding-backend/`
- No Slack Block Kit approve buttons in v1
- Catalog reload is process-local (single `agent-runtime`)

## Related

- Scheduled skills: [`scheduled-skills.md`](scheduled-skills.md)
