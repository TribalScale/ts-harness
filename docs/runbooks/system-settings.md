# System Settings

Operators edit tenant Jira defaults, identity maps, and Slack start intents in
Admin Console **System → Settings**. Settings writes **managed overlays**. It
never writes secrets or rendered `ts-harness-runtime.yaml`.

## Hashes

| Route | Tab |
|---|---|
| `#/system/settings` | Jira defaults |
| `#/system/settings/identity` | Identity |
| `#/system/settings/routing` | Routing |
| `#/system/settings/policy` | Policy |
| `#/system/settings/sandbox` | Sandbox |
| `#/system/settings/channels` | Channels |
| `#/system/settings/toolsets` | Toolsets |
| `#/system/settings/connector` | Google connector |
| `#/system/settings/advanced` | Advanced (redacted YAML, read-only) |

Unknown inner sections fall back to Jira defaults. Unsaved local edits confirm
before leaving the page. Persisted drafts are not discarded.

## Overlay paths

YAML is bootstrap only. Live config is YAML baseline, then the runtime-settings
overlay (replace a key if present; omitted keys inherit YAML), then the routing
overlay (wins on intent `id`).

```text
{TS_HARNESS_RUNTIME_SETTINGS_STORE}/   # default: /workflow-definitions/runtime-settings
  drafts/<uuid>.json
  active/runtime_settings.json
  active/manifest.json
  history/runtime_settings/<sha>-<revision>.json

{TS_HARNESS_ROUTING_DEFINITION_STORE}/   # default: /workflow-definitions/routing
  drafts/<uuid>.json
  active/channel_routing.json
  active/manifest.json
  history/channel_routing/<sha>-<revision>.json
```

Document shape for runtime-settings:

```json
{
  "definition_type": "runtime_settings",
  "definition_version": "1.0.0",
  "program_defaults": {
    "repo_picker": { "repos": [{ "id": "org/repo", "default_branch": "dev" }] },
    "jira_filing": { "default_issue_type": "Story", "issue_type_by_project": {} },
    "ticket_picker": { "assignee": "mapped_user", "statuses": ["To Do"] }
  },
  "identity": {
    "slack_user_emails": { "U01234567": "name@company.com" },
    "jira_account_ids": { "name@company.com": "accountId" }
  }
}
```

`Reset to inherited` removes that key from the overlay draft so YAML wins again.
An omitted child stays on the YAML baseline. A present child replaces that child.
Policy, sandbox, channel bindings, toolsets, and the Google connector use the
same rule. Connector secret fields stay secret names. Pasting a key, a PEM
block, or a JSON credential fails validation.

Toolset membership is shared. A toolset edit applies to every agent that has
that toolset. Those tools are still added on top of a non-empty per-agent allow
list. Deny is how one agent loses a tool that its toolset grants. An empty
`mcp` or `google-workspace` list means every discovered tool.

Sandbox mode and backend stay `docker`. Timeout, egress, CPU, and memory from
this screen are sent to the sandbox worker on each call. Gateway tokens and the
identity provider stay in the install file. Activate never writes that file.

## Delivery keys

These `program_defaults` keys govern delivery programs. Validation rejects
unknown keys and bad values with the issue code shown.

```json
{
  "program_defaults": {
    "github": { "org": "acme" },
    "jira_filing": { "allowed_projects": ["PROJ"] },
    "governance": { "independent_review": { "enabled": true } },
    "merge_readiness": { "required_checks": { "acme/app": ["build", "test"] } },
    "release": {
      "environments": {
        "acme/app": [
          { "name": "staging", "trigger": { "kind": "workflow_dispatch", "workflow": "deploy.yml", "ref_input": "sha" } },
          { "name": "production", "trigger": { "kind": "deployment" }, "required_roles": ["release_manager"] }
        ]
      }
    },
    "delivery": { "max_parallel_work_orders": 3 },
    "compensation": { "jira": "label", "jira_close_transition": "Done", "delete_branches": false }
  },
  "identity": {
    "github_logins": { "name@company.com": "octocat" },
    "roles": { "name@company.com": ["approver", "release_manager"] }
  }
}
```

