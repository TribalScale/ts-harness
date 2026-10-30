# Changelog

All notable changes to TS Harness are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) as described in
[`docs/releasing.md`](docs/releasing.md).

## [Unreleased]

First public release.

### Added

- Local quickstart (`scripts/quickstart.sh`): Postgres, agent runtime, memory API,
  sandbox worker, and admin console in Docker with a fake model provider.
- `scripts/quickstart.sh gates` runs the CI release gates against the local stack.
- Secret scanning with gitleaks in CI.
- Governance files: code of conduct, issue and pull request templates,
  CODEOWNERS, Dependabot configuration, and this changelog.
- `deploy-aws.yml` workflow: plans, waits for environment approval, then applies
  the saved plan.
- Installer `plan --plan-file` / `apply --plan-file` and `plan --detailed-exitcode`.
- Optional `terraform_state.lock_table` in the install contract for DynamoDB locking.
- `validate` fails with `TERRAFORM_TOO_OLD` when S3-native locking needs a newer
  Terraform.
- Cloud bootstrap starts the admin console. Phase 12 adds the `admin-ui` service,
  and the Tailscale URL now serves the console, with nginx proxying every
  gateway route.
- Multi-user scale-out ([design](docs/architecture/multi-user-scale-out.md)).
  `deployment_profile = scale_out` turns it on; `single_host` remains the
  default and keeps today's one-host install.
  - Identity: provider-neutral OIDC with presets (Auth0 first), teams from
    identity groups, a people directory, directory sync and SCIM offboarding,
    and programs that are private by default with sharing.
  - Coding jobs: the deliver stage runs as a queued coding job on a local
    Docker worker or, on `scale_out`, an ECS Fargate task in a private VPC.
    Workers egress through a proxy and hold only a job token. A watchdog
    handles lost and overrunning jobs. Projects add worker sizing and private
    package registries (migrations 033 and 034).
  - GitHub: App installation tokens per repository with the PAT as fallback,
    webhooks per host, and a GitHub tab in System (migration 035).
  - Delivery: repositories per project, a repository per work order, and
    `deploy_release` 3.0.0 with one `release_track` per repository, ordered by
    cross-repository dependencies (migration 036).
  - Throughput: batch task claims by lane (fast, agent, coding) with fair
    ordering per requester and team, coding quotas, several workflow runners
    with workflow locks, and two runtime and memory API replicas behind
    PgBouncer (migrations 037 to 039).
  - Cost: an LLM proxy meters every model call per team and program with
    shared rate limits. Team budgets default to 5,000 USD a month, with a Slack
    warning at 80%. At 100% the team's agent and coding work is held until a
    manager (up to 10,000) or an admin raises the budget on the new Budgets
    page (migrations 040 and 041).
  - Monitoring: a scale-out CloudWatch dashboard and alarms for coding jobs
    queued over 15 minutes, lost-job rate and GitHub quota under 10%, with the
    same numbers as Prometheus gauges (migration 042).
  - Shared state ([runbook](docs/runbooks/shared-state.md)):
    `TS_HARNESS_STATE_BACKEND=postgres` keeps workflow definitions, the cron
    file, scheduler and delegation state, audit events and Slack dedup in
    Postgres, and `TS_HARNESS_OBJECT_BACKEND=s3|gcs` keeps managed skills
    (indexed in Postgres, immutable approved versions) and agent workspaces in
    buckets that Terraform creates on AWS and GCP (migration 043). Replicas read
    local mirrors and write under Postgres locks. `scale_out` turns both on;
    `single_host` keeps files on the host. `scripts/ops/migrate-host-state.sh`
    imports, verifies and exports for rollback; existing `scale_out` installs
    should pin both flags to `local` until they import.
