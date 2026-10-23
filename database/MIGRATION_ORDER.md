# Database Migration Order

Apply migrations in this strict order via `scripts/ops/migrate-db.sh`:

| Order | File | Notes |
|-------|------|-------|
| 1 | `001_init_memory.sql` | Core memory tables |
| 2 | `002_init_sessions.sql` | Runtime sessions (renamed from duplicate `002_` prefix) |
| 3 | `002_workflow_tasks.sql` | Workflow tasks (legacy number; runs after sessions) |
| 4 | `003_cost_tracking.sql` | Token usage |
| 5 | `004_session_audit.sql` | Session audit log |
| 6 | `005_workflow_engine_readiness.sql` | Workflow engine columns |
| 7 | `006_workflow_runner.sql` | Workflow plans, claim lease |
| 8 | `007_tenant_isolation.sql` | `tenant_id` on all tables |
| 9 | `008_workflows.sql` | First-class workflows table |
| 10 | `009_scim.sql` | SCIM users and groups |
| 11 | `010_workflow_compensation.sql` | Compensation columns |
| 12 | `011_retry_backoff.sql` | Retry backoff |
| 13 | `012_runtime_transcripts.sql` | Runtime transcript store |
| 14 | `013_workflows_source_channel.sql` | Workflow source channel columns |
| 15 | `014_workflow_wait_tokens.sql` | External wait tokens |
| 16 | `015_workflow_superseded.sql` | Superseded workflow status + link column |
| 17 | `016_workflow_wait_audit_actions.sql` | Wait-related audit actions |
| 18 | `017_workflow_lifecycle_audit_actions.sql` | Lifecycle audit actions |
| 19 | `018_google_oauth_credentials.sql` | Google OAuth credential storage |
| 20 | `019_delivery_programs.sql` | Delivery program coordinator tables |
| 21 | `020_delivery_program_audit_actions.sql` | Delivery program audit actions |
| 22 | `021_workflow_program_link_audit_action.sql` | Workflow↔program link audit action |
| 23 | `022_program_definition_pinning.sql` | Program definition snapshot / version pins |
| 24 | `023_program_project_memory.sql` | Program / project memory (existing file; register only) |
| 25 | `024_transcript_fts.sql` | Transcript `content_tsv` GIN index |
| 26 | `025_program_hierarchy.sql` | Parent/root program ids and `program_external_refs` |
| 27 | `026_workflow_decisions.sql` | `workflow_decisions` table and gate/wait deadline columns |
| 28 | `027_program_releases.sql` | `program_releases` per-environment promotions and rollback targets |
| 29 | `028_audit_details_program_index.sql` | Audit `details` expression indexes for evidence export; `program_evidence_exported` action |
| 30 | `029_runner_leader_lease.sql` | Single-row workflow runner leader lease |
| 31 | `030_program_department.sql` | Program department and department-memory promotion audit |
| 32 | `031_teams_directory.sql` | Teams from identity groups, people directory, team memberships |
| 33 | `032_program_access.sql` | Program owner, visibility and sharing grants; access audit actions |
| 34 | `033_coding_jobs.sql` | Queued coding jobs run by worker containers; coding job audit actions |
| 35 | `034_projects_registries.sql` | Projects with team ownership and worker sizing; per-project package registries; project and registry audit actions |
| 36 | `035_github_app.sql` | GitHub App registrations per host, installations, processed webhook deliveries; directory_users.github_login; GitHub audit actions |
| 37 | `036_project_repositories.sql` | Repositories per project (host, owner, name, App installation, default branch, role) |
| 38 | `037_task_lanes.sql` | Task lanes, team and requester per task, batch claims with fair ordering, lease renewal |
| 39 | `038_workflow_locks.sql` | Per-workflow write lock with expiry for runners and webhooks |
| 40 | `039_shared_runtime_state.sql` | Tool approvals, admin rate-limit windows and service leases shared by runtime replicas |
| 41 | `040_llm_usage_budgets.sql` | Metered LLM usage with a monthly rollup, team budget overrides and model rate buckets |

Existing deployments with both `002_*` files already applied: `migrate-db.sh` tracks applied
filenames; do not rename historical files on disk.

`022_program_definition_pinning.sql` was renamed from a temporary dual-`020_*`
filename before Artemis cutover so `schema_migrations` never recorded the old
name. Do not reintroduce `020_program_definition_pinning.sql`.