| Key | Default | Effect |
|---|---|---|
| `github.org`, `repo_picker.repos` | none | Tenant GitHub binding. Coding runs and PRs outside the bound org or repo list are refused. |
| `jira_filing.allowed_projects` | none | Tenant Jira binding. Jira writes to other projects are refused. |
| `governance.independent_review.enabled` | `true` | Publishes the required status `harness/independent-review`: the PR needs an approving review from someone other than the PRD approver. Only a tenant admin can change it, the Activate call must carry `governance_reason`, and the change is audited as `governance_setting_changed`. Each program pins the value at creation, so turning it off never affects running programs. |
| `merge_readiness.required_checks` | every reported check | Per-repo check names that must pass on the PR head SHA before the merge wait arms (`invalid_map` when not keyed by `owner/repo`). |
| `release.environments` | `staging`, `production` via the Deployments API | Per repo, exactly two environments: pre-production, then production (`invalid_environments`). `workflow_dispatch` runs the named workflow on the base branch with the SHA as `ref_input`; `deployment` creates a GitHub deployment for the SHA. TS Harness never touches infrastructure. |
| `delivery.max_parallel_work_orders` | `3` | Size of each work-order wave, 1 to 10 (`invalid_max_parallel`). `1` delivers work orders one at a time. |
| `compensation.jira` | `label` | On program cancel, Jira issues get the `harness-cancelled` label and a comment. `close` also applies `jira_close_transition` (`invalid_compensation_jira`). |
| `compensation.delete_branches` | `false` | On cancel, delete branches TS Harness created whose PR is closed and not merged (`invalid_compensation_branches`). Open PRs are always commented on and closed; merged PRs are left alone. |

Approval gates can also carry `approval: { required_roles, expires_after_seconds,
escalate_to_role }` in their skeleton stage. Roles come from `identity.roles`.

## Activate (apply then persist)

Settings **Activate** is one page-level cutover. Do **not** call the two
single-store activate endpoints in sequence.

1. Validate settings schema and routing overlay (errors block; signal collisions
   are warnings).
2. Apply overlays in-process from YAML baselines.
3. Persist dirty actives only if apply succeeds.
4. If apply fails: keep last-known-good in memory, do not swap actives,
   `reload_ok: false`.

```text
GET    /admin/runtime-settings
POST   /admin/runtime-settings/drafts
GET    /admin/runtime-settings/drafts/{draft_id}
PUT    /admin/runtime-settings/drafts/{draft_id}
POST   /admin/runtime-settings/drafts/{draft_id}/validate
POST   /admin/runtime-settings/drafts/{draft_id}/activate
POST   /admin/runtime-settings/activate
```

`POST /admin/runtime-settings/drafts/{draft_id}/activate` activates
runtime-settings only. `POST /admin/runtime-settings/activate` is the Settings
page button and accepts optional `runtime_settings_draft_id`, `routing_draft_id`,
and each `expected_revision`.

Routing CRUD stays on `/admin/routing-definitions/*`. See
[`../architecture/routing-overlay.md`](../architecture/routing-overlay.md).

Advanced shows redacted draft-merge or active-live YAML (`***` for tokens and
resolved secrets). There is no apply-YAML control.

## Two Activates

| Surface | What it persists | When to use |
|---|---|---|
| **System → Settings → Activate** | Dirty runtime-settings and/or routing overlays | Tenant defaults, identity maps, Slack intents |
| **Program Builder → Activate** | That program definition only | Topology, node bindings, per-program `config.mode` |

In-flight programs stay on their pinned snapshot. New starts read
`resolved_config` after the settings overlay is applied.

## Program inherit

Each of `repo_picker`, `jira_filing`, and `ticket_picker` may set
`mode: inherit` or `mode: custom`.

| Saved program config | Resolved value |
|---|---|
| `mode: inherit`, key omitted, or `repos: []` with no mode | Tenant Settings / YAML `program_defaults` |
| `mode: custom` with a non-empty value | Program value only |
| `mode: custom` and `repos: []` | Fail closed for this program only |
| Inherit and tenant list empty | Fail closed |

Missing `mode` plus a non-empty `repos` list stays custom so existing overrides
keep working. Built-in `content_to_jira_work_orders` keeps `repos: []` and now
inherits the tenant list. Edit that list in Settings, not the built-in JSON.

Program Inspector (drafts): **Use tenant defaults** vs **Custom** on each
section. Built-ins are read-only; clone to change inherit vs custom. Slack start
intents are edited in Settings → Routing.

## Env

- `TS_HARNESS_RUNTIME_SETTINGS_STORE` — optional; defaults to
  `{TS_HARNESS_WORKFLOW_DEFINITION_STORE}/runtime-settings`
- `TS_HARNESS_ROUTING_DEFINITION_STORE` — optional; defaults beside the
  workflow store at `routing/`

Backup the shared workflow-definitions volume including `runtime-settings/` and
`routing/`. Container recreate is not required after a successful Settings
Activate.
