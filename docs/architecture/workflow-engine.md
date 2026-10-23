# Workflow Engine Architecture

## Hook engine (v1.1)

Workflow hook semantics live in the runtime `WorkflowEngine` under
`runtime/ts_harness/workflows/`. Skeleton JSON declares hooks; the engine
dispatches them on promotion, human gates, and runner transitions. See
[workflow-hooks.md](workflow-hooks.md) for the hook catalog and expression language.

## Current Phase

TS Harness owns a repo-hosted workflow control plane: durable task state,
explicit state transitions, approval gates, atomic claim semantics, audit events,
first-class workflow records, delivery programs, declarative stage contracts,
`qa_gate`, compensation hooks, and operability status aggregation.

The memory API sidecar is the write interface. The `workflow-runner` sidecar
claims ready work and dispatches through the TS Harness agent-runtime
adapter (`POST /workflow/dispatch`). Specialist agents execute via the runtime;
workflow skeletons under `config/workflows/` define control flow.

## Future Phase

A later workflow engine can add a runner that reads workflow definitions,
schedules dependency graphs, dispatches ready tasks, handles retries and
timeouts, pauses for approvals, resumes blocked work, and coordinates
compensation or rollback plans.

The workflow engine owns workflow control flow: dependency resolution, task
readiness, retries, timeouts, approval pauses, and recovery. The agent runtime remains the
execution runtime for subagent work and is called through an adapter interface.

This keeps workflow state portable. A future workflow could dispatch through
the agent runtime, a local worker, a CI job, or another execution backend without changing
the workflow state model.

The future runner is responsible for:

- Loading workflow definitions.
- Creating task graphs.
- Claiming ready tasks atomically.
- Dispatching executable tasks through an adapter.
- Retrying failed tasks according to policy.
- Marking tasks timed out when leases expire.
- Pausing and resuming approval-gated work.
- Emitting audit events for all state changes.
- Triggering compensation steps when a workflow cannot proceed.

The future runner should not:

- Execute specialist work directly.
- Store workflow state outside the memory API or database.
- Depend on Slack as the only input channel.
- Depend on the agent runtime as the only execution backend.
- Bypass approval gates or audit logging.

Minimum viable runner:

- Poll or subscribe for pending tasks.
- Claim tasks whose dependencies have succeeded.
- Dispatch claimed tasks to the agent runtime.
- Record success, failure, blocked, or cancelled outcomes.
- Skip approval-gated tasks until approved.
- Mark stale running tasks as failed or blocked after timeout.

## MVP Runner

The `workflow-runner` sidecar polls the memory API for ready tasks, claims them
with leases, dispatches through the TS Harness agent-runtime adapter,
records `runtime_task_id`, writes terminal or blocked outcomes, retries within
`max_retries`, and recovers stale locks according to
`WORKFLOW_RUNNER_STALE_POLICY`.

Workflow definitions are versioned skeletons with `task`, `expand`, `compensate`,
and barrier stages, plus declarative contracts (`contracts` / `contract_ref`).
`expand` stages return structured `proposed_tasks`; plans are stored in
`workflow_plans` and materialized after plan-level approval. Implement paths
enforce `qa_gate` before `await_human_merge`. Compensation can be declared via
`on_failure.compensate_on_failure`.

The `verified_merge` fragment (1.6.0) runs `await_pr_checks` → `review_precheck`
→ `code_review` → `finalize_code_review` → `qa_gate` → `ensure_fresh` →
`await_human_merge`. `review_precheck`, `finalize_code_review` and
`ensure_fresh` are system steps (barrier stages run by the runner, no agent);
`code_review` runs the read-only `review-agent`, and `finalize_code_review`
computes the verdict from its findings. Blocked barriers and blocked external waits
both run the stage's `on_block`, so a stale base (`stale_base`) or a sibling
merge (`base_advanced`) sends the PR back to the implement stage as a refresh
revision. `revise_on_reject` supports a named `revise.budget`, counted
separately from the default budget, and `revise.static_input` merged into the
revision input. `on_block.cases` overrides that config per block reason: the
first case whose `error_prefix` matches the block error is applied over it, and
`action: "stop"` leaves the stage blocked without a revision. `await_human_merge`
uses this for human rejection (a "Request changes" review or `/revise` comment
revises with `budget: "human"`; a closed PR stops).

A task stage may set `"execution": "coding_job"` (`deliver_work_order` 1.5.0
does). With `CODING_JOBS_ENABLED=true` the runner does not dispatch it to an
agent: it queues a row in `coding_jobs`, parks the task on
`awaiting_external:coding.finished` and moves on. `coding-launcher` starts a
worker container per job; the worker reports to the memory API with a
per-job token, and completion (or the `sweep_coding_jobs` watchdog marking it
lost) puts the task back to pending in the same transaction. On that dispatch
the runner reads the unconsumed job result, maps it to the stage output and
runs the stage contract (including `validate_coding_backend_pr_url`) before the
normal success or blocked handling, then marks the job consumed. A coding_job
stage must have a `contract_ref`.