- Jira service account ([runbook](docs/runbooks/jira-service-account.md)):
  cancel cleanup and the `atlassian` MCP server authenticate with an OAuth 2.0
  client credential (`jira-client-id`, `jira-client-secret`, optional
  `jira-cloud-id`) through `api.atlassian.com`, and fall back to the email and
  API token. Phase 10 now passes the Jira secrets to the runtime as
  `ATLASSIAN_*`, and the MCP server gets them through `secret:` env references.
- Jira tools on the `atlassian` MCP server: `jira_search`, `jira_get_issue`,
  `jira_get_all_projects`, `jira_create_issue`, `jira_batch_create_issues`,
  `jira_create_issue_link`, `jira_add_comment` and `jira_update_issue`, granted
  through the new `jira-read` (orchestrator, pm-agent, dev-agent, comms-agent)
  and `jira-write` (pm-agent) toolsets. Inside a workflow, Jira writes need a
  stage that declares `jira_write`.
- Jira descriptions and comments accept Markdown (headings, bullet, numbered and
  checkbox lists, fenced code, quotes, tables, bold, inline code and links) and
  are sent as Atlassian Document Format; issues read back as the same Markdown.
- PM skills: `jira-ticket-format` (ticket rules plus story, epic, task and bug
  templates), `prd-drafter`, `prd-review` and `story-writer`. PRDs carry an
  FR index; acceptance criteria live in the Jira stories.

### Changed

- `jira_from_prd` 2.0.0 reviews the PRD, plans the stories, waits for approval of
  the plan (reject with feedback to revise it), then files the epic, stories and
  `Blocks` links in one stage that never retries. The program records the story
  keys. Runs pinned to 1.x keep their version. `software_delivery` is 1.3.0.
- Rejecting a gate that has `resume_after` steps now cancels their pending tasks,
  so only the re-gate's copies remain.
- Bug filing receives the approved bug report, and Bug issues lead with it.

- `atlassian_search` uses `GET /rest/api/3/search/jql`; Atlassian removed
  `/rest/api/3/search`.
- Jira write tools are offered to agents again when `jira_filing.allowed_projects`
  is set; the project check now runs on the call's arguments at dispatch.
- Read-only Jira MCP tools no longer count as publish tools in workflow validation.

- Licensed under the Apache License, Version 2.0.
- Skills are vendored in `skills/`; the `ts-skills-library` dependency and the
  `ts-` skill prefix are gone.
- `AGENT_RUNTIME_URL` is the runtime URL. Environment variables, the console
  command, the webhook signature, and Terraform addresses use TS Harness names.
- The agent-runtime image builds `gh`, gitleaks, and osv-scanner from pinned
  release commits with a current Go toolchain.
- Terraform backends are committed as empty `backend "s3" {}` / `backend "gcs" {}`
  blocks; the installer supplies the location from `terraform_state`. Remove any
  hand-copied `infra/<cloud>/backend.tf` before pulling. The `backend.tf.example`
  files are gone.
- Provider lock files (`infra/aws/.terraform.lock.hcl`, `infra/gcp/.terraform.lock.hcl`)
  are committed.
- The AWS providers refuse credentials for any account other than `aws_account_id`.
- Installer Terraform output streams live instead of printing after each command.
- CI pins Terraform 1.10.5.
- The `jira_to_pr` routing signal matches `implement` followed by a Jira key in
  any project (`implement [a-z][a-z0-9]+-\d+`) instead of one internal project
  prefix.
- The release workflow no longer has a placeholder cosign signing step or the
  `id-token: write` permission it requested. Images are not signed until they
  are published.
- The release job drops the unused `packages: write` permission and gains
  `security-events: write`, which the Trivy SARIF upload needs. Upload failures
  now fail the job instead of being ignored.

### Removed

- The committed `.lkg` last-known-good config copies. The gateway writes them
  at runtime, and `*.lkg` is now ignored.

### Fixed

