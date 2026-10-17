# Skill: audit-logging

Use this skill to write compliance-grade audit entries to the `session_audit_log` table. Every routing decision, specialist dispatch, and result must have an audit entry.

## What to log and when

| Trigger | action_type | When |
|---------|-------------|------|
| User sends a message | `user_message` | Before any processing |
| You decide to route to a specialist | `route_decision` | After routing choice, before dispatch |
| You spawn a specialist session | `specialist_dispatch` | Immediately before dispatching |
| Specialist returns a result | `specialist_result` | After you receive the structured output |
| A tool is invoked by an agent | `tool_invocation` | When a significant tool call is made |
| A specialist returns a tool result | `tool_result` | After a meaningful tool result |
| You escalate to a human | `escalation` | When escalating |
| Any unhandled error occurs | `error` | On exception or failure |
| You synthesize the final answer | `synthesis` | Before responding to the user |

## SQL for each action type

### user_message
```sql
INSERT INTO session_audit_log
  (session_id, agent_id, action_type, channel, user_id, summary, details, occurred_at)
VALUES (
    '<session-id>',
    'orchestrator',
    'user_message',
    'slack',            -- or 'web', 'api', etc.
    '<user-id>',
    'User requested: create new feature branch for PROJ-456',  -- summary only, NO verbatim PII
    '{"request_category": "engineering", "has_attachment": false}'::jsonb,
    now()
);
```

### route_decision
```sql
INSERT INTO session_audit_log
  (session_id, agent_id, action_type, summary, details, confidence, occurred_at)
VALUES (
    '<session-id>',
    'orchestrator',
    'route_decision',
    'Routing to dev-agent: engineering task requiring code changes',
    '{"target_agent": "dev-agent", "routing_rule": "code_change", "alternatives_considered": ["qa-agent"]}'::jsonb,
    0.95,
    now()
);
```

### specialist_dispatch
```sql
INSERT INTO session_audit_log
  (session_id, agent_id, action_type, summary, details, occurred_at)
VALUES (
    '<session-id>',
    'orchestrator',
    'specialist_dispatch',
    'Dispatching dev-agent for: implement OAuth2 refresh token logic',
    '{"specialist": "dev-agent", "workflow_task_id": "<task-uuid>", "input_summary": "OAuth2 refresh token implementation"}'::jsonb,
    now()
);
```

### specialist_result
```sql
INSERT INTO session_audit_log
  (session_id, agent_id, action_type, summary, details, confidence, occurred_at)
VALUES (
    '<session-id>',
    'orchestrator',
    'specialist_result',
    'dev-agent completed: PR #42 opened with OAuth2 changes',
    '{"specialist": "dev-agent", "status": "succeeded", "pr_url": "...", "warnings": []}'::jsonb,
    0.9,
    now()
);
```

### escalation
```sql
INSERT INTO session_audit_log
  (session_id, agent_id, action_type, user_id, summary, details, occurred_at)
VALUES (
    '<session-id>',
    'orchestrator',
    'escalation',
    '<user-id>',
    'Escalating to human: production deployment requires explicit approval',
    '{"reason": "deployment_gate", "requires_approval_from": "team-lead"}'::jsonb,
    now()
);
```

### error
```sql
INSERT INTO session_audit_log
  (session_id, agent_id, action_type, summary, details, occurred_at)
VALUES (
    '<session-id>',
    'orchestrator',
    'error',
    'dev-agent failed: timeout after 120s',
    '{"specialist": "dev-agent", "error_type": "timeout", "workflow_task_id": "<task-uuid>"}'::jsonb,
    now()
);
```

### synthesis
```sql
INSERT INTO session_audit_log
  (session_id, agent_id, action_type, summary, details, confidence, occurred_at)
VALUES (
    '<session-id>',
    'orchestrator',
    'synthesis',
    'Synthesized response from 2 specialists: dev-agent (succeeded), qa-agent (succeeded)',
    '{"specialists_used": ["dev-agent", "qa-agent"], "output_length_chars": 450}'::jsonb,
    0.92,
    now()
);
```

## Querying the audit trail

```sql
-- Full session timeline
SELECT agent_id, action_type, summary, confidence, occurred_at
FROM session_audit_log
WHERE session_id = '<session-id>'
ORDER BY occurred_at;

-- All dispatches in the last 24 hours
SELECT session_id, summary, details->>'specialist' AS specialist, occurred_at
FROM session_audit_log
WHERE action_type = 'specialist_dispatch'
  AND occurred_at > now() - interval '24 hours'
ORDER BY occurred_at DESC;

-- Recent escalations
SELECT session_id, user_id, summary, occurred_at
FROM session_audit_log
WHERE action_type = 'escalation'
ORDER BY occurred_at DESC
LIMIT 20;
```

## Hard rules

- **NEVER** log verbatim user messages, passwords, API keys, or PII — summaries only.
- **NEVER** log full message content — use category, topic, and key entities.
- Every `session_id` must be consistent across all log entries for a single user turn.
- Every specialist dispatch must be followed by a `specialist_result` or `error` entry.
- Log `error` for all failures, even internal ones not visible to the user.

## Retention

The `purge_old_audit_logs(retention_days)` SQL function deletes rows older than the specified number of days. The default is 90 days. Run it as part of the `memory-maintenance` cron job:

```sql
SELECT purge_old_audit_logs(90);
```
