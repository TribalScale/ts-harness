# Program Builder Runbook

Program Builder manages delivery-program topology definitions that compose
workflow skeletons into a DAG. Built-in definitions ship from `config/programs`
and are read-only templates. Managed drafts, active versions, and history live
under the shared `ts-harness-workflow-definitions` volume at
`/workflow-definitions/programs`.

Admin Console routes:

- Studio list / library: `#/workflows` → **Programs** tab
- New draft: `#/workflows/programs/new`
- Draft editor: `#/workflows/programs/drafts/<draft-id>`
- Active / built-in view: `#/workflows/programs/<program_type>`

Runtime **Programs** (`#/programs`) remains the instance/run view for in-flight
delivery programs. See also [`workflow-builder.md`](workflow-builder.md) for
per-skeleton authoring.

## Supported Graph Elements

| Element | Role |
|---|---|
| Node | Phase that materializes a workflow type (default + optional variants) |
| Edge | Manual or automatic transition with optional confirm, signals, artifact patch |
| Startable nodes | Entry points for `start_program` / mid-pipeline entry |
| Input bindings | Seed workflow input from artifacts, transcript, or static values |
| Completion adapter | Named extractor that patches program artifacts after the phase |

Variants select an alternate workflow type when artifact predicates match
(for example `planning_track=work_orders` → `implement_work_orders`).

## Built-In Versus Managed

- Built-in programs are packaged with the runtime image (`config/programs/*.json`)
  and act as templates. They cannot be overwritten by activation.
- Managed programs are draft/active/history JSON files under
  `/workflow-definitions/programs`.
- Resolution order: managed active definition first, then built-in
  `config/programs`.
- The default for callers that omit `program_type` is built-in
  `software_delivery`.

Storage layout (namespaced peer of workflow definitions):

```text
/workflow-definitions/
  drafts/...
  active/...
  history/...
  programs/
    drafts/<draft-id>.json
    active/<program-type>.json
    active/manifest.json
    history/<program-type>/<sha256(version)[:32]>-<content-revision>.json
```

History filenames use a SHA-256 prefix of the exact `definition_version` string
plus the canonical definition content hash. The JSON payload still stores the
original human-readable `definition_version`.

## Catalog Restrictions

The builder can only select server-provided catalogs:

- **Workflow types** from the workflow catalog (built-in + managed active)
- **Completion adapters** registered in the runtime adapter registry
- **Transition modes** `manual` and `automatic`

The UI cannot upload executable adapter or workflow code. Referenced workflow
types must already validate as known skeletons.

## Jira defaults inherit

Program Inspector **Jira defaults** has three toggles (repo allowlist, Jira
filing, ticket picker): **Use tenant defaults** writes `{ mode: "inherit" }`;
**Custom** writes `mode: custom` plus that program's rows. Tenant lists live in
[System Settings](system-settings.md), not on this page. Built-in maps are
read-only; clone to change inherit vs custom. Slack start intents are edited in
`#/system/settings/routing`. Settings Activate and Program Activate are
separate cutovers.

## Activation And Immutability

Activating a draft:

1. Validates schema and semantic rules (graph connectivity, known workflows,
   adapters, variant predicates, startable nodes).
2. Writes immutable history for the version/content revision.
3. Updates `programs/active/manifest.json` (generation bump) as the reload
   commit point.
4. Publishes the plain program JSON into `/workflow-definitions/programs/active`.
5. Deletes the draft envelope.

If runtime reload fails after a successful activation, the API still returns
HTTP 200 with `reload_ok: false` and a warning. The Admin Console surfaces that
warning on the activated program view.

A `definition_version` can only be activated once for a program type, so edits
require a new version. Running program instances keep their original frozen
snapshot and workflow-version pins (see Pinning below).

Draft updates require `expected_revision`. Stale revisions return HTTP 409.

## Instance Pinning

When a delivery program starts, the runtime persists atomically:

- `program_type`
- `program_definition_version` and content revision
- Canonical `program_definition_snapshot`
- `resolved_workflow_versions` for every node and variant

Later advances and status rendering read the instance snapshot, not the latest
active definition. Activating a new program or workflow definition affects
**new** program instances only.

Schema columns are added by migration
`022_program_definition_pinning.sql` (via `scripts/ops/migrate-db.sh`).

Pre-migration rows with a null snapshot continue through
`FROZEN_LEGACY_COMPATIBILITY_TOPOLOGY`. See [`../compatibility.md`](../compatibility.md).

## Backup And Restore

Back up the shared volume before upgrades. The archive must include workflows,
programs, managed routing overlays (`routing/`), **and** runtime-settings
(`runtime-settings/`):

```bash
docker run --rm \
  -v ts-harness-workflow-definitions:/workflow-definitions:ro \
  -v "$PWD":/backup \
  alpine tar czf /backup/workflow-definitions.tgz -C / workflow-definitions
```

Layout covered by that archive:

```text
/workflow-definitions/
  drafts/...
  active/...
  history/...
  programs/...
  routing/
    drafts/<draft-id>.json
    active/channel_routing.json
    active/manifest.json
    history/channel_routing/...
  runtime-settings/
    drafts/<draft-id>.json
    active/runtime_settings.json
    active/manifest.json
    history/runtime_settings/...
```

Admin-created skills (drafts and approved uploads) live in a separate volume,
`ts-harness-skills-managed`, mounted at `/skills-managed`. Back it up the
same way:

```bash
docker run --rm \
  -v ts-harness-skills-managed:/skills-managed:ro \
  -v "$PWD":/backup \
  alpine tar czf /backup/skills-managed.tgz -C / skills-managed
```

