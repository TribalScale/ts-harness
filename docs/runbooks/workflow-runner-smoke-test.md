# Workflow-Runner Smoke Test

Use this runbook after enabling `WORKFLOW_RUNNER_ENABLED=true` on an instance.
The runner is optional. A second runner idles until the first one's database
lease expires. `stop_grace_period` is 120 seconds. SIGTERM stops the runner
from claiming another task; it does not interrupt a dispatch already in
progress. If that dispatch is still running when the grace period ends, Docker
kills the process. The task lease and `WORKFLOW_RUNNER_STALE_POLICY=block`
recover it.

## Verify Configuration

1. Confirm Terraform rendered the runner environment:
   - `WORKFLOW_RUNNER_ENABLED`
   - `WORKFLOW_RUNNER_POLL_INTERVAL_SECONDS`
   - `WORKFLOW_RUNNER_BATCH_SIZE`
   - `WORKFLOW_RUNNER_LEASE_SECONDS`
   - `WORKFLOW_RUNNER_STALE_POLICY`
2. Confirm compose includes `docker-compose.workflow-runner.yml`.
3. Confirm containers:

```bash
docker compose ... ps memory-api workflow-runner
```

The runner healthcheck uses `/tmp/workflow-runner.tick`; it is unhealthy if no
tick is written within three poll intervals.

## Memory API Smoke

```bash
docker compose ... exec agent-runtime \
  curl -s http://127.0.0.1:18792/health
```

```bash
docker compose ... exec agent-runtime \
  curl -s -X POST http://127.0.0.1:18792/workflow/ready \
  -H 'Content-Type: application/json' \
  -d '{"limit":1}'
```

## Expand Stage And Manual Approval

1. Create or locate a workflow whose `delivery_planning` parent task produced a
   plan.
2. Find the plan id from the parent expand task:

```bash
docker compose ... exec agent-runtime \
  curl -s http://127.0.0.1:18792/workflow/<workflow_id>
```

Look for `output.plan_id`.

3. Inspect the proposed tasks:

Endpoint: `GET /workflow/plan/<plan_id>`.

```bash
docker compose ... exec agent-runtime \
  curl -s http://127.0.0.1:18792/workflow/plan/<plan_id>
```

This returns the `workflow_plans` row, including `proposed_tasks`.

4. Approve the plan:

```bash
docker compose ... exec agent-runtime \
  curl -s -X POST http://127.0.0.1:18792/workflow/plan/approve \
  -H 'Content-Type: application/json' \
  -d '{"plan_id":"<plan-id>","actor":"human:<id>"}'
```

5. Confirm `workflow_plans.materialized_at` is set and child tasks appear in:

```bash
docker compose ... exec agent-runtime \
  curl -s http://127.0.0.1:18792/workflow/<workflow_id>
```

Children should have `approval_status='not_required'`.

To reject a plan:

```bash
docker compose ... exec agent-runtime \
  curl -s -X POST http://127.0.0.1:18792/workflow/plan/reject \
  -H 'Content-Type: application/json' \
  -d '{"plan_id":"<plan-id>","actor":"human:<id>","rejection_reason":"not approved"}'
```

## Common Failure Modes

- Missing memory-api: `/workflow/ready` fails or times out. The runner retries
  transport/5xx briefly, then logs `memory_api_unavailable` and continues the
  loop (lists soft-fail to empty). It should not crash-loop on transient DNS.
- Runtime not listening yet: look for `runtime_not_ready` / `runtime_healthz_failed`.
  Claims are skipped that tick; stale sweep still runs.
- Dispatch connection errors: look for `transport_error:` in blocked task errors
  and `retry_scheduled`. `/workflow/retry` sets `next_attempt_at` (exponential,
  capped at 300s) so the runner backs off before reclaiming.
- Unexpected post-claim exceptions: look for `dispatch_exception`; the runner
  blocks and requeues when retry budget remains.
- Whole-tick infra failures: look for `tick_error` with growing `next_interval`
  (poll backoff, capped at 60s). The tick file is still refreshed so the
  container healthcheck reflects process liveness.
- Runtime HTTP 401: verify `GATEWAY_TOKEN` and `AGENT_RUNTIME_URL`.
- Wrong `agent_id`: the claim endpoint returns `claimed=false`.
- Rejected plan: no children are created and the workflow halts at the expand
  parent.
- Stale lock: the runner marks a stale running task blocked or failed. A stale
  lock counts against retry budget.
- Retry cap hit: once `attempt > max_retries`, the task stops being claimable.

Manual recovery (rare after the resilience hardening above):

```bash
curl -sS -X POST http://127.0.0.1:18792/workflow/block \
  -H 'Content-Type: application/json' \
  -d '{"id":"<task-id>","error":"ops unblock","actor":"ops"}'
curl -sS -X POST http://127.0.0.1:18792/workflow/retry \
  -H 'Content-Type: application/json' \
  -d '{"id":"<task-id>","reason":"ops requeue","actor":"ops"}'
```

Runner logs are single-line JSON on stdout; inspect them with:

```bash
docker compose ... logs --tail=100 workflow-runner
```
