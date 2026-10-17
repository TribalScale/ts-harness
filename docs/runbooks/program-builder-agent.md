# Program builder agent

Build a new program by describing it to `builder-agent`. The agent writes three drafts as you: a workflow, a program, and (optionally) a routing intent. It proposes any skill it needs that does not exist yet. You review the drafts, approve skills in Inbox, and click **Activate all**. The agent never activates anything.

## Who can use it

You need the `edit_config` permission. Every route under `/admin/program-builder/` checks it, and the agent's tools check it again on each call. If your role loses `edit_config` during a session, the agent's next tool call fails.

A builder session belongs to the person who started it. Nobody else can post into it, read its drafts state, or activate it.

## Flow

1. Open **Workflows → Programs** (`#/workflows/programs`) and click **Build with agent**. The page is `#/workflows/programs/build`.
2. Fill in the short intake form: name, purpose, stages and who approves what, channels it posts to, and trigger phrases. All fields are optional. The form is sent with your first message only.
3. Chat with the agent. It reads the live catalogs (agents and their tools, approved skills, hooks, existing workflow and program types, routing intents, and the publish tools that need a gate), then calls `propose_program_bundle`.
4. The **Drafts** pane refreshes every 5 seconds. Each card shows the draft's status, error and warning counts, the issue list, and a link to open it in its normal builder:
   - workflow: `#/workflows/drafts/{id}`
   - program: `#/workflows/programs/drafts/{id}`
   - routing: **System → Settings → Routing** (`#/system/settings/routing`)
5. If the agent proposed a skill, the **Skills** card lists it with a link to **Inbox → Skills** (`#/inbox/skill_draft`). Review and **Approve** it there. The draft records you as its creator.
6. When there are no errors and every skill is approved, click **Activate all**. The page lists each step's result. On success it links to the new program and shows the trigger phrases to start it from a channel.

**Earlier builds** in the toolbar reopens one of your past sessions. **New build** starts a fresh one; each build gets its own session.

## Publish guardrails

Workflow validation has two publish-safety checks. In the normal workflow builder they are **warnings**. In the program builder they are **errors**, so **Activate all** stays disabled until they are fixed. A program built by an agent must put a person in front of anything that sends or publishes.

- `ungated_publish_stage`: a stage can call a publish tool and no upstream stage is a gate.
- `unrestricted_mcp_before_gate`: a stage has no **Restrict tools** list, its agent has MCP tools, and no upstream stage is a gate.

A **gate** is an upstream stage (through `depends_on`, at any depth) that has `approval_required: true`, or an `expand` stage with `plan_approval_required`. A stage's own `approval_required` does not count: that approval happens after the stage has already run.

**Publish tools** are `channel_send`, `google_workspace_gmail_send`, `google_workspace_calendar_create`, `google_workspace_calendar_update`, `google_workspace_calendar_cancel`, and every MCP tool (`mcp-` prefix). A tool on **System → Settings → Policy** → `approval_required_tools` is exempt, because each call already pauses for approval. Google Docs create/append and Drive create are drafting tools and are not on the list.

When a stage has **Restrict tools**, only that list is checked. Without it, the check uses the agent's full tool grant.

## New types only

The builder creates new programs. If the `workflow_type` or `program_type` is already built in or active, `propose_program_bundle` refuses with `type_exists` and writes nothing. Edit existing programs in the normal builders.

Once a bundle is activated, later turns in the same session may keep using its types, and the next proposal starts new drafts.

## Routing drafts

Routing is one shared document. The agent's routing draft is the active routing plus its one intent. If someone else already has a routing draft open, the agent cannot create another: the routing card shows "finish or discard the existing routing draft in Settings". The workflow and program drafts are still written. Finish or discard the other draft, then ask the agent to propose again.

The routing intent is optional. Without it, start the program from **Programs** or add an intent later in Settings.

## Editing drafts by hand

You can open any draft in its normal builder and change it mid-session. On its next proposal the agent gets `revision_conflict` with your current definition and writes nothing for that draft. It then merges its change into your version and proposes again.