- Agents could not reach Jira: at startup the runtime passed MCP server `secret:`
  env refs to `mcporter` unresolved, so the literal `secret:ATLASSIAN_CLIENT_ID`
  selected OAuth and every Jira call failed to authenticate. Startup now resolves
  refs the same way an MCP settings rebuild does. The story-writer example no
  longer names a real project key, which the planner copied into plans.
- The Jira story-plan gate posted "I finished jira filing…, but the output has no
  readable content yet" instead of the plan. Channel messages now render
  `plan_markdown` and `review_markdown`.
- With Postgres transcripts, any chat turn in which the agent called a tool
  failed with HTTP 500 ("cannot adapt type 'dict'"), because `tool_calls` was
  passed to the JSONB column without a JSON adapter. Only turns answered
  without tools, such as start confirmations, got through.
- Bug and content intake stopped at their first step. Approving the
  `pick_repo` gate recorded no `repo`, because gates never run an agent and
  nothing sent the gate output, so `gather_context` failed with "resolved no
  repo". Approval now records the repo from the program's allowlist: the one
  the reply or request names, or the only one allowlisted. A reply that names
  one allowlisted repo also counts as approval. Unclear replies on a repo
  gate list the allowed repos, and other gates no longer ask to "approve
  this PRD".
- `jira_create_issue_link` created Blocks links backwards: the issue that must
  land first showed as blocked by the issue waiting on it. Jira's
  `POST /issueLink` gives the outward description to `inwardIssue`, so
  `JiraClient.link_issues` now swaps the keys. Links filed before this fix point
  the wrong way.
- The weekly RDS master password rotation left `memory-api`, sessions and the
  workflow runner unable to open new connections until someone re-ran bootstrap.
  New phase `12a-db-credential-watch.sh` installs a 5-minute cron job that runs
  `sync-compose-db-env.sh` when the live secret differs from the running
  password. The sync script now recreates `workflow-runner`, `llm-proxy` and
  replicas as well, and phases 13 to 16 read `secrets.env` after
  `compose-env.sh` so a re-fetched secret wins over the stale copy.
- An unreadable optional Jira secret (for example `AccessDeniedException` before
  the instance policy lists it) stopped phase 10 without an error message. It now
  warns and leaves that secret unset.
- `jira_search` and `atlassian_search` refuse JQL that is only `ORDER BY` with a
  clear message instead of passing Jira's 400 through.
- The Jira service account runbook's targeted apply now includes the instance's
  secrets read policy.
- Workflow dispatch errors returned a server error instead of a blocked result.
- The embeddings preflight never detected a missing API key.
- Migrations 018 and 024 now run inside a transaction.
- AWS ignored `secret_prefix`: secrets and the SSM runtime-config were always
  named after `project_name`, while the installer printed population commands
  using `secret_prefix`. AWS now uses `secret_prefix` (falling back to
  `project_name`), as GCP does. `validate` fails with `SECRET_PREFIX_CHANGED`
  instead of renaming an existing install's secrets.
- The installer passed a relative `-var-file` path that broke under `-chdir`
  when `--state-dir` was relative.
- Bootstrap stopped after phase 14 and still reported success: the phase list
  was read from stdin, which `docker compose exec` drained. Phases 15, 16, and
  99 now run.
- The agent-runtime image omitted `evals/`, so the `agent_redteam` and
  `agent_output_judge` release gates failed with missing datasets.
- The admin console nginx resolved `agent-runtime` once at startup, so it
  returned errors after that container was recreated.
- Cloud installs never set `TS_HARNESS_SESSION_DB_URL`, so sessions and
  transcripts stayed in JSONL files on the host and Google OAuth token storage
  could not work. Bootstrap now points them at the install's Postgres, and
  `sync-compose-db-env.sh` recreates agent-runtime after a DB password change.
- The agent-runtime image's gitleaks build upgrades
  `github.com/klauspost/compress` from v1.17.11 to v1.18.7, clearing
  CVE-2026-63209 (HIGH), which failed the release-gates Trivy scan.
