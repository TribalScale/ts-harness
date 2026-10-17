# Workflow Builder Runbook

Workflow Builder V1 manages custom workflow definitions in the shared
`ts-harness-workflow-definitions` Docker volume. Built-in definitions still
ship from `config/workflows` and are read-only in the admin API.

Admin Console route: `#/workflows`.

## Supported Nodes

| Palette node | Serialized as | Notes |
|---|---|---|
| Task | `kind: "task"` | Agent work stage |
| Gate | `kind: "task"` + `approval_required: true` | Human approval gate |
| Expand | `kind: "expand"` | Fan-out / plan proposal |
| Compensate | `kind: "compensate"` | Failure compensation stage |
| Wait | `kind: "task"` + `wait_external` | External event wait |
| Barrier | `kind: "task"` + `barrier` | Aggregate child / fan-in results |
| Child workflows | `kind: "task"` + `barrier.hook: "fork_children"` | Start child workflows; optional join stage |
| Fragment | `kind: "fragment"` include | Reusable stage group, expanded when the workflow loads |

## Fragments

Fragments are versioned stage groups that workflows include by `fragment` and
`version`. Manage them in the **Fragments** tab (`#/workflows/fragments`):

- Built-in fragments ship from `config/fragments` and are read-only. The
  built-in `verified_merge` fragment holds the PR checks → QA gate → branch
  freshness → human merge tail used by `deliver_work_order` and
  `implement_jira_ticket`.
- When a built-in fragment gets a new version, the previous file is kept under
  `config/fragments/history/<fragment_id>/<version>.json`. Workflows pinned to
  an older version, including running ones and managed workflows, still
  resolve it. Only the top-level file is listed in the Fragments tab.
- Managed fragments use the same draft/validate/activate flow as workflows and
  live in `TS_HARNESS_FRAGMENT_DEFINITION_STORE`. Activated versions are
  immutable; **New version draft** starts the next patch version.
- A fragment declares `params` (stage references, used as
  `{{param.<name>}}`) and `inputs` (used as whole `$input.<name>` input_map
  values).

In a workflow, **Insert fragment** adds an include node. The inspector offers:

- a version select, with a hint when a newer version is active;
- a stage dropdown for each param;
- an expression field for each input, with required inputs marked.

**Show stages** on the node lists the stages the include expands to.

Validation expands includes first, then reports issues on the include:
`fragment <id>@<version> stage <name>: ...`. Running workflows keep the fragment
version they were started with, because the include pins it.

## Child Workflows

**Child workflows** adds a `fork_children` barrier stage. In the inspector:

1. Pick the child workflow. Its `$workflow.*` inputs pre-fill the intake map.
2. Set **For each** to a list expression for one child per item, or leave it
   empty for a single child. Set **Child label**, for example `$item.id`.
3. Map each intake input, using `$item.<field>` or `$prior.<stage>.output.<field>`.
4. Turn on **Wait for children** to add a `join_<stage>` wait stage on
   `workflow.child_terminal`, with an optional timeout.

The join resumes when every child succeeded. Its output includes
`children_outputs`, keyed by label. It blocks and lists the failed labels when
any child failed. Children use the child version that was current when the
parent started. **Open child definition** jumps to the child in the builder.

Managed definitions may use the full engine schema (`wait_external`,
`compensate`, `barrier`, `join`, `program.next_phase`, and engine
`input_map` prefixes). Multi-phase delivery-program composition is handled by
[Program Builder](program-builder.md) (`#/workflows/programs`), not this
single-skeleton editor.

## Built-In Versus Managed

- Built-in workflows are packaged with the runtime image and act as templates.
  They cannot be overwritten by activation.
- Managed workflows are draft/active/history JSON files in
  `/workflow-definitions`.
- The active managed directory is `/workflow-definitions/active`; the runtime
  and workflow runner both read it through
  `TS_HARNESS_MANAGED_WORKFLOWS_DIR`.
- Resolution order: managed active definition first, then built-in
  `config/workflows`.

Storage layout:

```text
/workflow-definitions/
  drafts/<draft-id>.json
  active/<workflow-type>.json
  active/manifest.json
  history/<workflow-type>/<sha256(version)[:32]>-<content-revision>.json
```

