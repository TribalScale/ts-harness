# Skill: workflow-state

Use this skill to coordinate multi-step workflows with persistent state. The
memory-api sidecar is the executable interface; do not issue raw SQL from the
orchestrator.

manual orchestrator-driven workflows remain valid when the workflow runner is
disabled. When the runner is enabled, the orchestrator creates workflow tasks and
lets the runner claim, dispatch, retry, and recover ready work.

## When to use

- Any task spanning 2+ specialist dispatches in sequence or parallel
- When a workflow may outlast a single session
- When a step needs explicit human approval before dispatch
- When two workers may attempt the same step concurrently

## API Primitives

Call the memory-api sidecar at `http://127.0.0.1:18792`.

Create a task:

```json
POST /workflow/task
{
  "workflow_id": "<uuid>",
  "workflow_type": "implement_delivery",
  "step_name": "create_jira",
  "agent_id": "pm-agent",
  "approval_required": true,
  "input": {"prd_id": "<artifact-id>"},
  "actor": "orchestrator"
}
```

Claim before dispatch:

```json
POST /workflow/claim
{
  "id": "<task-id>",
  "agent_id": "pm-agent",
  "actor": "orchestrator"
}
```

Only dispatch the specialist if the response is:

```json
{"claimed": true, "id": "<task-id>"}
```

Complete, fail, block, or cancel:

```json
POST /workflow/complete
{"id": "<task-id>", "output": {"summary": "done"}, "actor": "orchestrator"}
```

```json
POST /workflow/fail
{"id": "<task-id>", "error": "Timeout waiting for CI", "actor": "orchestrator"}
```

Approval gates:

```json
POST /workflow/approve
{"id": "<task-id>", "actor": "human:<user-id>"}
```

```json
POST /workflow/reject
{"id": "<task-id>", "actor": "human:<user-id>"}
```

Fetch workflow status:

```text
GET /workflow/<workflow_id>
```

Runner endpoints:

```json
POST /workflow/ready
{"limit": 5}
```

```json
POST /workflow/retry
{"id": "<task-id>", "actor": "workflow-runner"}
```

```json
POST /workflow/runtime
{"id": "<task-id>", "runtime_task_id": "<runtime-task-id>", "actor": "workflow-runner"}
```

```json
POST /workflow/stale
{"limit": 5}
```

Plan-level expansion uses `workflow_plans`:

```json
POST /workflow/plan/propose
{
  "workflow_id": "<workflow-id>",
  "parent_task_id": "<expand-task-id>",
  "proposed_tasks": [
    {"step_name": "write_jira_story", "agent_id": "pm-agent", "input": {}}
  ]
}
```

```json
POST /workflow/plan/approve
{"plan_id": "<plan-id>", "actor": "human:<user-id>"}
```

```json
POST /workflow/plan/reject
{"plan_id": "<plan-id>", "actor": "human:<user-id>", "rejection_reason": "..."}
```

## Transition Rules

Allowed task transitions:

- `pending -> running`
- `pending -> cancelled`
- `running -> succeeded`
- `running -> failed`
- `running -> blocked`
- `running -> cancelled`
- `blocked -> pending`
- `blocked -> cancelled`
- `failed -> pending`
- `failed -> cancelled`

Approval-gated tasks cannot be claimed or completed until
`approval_status = approved`. Rejected tasks cannot be claimed.

## Rules

- **Never** dispatch before `/workflow/claim` returns `claimed: true`.
- **Never** complete an approval-gated task before approval is recorded.
- Every workflow state change is audited to `session_audit_log`.
- Workflow skeletons define lifecycle guardrails; skills define agent behavior
  inside each task.
- Agents requested by `expand` stages return structured `proposed_tasks`.
  Approval is plan-level, not per-child.
- Stale running tasks consume retry budget when recovered; `max_retries = 0`
  means one attempt and no retries.
- If a dependency step failed, cancel downstream tasks or escalate before
  continuing.