If you discard a draft in its builder, its card shows "Discarded. Ask the agent to recreate it." The next proposal creates a new draft.

## Activate all

Activation runs in order: workflow, then program, then routing. Before it starts, the server re-runs the bundle validation and refuses (HTTP 409) if there is any error or any unapproved skill.

Each step uses the revision the page last displayed. If a draft changed since then, that step fails with a revision conflict instead of activating something you have not seen.

Activation stops at the first failed step; later steps show **Not run**. Nothing is rolled back. The button becomes **Retry remaining**, which skips parts that are already active. A routing step that activates on disk but fails to reload into the runtime (`reload_ok: false`) counts as failed.

Each step writes the same audit record as activating in the normal builder (`workflow_definition_activated`, `program_definition_activated`, `routing_definition_activated`), with `source: program_builder`.

## Configuration

The agent is `builder-agent` in `config/ts-harness-runtime.yaml.tmpl`. Its toolsets are `core-read`, `program-builder` (`builder_catalog`, `propose_program_bundle`), and `skill-authoring` (`propose_skill`). It denies `exec`, `write_file`, `patch_file`, and `delegate_task`, and has no MCP tools. Its procedure is the folder skill `skills/program-builder/program-builder/`.

`program_builder.agent_id` selects a different agent for the page.

The builder tools only run for sessions on the builder page (`source: program_builder`). They return an error anywhere else, including Slack and workflow stages.

## Scheduling and looping programs

### Scheduled runs

A **Start program** cron job starts a new run of a program on a schedule. Create one in **System → Cron** (job type **Start program**), or from **Schedule this program** in the program inspector. The job is stored in the cron file like other system jobs:

```json
{
  "name": "weekly-account-digest",
  "kind": "system",
  "handler": "start_program",
  "agent_id": "ops-agent",
  "cron": "0 9 * * 1",
  "timezone": "UTC",
  "program": {
    "program_type": "account_digest",
    "summary": "Weekly account digest",
    "entry_node": "collect",
    "artifacts": {"region": "emea"},
    "notify": {"channel": "C0123", "text": "Weekly digest run started"}
  }
}
```

- `entry_node` defaults to the first startable node. Saving checks that the start artifacts satisfy the entry node.
- Job names must not contain `:`.
- Each run is bound to `cron:<job name>`. A tick is skipped while the newest run for that binding is still open (`running` or `blocked`, including runs waiting for approval). Renaming the job resets this check.
- Manual **Trigger** uses the same check.
- Each run is started by `scheduler` with no model call. The result (`started`, `skipped` with the reason and open program id, or `error`) is in the `system_cron_job` audit record.
- With `notify`, the scheduler posts the start message to the Slack channel and binds the run to that thread. Step updates, approval waits and completion text then post in the thread. If the Slack post fails, the run starts without a thread and the audit record shows `notify.status: failed`.
- Approval gates in a scheduled run appear in **Inbox** like any other run. Replies in the Slack thread go to normal chat, not to the program.
- A run that hangs open blocks its schedule. Cancel it from **Programs**; the skip audit record names it.

### Looping a node

A node with `reentry` in list mode runs once per item of `remaining_list_artifact`:

1. On entry, the first pending item is copied into `next_item_artifact`. Entry fails with "nothing to iterate" when the list is empty.
2. After each pass, the item is added to `completed_list_artifact` and the node runs again with the next item.
3. When the list is done, the node follows its single automatic edge, or the program succeeds.
4. Past `max_iterations` (default 50) the program is cancelled with reason `reentry_max_iterations`, which also clears its Inbox items and frees its schedule.

The software delivery `implement` node keeps its work-order behavior (dependency waves, no default cap).

## Not covered

- Editing or re-versioning existing programs with the agent.
- Builder sessions from Slack or other channels.
- Rolling back a partial activation.
- Folder skills from the agent. `propose_skill` writes single-file skills; upload folder skills from **System → Skills**.

See also [workflow-builder.md](workflow-builder.md) and [content-campaign.md](content-campaign.md).
