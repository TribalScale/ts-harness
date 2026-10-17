# Workflow Hooks

TS Harness workflows are declared in JSON skeletons under `config/workflows/`. The runtime `WorkflowEngine` interprets skeleton hooks and materializes tasks through memory-api.

## What belongs in JSON vs Python

| In JSON | In Python / skills |
|---------|-------------------|
| Stage graph, agents, dependencies, retries | Agent prompts and tool policies |
| `input_map`, `on_reject`, `on_block`, `on_complete`, `on_failure` | Jira/GitHub integrations |
| `promotion`, `notifications`, `wait_external`, `join` | OPA policy, expand validation rules |
| Hook names and configuration | Runner claim/dispatch/retry only |

## Hook catalog

| Hook | Trigger | Purpose |
|------|---------|---------|
| `promote` | `start_workflow` tool | Create workflow record + static DAG |
| `revise_on_reject` | `workflow_reject*` on gates with `on_reject`; runner `blocked` when `on_block` is set | Dynamic revise + re-gate loop (PRD reject or QA fail) |
| `hydrate_input` | Promote, approve, downstream stages | Resolve `input_map` into task input |
| `approve_gate` | `workflow_approve` | Approve gate + optional downstream hydrate |
| `expand_on_complete` | Runner on expand success | Validate/propose expansion plan |
| `notify_bound_channel` | Runner transitions | Slack thread updates from `notifications` |
| `present_specialist_output` | Runner after non-orchestrator task success (default) | Post specialist output to bound channel + orchestrator transcript; opt out with `present_output: false` |
| `compensate_on_failure` | Runner on terminal failure | Spawn compensate stage |
| `cancel_workflow` | `cancel_workflow` tool / API | Bulk cancel + unlink session |
| `wait_external` | Stage with `wait_external` block | Block until external resume |
| `fork_subworkflow` | `on_complete` on parent stage | Retired: validation rejects it in new definitions; in-flight tasks still run it. Use `fork_children` |
| `mark_prd_approved` | `approve_prd` on_complete (prd_request) | Set session approved; notify channel; no downstream tasks |
| `continue_delivery` | `continue_delivery` tool after confirm | Bridge to next phase (`jira_from_prd` when programs enabled) |
| `register_program` | `promote` when programs enabled | Create/link delivery program on workflow start |
| `complete_program_phase` | Runner when all phase tasks succeed | Merge artifacts; optional auto-advance |

## Expression language (`input_map`)

| Token | Meaning |
|-------|---------|
| `$prior.<step>.output` | Latest output from a predecessor step |
| `$source.output` | Latest succeeded task from `source_steps` (reject hooks) |
| `$reject.comments` | Reject tool comments |
| `$runtime.revision_count:<step>` | Count of tasks with step name + 1 |
| `$workflow.binding_key` | `workflows.source_channel` |
| `$item`, `$item.<field>` | Current item in `fork_children.for_each` |
| `$revise.id` | Task id created by revise hook (re-gate depends_on) |
| `$re_gate.id` | Task id created by the re-gate (resume_after depends_on) |

## Child workflows (`fork_children`)

A `barrier` stage with `hook: fork_children` starts child workflows; a following stage joins them with `wait_external` on `workflow.child_terminal`.

```json
{
  "name": "fork_locales",
  "kind": "task",
  "agent_id": "orchestrator",
  "depends_on": ["plan"],
  "barrier": {
    "hook": "fork_children",
    "workflow_type": "translate_page",
    "for_each": "$prior.plan.output.locales",
    "label": "$item.code",
    "input_map": { "locale": "$item.code", "page_url": "$prior.plan.output.page_url" }
  }
},
{
  "name": "join_locales",
  "kind": "task",
  "agent_id": "orchestrator",
  "depends_on": ["fork_locales"],
  "wait_external": {
    "hook": "wait_external",
    "event_type": "workflow.child_terminal",
    "children": { "source": "$prior.fork_locales.output.children" },
    "timeout_seconds": 86400
  }
}
```

| Field | Meaning |
|-------|---------|
| `workflow_type` | Child definition to start (required) |
| `for_each` | Expression that resolves to a list; one child per item. Omit it to start a single child |
| `label` | Expression evaluated per item that names the child in join output (default: the item index). Labels must be unique |
| `input_map` | Intake input for each child's first stage; `$item` / `$item.<field>` refer to the current item |

- The fork output is `{children: [{workflow_id, label}], child_workflow_ids, child_workflow_type}`.
- Re-running the barrier skips children it already started. It records them in the task input under `_forked_children`.
- Children are pinned to the child definition version that was current when the parent started. It is recorded in `_child_version`. `fork_work_order_wave` pins its children the same way.
- When every child finishes, the join output adds `children_outputs`: each label mapped to that child's merged task output. It also adds `failed_labels`. The join blocks when any child failed.
- Validation rejects the following:
  - an unknown or unloadable child type;
  - a fork cycle, where a child forks back into an ancestor;
  - a join whose `children.source` is not `$prior.<fork stage>.output.children`;
  - a join that does not depend on its fork stage.
- Validation warns when the child reads `$workflow.<key>` but `input_map` does not supply `<key>`.

## Example: PRD revision loop

See `config/workflows/prd_request.json` (`approve_prd.on_reject`). Rejecting the gate creates `revise_prd` and a new `approve_prd` without hardcoded Python. Delivery stages live in phase skeletons (`jira_from_prd`, `implement_delivery`) and advance via `advance_program` or `continue_delivery`. `deploy_release` remains in the workflow catalog.

## Specialist output presentation (default)

When a workflow task succeeds, the engine presents specialist output back to the bound channel unless opted out:

| Default | Behavior |
|---------|----------|
| Present | Any stage where `agent_id != orchestrator` and `kind != expand` |
| Skip | Orchestrator-owned stages (gates, merge waits) |
| Skip | `expand` stages (plans use their own approval flow) |

Per-stage overrides in skeleton JSON:

| Field | Values | Purpose |
|-------|--------|---------|
| `present_output` | `true` / `false` | Force present or suppress |
| `present_prompt` | `approval` / `inform` / `intake` / `merge` / `none` | Footer CTA after the posted output |

Presentation stores `specialist_handoff` + transcript on the linked session so the orchestrator can reference the latest specialist output on follow-up turns.

## Engine API

- `start_promotion(...)` — channel → durable workflow
- `handle_reject(...)` / `handle_approve(...)` — human gates
- `on_task_transition(task, event)` — runner post-dispatch hooks
- `cancel(...)` / `resume_external(...)` — lifecycle
- `get_status(...)` — aggregate status for tools

Enable with `TS_HARNESS_HOOK_ENGINE=true` (default).

## Memory-api extensions

- `PATCH /workflow/task/{id}/input` — hydrate merges
- `POST /workflow/resume-external` — external wait resume
- `GET /workflows/{id}/children` — fork/join queries
- `POST /workflows/{id}/cancel` — workflow-level cancel

Migration `014_workflow_wait_tokens.sql` adds `wait_token` and `expected_event_type` on `workflow_tasks`.