History filenames use a SHA-256 prefix of the exact `definition_version` string
plus the canonical definition content hash. The JSON payload still stores the
original human-readable `definition_version`. Older sanitized legacy filenames
are accepted on read when the payload version matches exactly.

## Catalog Restrictions

The builder can only select server-provided catalogs:

- **Hooks** from the registered hook registry
- **Agents** from runtime config
- **Skills** that are approved/injectable (draft, pending, rejected, blocked,
  and agent-created skills are excluded)
- **Contracts / semantic validators** already known to the runtime

The UI cannot upload executable hook/agent/skill code.

## Stage Tool Restrictions

A task stage can narrow which tools its agent may call with `tool_allow`.

- **Omitted** (the default): the stage inherits the agent's full tool grant.
- **Present**: the list is the stage's whole tool set. Every other tool,
  including other agent tools, is denied with policy id `stage_tool_allow`.
  The one exception is `read_skill_reference`: a restricted stage always keeps
  it so the agent can read its assigned skills' supporting files. It is not
  shown in the inspector checkboxes. The agent's `tool_deny` still removes it.
- The list can only narrow, never widen. Each name must be in the agent's
  effective tools (allow list plus toolsets, including discovered MCP and
  Google Workspace tools) minus its `tool_deny`. Validate reports any other
  name as `unknown_tool`, and an empty list fails to load.
- Agent deny still wins at runtime, and a listed tool still goes through
  `approval_required` when policy says so. A subagent delegated from the
  stage cannot exceed the stage list either.
- Channel chat ignores `tool_allow`; it applies only to workflow dispatch.

In the inspector, tick **Restrict tools** under Skill hints and pick from the
selected agent's tools. Unticking removes the key. Switching agents drops
names the new agent does not have.

A publish step that may only append to a Google Doc:

```json
{
  "name": "publish_blog",
  "kind": "task",
  "agent_id": "pm-agent",
  "depends_on": ["approve_copy"],
  "tool_allow": ["read_file", "google_workspace_docs_append"]
}
```

In a workflow stage the model also sees approval-required tools. Calling one
pauses the stage (`awaiting_tool_approval:<id>`) and adds a request to Inbox
→ Approvals. When the workflow has bound-channel notifications on, the Slack
thread also gets a message naming the step and the tool. Reply `approve` or
`reject` in that thread, or decide it in Inbox. Either one needs the
`approve_gate` permission, and only the first decision counts. Inbox shows
"Already decided" if Slack got there first. The decision resumes the stage
without spending a retry: an approved call runs once with the approved
arguments, and a rejected one is reported to the agent so it can finish the
stage. Channel chat, cron and delegated subagents still see only tools that
policy allows outright.

### Publish warnings

Validate also warns when a stage can publish before a person has approved
anything:

- `ungated_publish_stage`: the stage can call a publish tool and no upstream
  stage is a gate. Publish tools are `channel_send`, Gmail send, Calendar
  create/update/cancel, and every `mcp-` tool, minus the tools on
  `approval_required_tools`. Docs create/append are not publish tools.
- `unrestricted_mcp_before_gate`: the stage has no `tool_allow`, its agent has
  MCP tools, and no upstream stage is a gate.

A gate is an upstream stage (through `depends_on`, at any depth) with
`approval_required: true`, or an `expand` stage with `plan_approval_required`.
The stage's own `approval_required` does not count. With `tool_allow`, only
that list is checked; without it, the agent's full grant is.

These are warnings here, so existing workflows still activate. The
[program builder agent](program-builder-agent.md) treats them as errors.

## Activation And Immutability

Activating a draft:

1. Validates the definition against schema and semantic rules.
2. Writes immutable history for the version/content revision.
3. Updates `active/manifest.json` (generation bump) as the reload commit point.
4. Publishes the plain skeleton JSON into `/workflow-definitions/active`.
5. Deletes the draft envelope.

If runtime reload fails after a successful activation, the API still returns
HTTP 200 with `reload_ok: false` and a warning. The Admin Console surfaces that
warning on the activated workflow view.

A `definition_version` can only be activated once for a workflow type, so edits
require a new version. Running workflows keep their original frozen
`definition_version` and contracts.

Draft updates require `expected_revision`. Stale revisions return HTTP 409.

## Backup And Restore

Back up the named volume before upgrades or risky admin changes. Include
workflows, programs, and managed routing (`routing/`) in the same archive:

