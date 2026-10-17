# Workflow Engine Production State

## Purpose

This document describes the production-ready workflow engine target state that
builds on the MVP runner described in `workflow-engine.md`. The MVP proves that a
repo-owned runner can poll ready tasks, claim work, dispatch agent-runtime subagents,
record outcomes, pause for approvals, and recover stale running work.

Production readiness means the runner is dependable enough to own real
multi-agent business workflows without a human manually shepherding every state
transition.

## Production Outcome

In production, TS Harness agent-runtime executes specialist agents. The
workflow engine is the durable control plane for multi-step work.

The production flow is:

1. A Slack message, API request, cron job, or operator action identifies a
   `workflow_type`.
2. The workflow definition expands into a task graph under one `workflow_id`.
3. The workflow runner polls or subscribes for ready tasks.
4. The memory API atomically claims each ready task and records the lease.
5. The runner dispatches claimed tasks through the TS Harness agent-runtime adapter.
6. Agent-runtime executes specialist work with the configured agent.
7. The runner records outputs, failures, blocked states, retries, approvals, and
   runtime task ids through the memory API.
8. The runner advances dependent tasks, pauses at gates, or starts compensation
   when the workflow cannot proceed.
9. Users receive progress and completion updates through the original channel or
   a configured notification adapter.

## Production Components

- `memory-api`: the only write interface for workflow state, approvals, claims,
  runtime correlation, and audit events.
- `workflow-runner`: a long-running sidecar or service that owns readiness,
  claiming, dispatch, retries, timeouts, and recovery.
- Agent-runtime adapter: the runner boundary that starts specialist work and maps
  runtime results back to workflow outcomes.
- Workflow definitions: versioned process templates that define steps, agents,
  dependencies, approval gates, retry policy, timeout policy, and compensation.
- Notification adapters: channel-specific progress and approval surfaces, such
  as Slack, without making Slack the only input channel.
- Postgres or RDS: durable storage for task state, leases, attempts, audit
  events, approvals, runtime ids, and workflow metadata.
- Operations tooling: smoke tests, health checks, runbooks, secret rotation, and
  stale-workflow debugging.

## Capabilities Beyond The MVP

### Workflow Definitions

The MVP can run manually-created tasks. Production should support versioned
workflow definitions that can expand a user request into a repeatable task graph.

Definitions should describe orchestration, not domain behavior. A definition
should say which step runs, which agent owns it, what it depends on, and whether
approval is required. Skills remain responsible for domain-specific work such as
generating Jira stories, drafting a PRD, testing a branch, or preparing release
notes.

For example, a PRD workflow can continue after approval:

1. Draft PRD.
2. Review PRD.
3. Wait for human approval.
4. Generate user stories.
5. Write stories to Jira.
6. Ask for implementation approval.
7. Dispatch `dev-agent` for implementation planning or PR work.

### First-Class Workflow Records

**Status: implemented.** Workflow rows are created via the memory API
(`POST /workflows/create`) and carry workflow-level metadata used by the runner,
delivery programs, and operability status.

Workflow records track:

- `workflow_id`
- `workflow_type`
- definition version
- source channel or source system
- requester
- current workflow status
- started and completed timestamps
- cancellation reason
- parent workflow, if the workflow was spawned by another workflow

Task state should remain separate from workflow state. A workflow can fail,
pause, or cancel because of policy even when individual tasks have different
terminal states.

### Dispatch Adapter Reliability

The agent-runtime adapter should be isolated from the runner core. The runner decides
what should happen next; the adapter decides how to ask the agent runtime to execute a
specialist task.

The adapter should provide:

- deterministic request payloads containing `workflow_id`, `task_id`,
  `workflow_type`, `step_name`, input, and required skill hints
- runtime task id capture
- clear mapping from agent-runtime result states to workflow states
- retryable versus non-retryable dispatch failure classification
- bounded concurrency per agent and per workflow
- structured errors that can be written to task `error` fields

### Approval And Human Gates

Approval gates should be durable and channel-independent. Slack can be the first
approval surface, but the workflow state should not depend on a Slack thread
remaining available.

Production approval handling should include:

- approval request audit events
- approver identity
- approval decision timestamp
- rejection reason
- request-changes outcome, separate from hard rejection if needed
- expiry behavior for approvals that are not answered
- resume behavior after approval

### Retry, Timeout, And Stale Work Recovery

The production runner should enforce retry and timeout policies consistently.
Retries should not be ad hoc prompt behavior.

Production policy should cover:

- max attempts per task
- retry backoff
- retryable failure classes
- stale `running` task detection
- lock ownership and lease expiry
- terminal failure when retries are exhausted
- blocked state when human or external intervention is required
- downstream cancellation or compensation when a dependency fails

### Compensation And Rollback

The MVP can mark failed or blocked tasks. Production should support explicit
compensation for workflows that have already caused external effects.

Compensation examples:

- close or label Jira tickets created by an abandoned workflow
- revert a generated branch or PR
- notify a channel that an automated delivery flow was cancelled
- create a cleanup task for `ops-agent`

Compensation should be declared in workflow definitions and executed through the
same task state machine as normal work.