Restore by stopping the runtime and runner, replacing the volume contents from a
known-good archive, then starting the services again. After restore, use the
admin activate endpoint or restart the runtime if definitions do not reload.
Treat `reload_ok: false` on activate as a **failed activate for ops** — the
draft may be committed on disk, but live routing/program reload did not apply;
fix the warning before Slack E2E.

## Rollback

Rollback is performed by re-activating a prior history JSON as a new draft with
a new `definition_version`. Do not edit the active file in place; activation is
the audit and reload boundary.

## Artemis Deploy Notes

Rebuild/recreate services that consume the shared definition volume:

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
- `TS_HARNESS_ROUTING_DEFINITION_STORE=/workflow-definitions/routing`
  (optional override; defaults beside the workflow store)
- `TS_HARNESS_RUNTIME_SETTINGS_STORE=/workflow-definitions/runtime-settings`
  (optional override; defaults beside the workflow store)

Apply migrations (including `022_program_definition_pinning.sql`) before relying
on pinned snapshots. Recreate `memory-api` after `agent-runtime` when they share
netns, using both compose files as in the standard Artemis deploy procedure.

## Acceptance Checklist

1. Open Admin Console → **Workflows** → **Programs**.
2. Confirm built-in `software_delivery` renders the expected DAG
   (`prd` → `jira` | `work_orders` → `implement`).
3. Clone into a new unique `program_type`.
4. Edit a node workflow type or edge signal; save the draft.
5. Introduce a cycle or unknown workflow reference and confirm validation fails.
6. Fix issues, validate, and activate as `1.0.0`.
7. Start a test program; confirm instance status shows pinned type/version.
8. Activate `1.1.0` of the managed program.
9. Confirm the earlier run remains on `1.0.0` snapshot pins.
10. Confirm a new run uses `1.1.0` without restarting containers (or after
    fail-closed reload if history is missing).

## Clone Software Delivery (Slack Proof)

Goal: prove builders can author the same class of E2E as built-in software
delivery without editing repo JSON.

1. In Workflow Builder, clone each SD skeleton to a **unique** managed
   `workflow_type` (e.g. `prd_request_ui`). Activate with `reload_ok: true` on
   **gateway and runner**.
2. In Program Builder, clone `software_delivery` to a unique `program_type`.
   Keep the five `extract_*` completion adapters on the clone (do not switch the
   clone to `extract_declared_outputs`). Set node `reentry` /
   `complete_channel_text` as needed.
3. Register Slack start intents in **System → Settings → Routing**
   (`#/system/settings/routing`) with **distinct** signals that do **not**
   collide with YAML `governed_prd` (avoid reusing `"create a prd"`). Example
   clone phrases: `"start ui delivery"`, `"create ui prd"`.
4. Use Settings **Activate** (not Program Builder Activate) so routing reloads
   with `reload_ok: true` on the gateway (runner does not load routing). Overlay
   wins on intent `id`; see
   [`../architecture/routing-overlay.md`](../architecture/routing-overlay.md)
   and [`system-settings.md`](system-settings.md). Program Activate persists
   topology and per-program `config.mode` only.
5. From Slack, run the clone path through intake → draft → approve → advance.
6. Confirm implement re-entry still works when the running workflow type is the
   clone name and node `reentry` is configured.

Treat any activate response with `reload_ok: false` as failed for live proof.

## Palette-Authored RFP Vertical

Fixtures under `tests/fixtures/rfp_review.palette.json` and
`rfp_response.program.json` show the expected shape:

1. Author `rfp_review` in Workflow Builder (intake → wait → draft → approve) using
   skills `clarify-rfp` / `draft-rfp` on `sales-agent`. Prefer
   `present_prompt: approval` over `write_file`.
2. Author `rfp_response` program with `completion_adapter: extract_declared_outputs`
   and `produces_artifacts: ["approved_rfp"]`.
3. Register overlay intent signals such as `"create rfp"` / `"draft rfp"` with
   `program_type: rfp_response` and `entry_phase: rfp` in Settings → Routing.
4. Activate workflow and program in their builders; activate routing (and any
   tenant defaults) from Settings with `reload_ok: true`, then Slack-run
   intake → draft → approve → complete.

The same console path for a marketing brief is
[`content-campaign.md`](content-campaign.md).

## API Surface

```text
GET    /admin/program-definitions
GET    /admin/program-definitions/catalog
GET    /admin/program-definitions/{program_type}
POST   /admin/program-definitions/drafts
GET    /admin/program-definitions/drafts/{draft_id}
PUT    /admin/program-definitions/drafts/{draft_id}
DELETE /admin/program-definitions/drafts/{draft_id}
POST   /admin/program-definitions/drafts/{draft_id}/validate
POST   /admin/program-definitions/drafts/{draft_id}/activate
POST   /admin/program-definitions/{program_type}/start

GET    /admin/routing-definitions
POST   /admin/routing-definitions/drafts
GET    /admin/routing-definitions/drafts/{draft_id}
PUT    /admin/routing-definitions/drafts/{draft_id}
POST   /admin/routing-definitions/drafts/{draft_id}/validate
POST   /admin/routing-definitions/drafts/{draft_id}/activate

GET    /admin/runtime-settings
POST   /admin/runtime-settings/drafts
POST   /admin/runtime-settings/activate
```

All mutation routes require admin authorization and origin validation.
Audit events cover draft create/update/delete, validate, activate, and start.

## Scope Limits

Out of scope for the current builder:

- Authoring new completion-adapter implementations from the UI
- Live mutation of in-flight program snapshots
- Free-form custom code on edges (only catalogued signals / artifact patches)
- Replacing the runtime Programs instance screen (still `#/programs`)
