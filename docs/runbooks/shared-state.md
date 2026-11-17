# Shared state: Postgres and object storage

By default the runtime keeps its durable state in files on the host: Docker
volumes and bind mounts under the data disk. Two flags move that state off the
host, so runtime replicas can share it and the control plane can later move to
Fargate:

| Flag | Values | Moves |
| --- | --- | --- |
| `TS_HARNESS_STATE_BACKEND` | `local`, `postgres` | Definitions, cron, scheduler and delegation state, audit, Slack dedup |
| `TS_HARNESS_OBJECT_BACKEND` | `local`, `s3`, `gcs` | Managed skill files and agent workspaces |

`s3` and `gcs` need `postgres`, because skill files in a bucket are indexed in
Postgres. `postgres` needs a session database (`TS_HARNESS_SESSION_DB_URL`).
The runtime refuses to start with any other combination.

## What lives where

| State | `local` (default) | Shared |
| --- | --- | --- |
| Workflow, program, fragment, routing and settings definitions | `ts-harness-workflow-definitions` volume | Postgres `shared_files`, namespace `definitions` |
| Cron jobs | `config/runtime-cron-jobs.json` bind mount | Postgres `shared_files`, namespace `cron` |
| Scheduler last runs, delegation counters | `/state/scheduler.json`, `/state/delegation.json` | Postgres `runtime_kv` |
| Audit events | `/state/audit.jsonl` | Postgres `runtime_audit_events` |
| Slack event dedup | process memory | Postgres `channel_event_dedup` |
| Managed skills (active and drafts) | `ts-harness-skills-managed` volume | Index in Postgres `skill_versions`; files in the skills bucket |
| Agent documents (`AGENTS.md`, `MEMORY.md`, `HEARTBEAT.md`) and file-tool output | `/workspace/{agent}` bind mount | Workspaces bucket, `workspaces/{agent}/` |
| Work-order checkouts, sandbox working dirs | `/workspace/{agent}/coding-backend` | Container scratch, `TS_HARNESS_SCRATCH_DIR` (`/var/tmp/ts-harness`) |

Readers keep reading plain files. In shared mode the runtime and workflow
runner point the definition, cron and managed-skill path variables at a
container-local mirror (`TS_HARNESS_MIRROR_DIR`, `/var/lib/ts-harness/mirror`)
and refresh it from Postgres every couple of seconds. Writers take a lock in
Postgres instead of `fcntl`.

Checkouts are not durable in scratch. On `scale_out` with coding jobs on, they
run in Fargate workers. With object storage on and coding jobs off, the runtime
logs a startup warning.

## Defaults

- Terraform: `state_backend` and `object_backend` (AWS blank follows
  `deployment_profile`: `scale_out` gives `postgres` and `s3`; GCP defaults to
  `local`). The values go to the runtime config with the bucket names.
- Install config: `runtime.state_backend` and `runtime.object_backend`. Blank
  keeps the Terraform default.
- Phases: 10a persists the four variables in instance-env, 12 passes them to
  compose, 10b and 15 read them. Unset flags follow the profile in the same way
  (`resolve_storage_backends` in `scripts/phases/lib/common.sh`); `scale_out`
  without both bucket names keeps objects local and prints a warning.

Once a backend is on, the volumes it replaces are mounted read-only for one
release as a safety net: the definitions volume and the cron file with
`postgres`, the managed skills volume with `s3` or `gcs`. `/state` and
`/workspace` stay read-write (sessions fallback, Slack socket lock, explicit
checkout paths).

**Existing `scale_out` installs:** the AWS default flips both flags on the next
`terraform apply`. Pin them to `local` in `terraform.tfvars` (or the install
config) before applying a release that includes this change, then follow the
switch below.

## Switch an existing install

Plan a short maintenance window: writes made between the import and the
restart are not carried over.