Today, technical failures use the declared `compensate_on_failure` hook, and
program cancel runs deterministic cancel compensation over the program tree's
`program_external_refs` (see
[`governed-delivery-flow.md`](../runbooks/governed-delivery-flow.md#cancel-and-cleanup)).
Deployments are never compensated; release failures use the `rollback` node.

### Observability And Audit

Production workflows should be explainable after the fact. A human should be
able to answer what happened, who approved it, which agent ran, what failed, and
why the runner chose the next step.

Production observability should include:

- workflow-level audit events
- task-level audit events
- runner decision logs
- runtime task correlation
- per-workflow status output
- counts for ready, running, blocked, failed, stale, and approval-pending tasks
- error summaries suitable for Slack or runbooks

### Operations And Deployment

The runner should be deployed as an optional feature first, then promoted after
smoke tests and operational runbooks are reliable.

Production deployment should include:

- `workflow_runner_enabled` feature flag
- Docker Compose service or equivalent managed service
- health check for the runner process
- memory API health dependency
- startup ordering with agent-runtime and memory API
- secret rotation support
- verification in `verify-multi-agent.sh`
- rollback steps that disable the runner without deleting workflow state
- runbook for stuck workflows and stale locks

## Production Readiness Checklist

implemented by the MVP:

- Workflow definitions are versioned and validated before use.
- Runner claims tasks through the memory API before dispatch.
- Runner never executes specialist work directly.
- Agent-runtime dispatch is isolated behind an adapter.
- Approval-gated tasks cannot run until approval is recorded.
- `workflow_plans` stores proposed child tasks for plan-level approval.
- Leases prevent duplicate dispatch and recover stale running tasks.
- Retries are policy-driven and bounded.
- Workflow and task state changes are audited.
- Runtime task ids are recorded for dispatched work.
- Runner can be enabled, disabled, restarted, and rolled back safely.
- Smoke tests cover task creation, claim, dispatch, outcome recording, approval,
  retry, and stale-task recovery.
- Runbooks explain common failure modes and recovery steps.

implemented (updated 2026-08-05 durability upgrades):

- First-class workflow records track workflow-level metadata and status.
- Compensation hooks exist (`compensate_on_failure`) and are wired on
  `implement_work_orders` technical failure (`record_implement_failure`).
- Bound-channel progress/completion notifications for workflow events.
- Operability status payload on `workflow_status` / `program_status`
  (`waiting_on`, lease, qa_status, recommended_action).
- Host merge poller materialization via phase 15 (`poll-github-merges`).

implemented (updated 2026-09-30 enterprise delivery hardening):

- One root program per change with child programs (`parent_program_id`,
  `root_program_id`), child status rollup, and `program_external_refs` for every
  Jira issue, PR, branch, and deployment a program creates.
- Recorded human decisions (`workflow_decisions`) with actor and roles; gate
  `approval.required_roles`, `expires_after_seconds`, and `escalate_to_role`.
- Tenant Jira and GitHub bindings enforced in code.
- Merge readiness: `github.pr_checks` waits on the PR head SHA and the required
  status `harness/independent-review`, pinned per program.
- Release on the program graph: `deploy_release` 2.0 promotes through the target
  repo's GitHub deploy behind recorded promote decisions; `rollback_release`
  redeploys the last good SHA.
- Parallel work-order waves: `implement_work_orders` 2.0 forks
  `deliver_work_order` child workflows and joins them. Children are pinned to
  the child version current when the parent started.
- Reusable workflow units: versioned stage fragments (the built-in
  `verified_merge` tail is shared by `deliver_work_order` and
  `implement_jira_ticket`) and the generic `fork_children` step with a
  `workflow.child_terminal` join, both authored in the Workflow Builder.
- Wait deadlines (`escalate_at`, `expires_at`) and the `escalate-stale-waits`
  system job; expired waits stay blocked with a recommended action.
- Portfolio board of root programs in the Admin Console (phase, status, owner,
  children, wait age, escalation, recommended action) with filters.
- Cancel compensation over `program_external_refs`: label and comment Jira, close
  open PRs, optionally delete TS Harness branches, with results on program status.

still future:

- Graphical dashboards beyond the portfolio board (trend and throughput views).
- Automatic retry of failed cancel-compensation actions (today an operator
  re-runs cancel after fixing credentials).
- Per-wait `escalate_to_role` on external waits (today only approval gates name
  an escalation role).

## PRD To Jira Production Example

A production PRD-to-Jira workflow could look like this:

1. Slack receives: `Create a PRD for an online tutorial.`
2. The orchestrator or request adapter selects `workflow_type = prd_to_jira`.
3. The workflow definition creates tasks for requirements, drafting, review,
   approval, story generation, Jira writing, and implementation handoff.
4. The runner dispatches `pm-agent` to clarify and draft the PRD.
5. The workflow pauses for human PRD approval.
6. After approval, the runner dispatches the story-generation step to the agent
   and skill declared by the workflow definition.
7. The runner dispatches the Jira-writing step with the approved PRD and story
   output as input.
8. The runner asks for implementation approval if required.
9. The runner dispatches `dev-agent` for planning or implementation work.
10. The runner records all outcomes, runtime ids, approvals, and audit events.

This is the production distinction: the user request becomes a durable workflow,
not a chain of manual chat-session decisions.

## Boundaries

The production workflow engine should not become a replacement for agent-runtime,
Slack, Jira, or agent skills.

It should own:

- workflow state
- dependency resolution
- task readiness
- claiming and leases
- retries and timeouts
- approval pauses and resumes
- compensation scheduling
- audit and observability

It should delegate:

- specialist execution to TS Harness agent-runtime
- domain behavior to skills and agents
- channel-specific messaging to notification adapters
- external system writes to the appropriate agent, skill, or integration adapter

## Promotion Path

Production should be reached in stages:

1. Ship the MVP runner disabled by default.
2. Enable it in staging for synthetic workflows.
3. Add static workflow definitions for one or two real workflows.
4. Prove approval, retry, stale recovery, and rollback behavior.
5. Add workflow-level records and status views if shared `workflow_id` is no
   longer enough.
6. Enable the runner for selected production workflows.
7. Expand workflow definitions and compensation policies after operational
   evidence is good.

The goal is not to build a large workflow platform immediately. The goal is to
make multi-agent work durable, explainable, recoverable, and safe enough for
real production workflows.