```bash
docker run --rm \
  -v ts-harness-workflow-definitions:/workflow-definitions:ro \
  -v "$PWD":/backup \
  alpine tar czf /backup/workflow-definitions.tgz -C / workflow-definitions
```

Restore by stopping the runtime and runner, replacing the volume contents from a
known-good archive, then starting the services again. After restore, use the
admin activate endpoint or restart the runtime if definitions do not reload.
If activate returns `reload_ok: false`, treat it as a failed activate for ops
until reload succeeds on gateway and runner.

## Rollback

Rollback is performed by re-activating a prior history JSON as a new draft with
a new `definition_version`. Do not edit the active file in place; activation is
the audit and reload boundary.

Example:

```bash
# Copy history into a draft via Admin UI Clone/New, bump definition_version,
# validate, then Activate.
```

## Artemis Deploy Notes

Rebuild/recreate services that consume the new volume mounts:

```bash
docker compose \
  -f docker/docker-compose.agent-runtime.yml \
  -f docker/docker-compose.workflow-runner.yml \
  -f docker/docker-compose.admin-ui.yml \
  up -d --build --force-recreate agent-runtime workflow-runner admin-ui
```

Confirm env on runtime/runner:

- `TS_HARNESS_WORKFLOW_DEFINITION_STORE=/workflow-definitions`
- `TS_HARNESS_MANAGED_WORKFLOWS_DIR=/workflow-definitions/active`
- `TS_HARNESS_PROGRAM_DEFINITION_STORE=/workflow-definitions/programs`
  (program definitions nest under the same volume; see
  [program-builder.md](program-builder.md))
- `TS_HARNESS_ROUTING_DEFINITION_STORE=/workflow-definitions/routing`
  (optional; gateway-only overlay — see
  [../architecture/routing-overlay.md](../architecture/routing-overlay.md))

Recreate `memory-api` after `agent-runtime` when they share netns, using both
compose files as in the standard Artemis deploy procedure.

## Acceptance Checklist

1. Open Admin Console → **Workflows**.
2. Clone a built-in template into a new unique `workflow_type`.
3. Create task → gate → task on the canvas (palette also covers expand /
   compensate / wait / barrier).
4. Assign catalogued agents/skills and save the draft.
5. Introduce a dependency cycle and confirm validation fails.
6. Remove the cycle, validate, and activate as `1.0.0` with `reload_ok: true`
   on gateway and runner.
7. Start a test run; confirm the workflow appears in Programs/status.
8. Create and activate `1.1.0`.
9. Confirm the earlier run remains on `1.0.0`.
10. Confirm a new run uses `1.1.0` without restarting containers.

## API Surface

```text
GET    /admin/workflow-definitions
GET    /admin/workflow-definitions/catalog
GET    /admin/workflow-definitions/{workflow_type}
POST   /admin/workflow-definitions/drafts
GET    /admin/workflow-definitions/drafts/{draft_id}
PUT    /admin/workflow-definitions/drafts/{draft_id}
DELETE /admin/workflow-definitions/drafts/{draft_id}
POST   /admin/workflow-definitions/drafts/{draft_id}/validate
POST   /admin/workflow-definitions/drafts/{draft_id}/activate
POST   /admin/workflow-definitions/{workflow_type}/start
GET    /admin/workflow-fragments
GET    /admin/workflow-fragments/{fragment_id}?version=
POST   /admin/workflow-fragments/drafts
GET    /admin/workflow-fragments/drafts/{draft_id}
PUT    /admin/workflow-fragments/drafts/{draft_id}
DELETE /admin/workflow-fragments/drafts/{draft_id}
POST   /admin/workflow-fragments/drafts/{draft_id}/validate
POST   /admin/workflow-fragments/drafts/{draft_id}/activate
```

All mutation routes require admin authorization and origin validation.
Audit events cover draft create/update/delete, validate, activate, and start.

## Scope Limits

Out of scope for the workflow builder:

- Multi-phase delivery-program topology (use
  [Program Builder](program-builder.md); live topology is data-defined, not
  hardcoded `PHASE_*` constants)
- Custom hook / adapter / semantic-validator implementations from the UI
- Live mutation of in-flight workflow definitions
- `fork_subworkflow` authoring (retired; validation points to `fork_children`)
- Fragments that include other fragments (one level only)