Every task has a claim lane: `fast` for barriers, waits and approval gates,
`coding` for `execution: coding_job` stages, and `agent` otherwise. A stage may
override it with `"lane"`. Tasks also carry the `team_id` and `requested_by` of
their program (a trigger copies them at insert, following parent workflows).
`POST /workflow/claim-batch` (`claim_ready_workflow_tasks`, migration 037)
claims up to N ready tasks of one lane with `FOR UPDATE SKIP LOCKED`, at most
one per workflow and none for a workflow that already has a running task.
Approved gates and resumed or retried tasks go first, then the requester and
team with the fewest running tasks, then the oldest. Runners extend a running
task's lease with `POST /workflow/tasks/{id}/renew-lease`.

Coding jobs are claimed within the global limit and per-requester, per-program
and per-team quotas from `scheduling.coding` in the runtime config (defaults 2,
3 and 8; 0 means no limit). `CODING_QUOTA_PER_USER`, `CODING_QUOTA_PER_PROGRAM`
and `CODING_QUOTA_PER_TEAM` override them, and `scheduling.coding.global`
replaces `CODING_MAX_RUNNING` when set. A job over a quota stays queued.

**Implemented today:** first-class workflow records, delivery programs, stage
contracts, QA gates, compensation hooks, external wait/`match_all`, merge poller
host materialization (`scripts/phases/15-automation-cron.sh`), and stuck-status
fields on `workflow_status` / `program_status`. Workflows compose through
versioned stage fragments (`kind: "fragment"` includes that expand at load) and
`fork_children` child workflows with a generic join, both authored in the
Workflow Builder.

**Still future:** Slack-native approval
surfaces beyond current channel notifications, and a graphical stuck-workflow
dashboard (API payload exists; UI does not).

## Program definitions and pinning

Delivery topology is **data-defined**, not hardcoded phase constants.

- **Built-in document:** `config/programs/software_delivery.json` encodes the
  default DAG (`prd` → `jira` | `work_orders` → `implement`),
  workflow variants, artifact requirements, completion adapters, and edges.
- **Managed definitions:** versioned under
  `/workflow-definitions/programs` on the shared workflow-definitions volume
  (`TS_HARNESS_PROGRAM_DEFINITION_STORE`). Draft → validate → activate
  mirrors workflow builder semantics; see
  [program-builder.md](../runbooks/program-builder.md).
- **Registry:** `ProgramDefinitionRegistry` hot-reloads managed active
  definitions via `programs/active/manifest.json` generation and fails closed
  on missing/mismatched history.
- **Instance pins:** on `create_program`, memory-api persists
  `program_type`, `program_definition_version`, `program_definition_revision`,
  `program_definition_snapshot`, and `resolved_workflow_versions`. Advances and
  status rendering use the frozen snapshot; activating a new definition only
  affects new instances.
- **Legacy rows:** pre-pinning programs with a null snapshot continue through
  `FROZEN_LEGACY_COMPATIBILITY_TOPOLOGY` (`definition_version: legacy-compat`).
  That payload is not live topology for new work. See
  [compatibility.md](../compatibility.md).

Hardcoded `PHASE_ORDER` / `NEXT_PHASES` / `PHASE_SKELETON` /
`ENTRY_REQUIREMENTS` / `PROGRAM_PHASES` are retired as live sources of truth.

Example Slack PRD flow:

1. Slack receives: `Create a PRD for an online tutorial.`
2. The Slack adapter creates a workflow instance.
3. The workflow engine expands the workflow definition into tasks such as
   `clarify_requirements`, `draft_prd`, `review_prd`, `approval`, and
   `finalize_prd`.
4. The engine claims ready tasks through the memory API.
5. Claimed execution tasks are dispatched to TS Harness agent-runtime.
6. The runtime returns outputs, failures, or blocked status.
7. The engine advances dependent tasks, pauses for approvals, retries failures,
   or triggers compensation.
8. Slack receives progress updates and the final result.

## Non-Goals For Current Remediation

- No background worker or queue.
- No autonomous retry loop.
- No scheduler beyond existing agent-runtime cron behavior.
- Workflow runner dispatches to the TS Harness AgentRuntime adapter at `/workflow/dispatch`.
- No production deployment automation beyond existing human-gated runbooks.

## State Model

Allowed task status transitions:

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

Terminal states are `succeeded` and `cancelled`.

## Approval Gates

Approval-gated tasks set `approval_required = true` and start with
`approval_status = pending`. They cannot be claimed or marked `succeeded` until
approval is explicitly recorded. Rejected tasks cannot be claimed.

## Audit Requirements

Every task create, claim, completion, failure, block, cancellation, approval, and
rejection writes a `session_audit_log` row. If a future phase adds first-class
workflow records, workflow creation and workflow completion should also be
audited. This makes future runner behavior explainable before the runner exists.
