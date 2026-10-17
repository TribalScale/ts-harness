# Scheduled skills

Cron jobs can inject governed skills by id before the LLM runs. Resolution
reuses the workflow skill catalog (assignment, approval, toolsets, connectors).

## Job fields

In `runtime-cron-jobs.json` (written by Phase 15):

| Field | Purpose |
|-------|---------|
| `skill_id` | Single skill id (normalized into `skill_hints`) |
| `skill_hints` | List of skill ids to inject |
| `prompt` | Operator instruction layered on the skill text |

When neither skill field is set, behavior stays prompt-only (legacy).

Example:

```json
{
  "name": "weekly-ops-health-check",
  "cron": "0 10 * * 1",
  "timezone": "UTC",
  "agent_id": "ops-agent",
  "skill_id": "ops/health-check",
  "prompt": "Run the scheduled health check skill and summarize findings.",
  "announce": {"channel": "slack", "to": "channel:C_OPS"}
}
```

## Force a run

```bash
curl -fsS -X POST \
  -H "Authorization: Bearer $GATEWAY_TOKEN" \
  "http://127.0.0.1:${GATEWAY_PORT:-18789}/cron/trigger"
```

## Debug failed inject

If hints are set but the skill is unknown, unassigned, draft, or blocked:

- No LLM call is made (fail closed).
- Audit action: `cron_skill_resolve_failed`
- Metric: `cron_skill_resolve_failed`

Check agent `skills:` categories in runtime YAML, skill frontmatter
`approval_status` / `trust_level`, and `/skills/doctor?agent_id=ops-agent`.

## Related

- Cron registration: [`cron-registration-debugging.md`](cron-registration-debugging.md)
- Skill authoring: [`skill-authoring.md`](skill-authoring.md)