1. Deploy this release with the flags off (`local`). Behavior is unchanged.
   Migration 043 runs with the memory API.
2. Create the buckets and IAM. Set `object_backend = "s3"` (GCP: `gcs`) and
   `state_backend = "postgres"` in tfvars, keep the host on the old values by
   not re-running phases yet, and apply. Note the bucket names:

   ```bash
   terraform -chdir=infra/aws output -raw skills_bucket
   terraform -chdir=infra/aws output -raw workspaces_bucket
   ```

3. Pause writes: stop admin edits, and optionally `docker stop` the workflow
   runner.
4. Import, then verify. The wrapper runs inside a one-off container next to
   `agent-runtime`, with its network and volumes:

   ```bash
   sudo bash scripts/ops/migrate-host-state.sh --import \
       --object-backend s3 --skills-bucket <skills> --workspace-bucket <workspaces>
   sudo bash scripts/ops/migrate-host-state.sh --verify \
       --object-backend s3 --skills-bucket <skills> --workspace-bucket <workspaces>
   ```

   Both print a JSON report and exit 1 on any problem. Import never overwrites
   remote data that differs; it reports a conflict. Re-run with `--force` only
   when the host copy should win. `--only definitions,cron,...` limits the
   sections. Closed drafts (rejected or blocked) are not imported.

5. Re-run phases 10a, 10b, 12 and 15
   ([re-run-bootstrap-phase.md](re-run-bootstrap-phase.md)) so instance-env,
   the compose environment and the cron merge pick up the flags. Phase 12
   recreates `agent-runtime` first, then memory-api and the runner. Phase 10b
   uploads rendered workspace templates only where an object is missing.
6. Check:
   - `docker logs agent-runtime` shows the mirror paths and no storage errors.
   - Activate a workflow draft; the runner picks it up.
   - Propose and approve a skill; the catalog lists it after a runtime restart.
   - Edit a cron job in the console.
   - Write a file through an agent; it appears under `workspaces/{agent}/`.
   - Run `--verify` again. Expect differences only for what changed since.

## Roll back

1. Export what Postgres and the buckets hold back into the volumes:

   ```bash
   sudo bash scripts/ops/migrate-host-state.sh --export \
       --object-backend s3 --skills-bucket <skills> --workspace-bucket <workspaces>
   ```

   Export writes the definitions tree (removing files that are gone), the cron
   file, active and draft skills (`drafts/` is made to match the index), the
   key-value state, and every workspace object that is missing or different on
   disk. It never deletes workspace files. Audit events written after the
   switch stay in Postgres; query them with `GET /audit-events` on the memory
   API.

2. Set both flags back to `local`, re-run phases 10a, 12 and 15.

## Backups

- Postgres: RDS automated snapshots and point-in-time recovery cover
  definitions, cron, key-value state, audit and the skill index
  ([db-restore.md](db-restore.md)).
- Buckets: versioning is on for both. Noncurrent versions expire after 90 days
  (skills) and 30 days (workspaces).
- `skills/drafts/` objects expire after 30 days. An open draft older than that
  loses its files: the mirror skips it with a warning, and the proposer has to
  submit it again. Approve or reject drafts within 30 days.
- After a database restore the shared trees can go back in generation. Mirrors
  notice and pull everything; nothing needs to be cleared by hand.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Runtime exits with `StorageConfigError` | The flag combination: `s3` without `postgres`, a missing bucket name, or no session database. |
| Edits on one replica do not show on the other | `docker logs` for `mirror sync failed`; the memory API must be reachable from both. |
| `ConflictError: ... changed on another replica` | Two admins saved at once. Reload and save again. |
| Skill write fails with `timed out waiting for shared lock` | A writer crashed holding the lock; it expires after 60 seconds. |
| `--verify` reports `only in Postgres` | Something was written after the import. Expected after the switch. |
| `migrate-host-state.sh`: script not in the running image | Deploy this release first (step 1). |
