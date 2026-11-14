# Changelog

All notable changes to TS Harness are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) as described in
[`docs/releasing.md`](docs/releasing.md).

## [Unreleased]

First public release.

### Added

- Software delivery programs may end after the PRD or the Jira tickets. The
  program is marked succeeded ("Done at PRD" in the console), the thread is
  offered the next steps, and continuing later reopens the same program. Older
  programs stuck at PRD can be closed with `scripts/close-ended-programs.py`
  (dry run by default). See
  [governed delivery flow](docs/runbooks/governed-delivery-flow.md#stopping-after-the-prd-or-jira).
- Intake sources: attach files (console, Slack, Telegram, Discord) or paste
  Google Drive file and folder links when asking for a plan, bug report, PRD or
  RFP. The intake stages read them as text, including images described by the
  stage agent's model. Uploads stream to the intake object store (local, S3 or
  GCS); filing and implementation never see the raw files. See
  [intake sources](docs/runbooks/intake-sources.md).
- Jira to PR can confirm a serial queue. `implement all` and `implement the rest` order this person's assigned tickets, blockers first, and implement them one at a time after each merge. A named key still runs that ticket only.
- Knowledge notes: reviewed `avoid` and `howto` notes at org and project scope.
  Active notes load into every run. Proposals from a finished-program review or
  from `propose_knowledge_note` wait in the Inbox until a person approves them.
  See [knowledge notes](docs/runbooks/knowledge-notes.md).
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
- Workflow definitions choose what a stage shows at its gate. A contract's
  `presentation: {body, title}` names output fields, a stage's `present_body`
  overrides it, and without either the first `*_markdown` string in the
  contract's `output_schema` is used. Validation warns when a path is not a
  string field, and the admin console has a "Show at gate" picker.
  `bug_to_plan` 1.1.0 presents `diagnosis` and `bug_report`.
- Stage `retry.max_turns` (1 to 64) sets how many model turns one task attempt
  may take; without it the agent default of 8 applies.
  `jira_from_prd` 2.2.0 gives `file_jira` 24, enough to file an epic, its
  stories and links in one attempt.
- `$runtime.today` input_map expression (UTC date). `prd_request` 1.4.0 passes
  it to the drafter, which stamps **Last updated** with it.

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

- The installer rejected install configs with a `github_app` section (missing from the
  schema), and mapped `app_id` to `github_app_app_id`, which Terraform ignores. The
  section is now allowed and maps to `github_app_id`.
- S3 object buckets keep SSE-C blocked. Terraform was about to clear the AWS default.
- Terraform did not create a bucket for intake files, so with `object_backend=s3`
  or `gcs` console uploads failed with 503. AWS and GCP now create
  `<project_name>-intake` (encrypted, not versioned, `intake/` deleted after 14 days),
  publish `TS_HARNESS_INTAKE_BUCKET` in runtime-config, and pass it through
  instance-env and compose. The installer warns when the bucket is missing.
- `TS_HARNESS_INTAKE_VISION_MODEL` could only be set by editing the host's
  environment. It is now the `intake_vision_model` Terraform variable (and install
  contract field), stored in runtime-config. Empty still uses the agent's model.
- Reopening a software delivery program that ended at its PRD failed in the
  memory API: the `delivery_program_reopened` audit action was not allowed by
  the audit log check. Migration `045_program_may_end_audit.sql` adds it, and a
  test now checks every audit action the gateway writes against that list.
- Workflow stages hand off through the next stage's agent. `delegate_task` and
  `propose_delegate_task` are denied while a workflow task is running, unless
  that stage lists the tool in `tool_allow`. Approving an intake gate keeps
  `repo_url`, `base_branch`, `work_order_body`, and `jira_issue_key` on the
  gate output, and a branch named in the reply wins. `$workflow.jira_issue_key`
  now resolves for the following stage.
- A failed `run_coding_backend` call is the stage error on its own. An empty
  `pr_url` no longer hides that error behind a schema complaint, and the tool
  audit stores the result.
- An operator start label such as `operator:thd32:ticket` is not a channel.
  Presentation and the bound-channel notification skip it, and the stage stays
  succeeded.
- `review_precheck` keeps the GitHub pull request title on each pull request
  record. QA and code review judge a title rule, including a Jira-key prefix,
  from that field.
- A chat thread whose program had finished (succeeded, failed, or cancelled)
  answered a new request phrase, such as "plan work from this content", as
  plain chat instead of offering a new program. The thread's session still
  pointed at the old program and its last workflow. A message that matches a
  routing rule now unlinks the finished program first (keeping its id as
  `previous_program_id`); status and artifact questions keep it.
- Repo context for bug and content plans ranks files by the request itself.
  The routing phrase that started the flow ("plan work from this content",
  "file this bug"), the repo and branch names, and section labels such as
  `Goal:` no longer pull in unrelated workflow, skill, and test files.
- Bug-to-Jira and content-to-Jira runs no longer need a manual push between
  steps:
  - Filing into Jira starts once the epic choice is confirmed. `file_tickets`
    is no longer a second approval gate that never ran
    (`file_work_orders_as_jira` 1.2.0).
  - The confirmed Jira project and any epic key named in the reply reach the
    filing step.
  - `jira_get_all_projects` reads every page, so projects past the first 50
    are found.
  - Saying "file these tickets" (or another edge signal) after a phase
    finishes asks to continue to the next phase. "yes" advances the program
    and moves the chat to the new workflow.
  - Intake replies that start with "no" but say "ready" now approve. Other
    intake replies are kept as answers instead of being rejected.
  - A content plan's `prd_markdown` becomes the phase's `approved_prd`.
  - A planner pack with a single work order is accepted.
  - Filed Jira keys are read from the filing summary when the agent leaves
    them out of the output.
  - When a program starts or advances on a gate (pick repo, choose epic), the
    chat says what to reply instead of "will run shortly".
- Content-to-plan's codebase comparison could not see the repo: `pm-agent` has
  no GitHub access, so it fell back to Jira. A `gather_context` step now fetches
  the repo tree and the files the content names, and the comparison works from
  them (`content_to_plan` 1.2.0).
- A program that finished its last phase kept `status = running` on its row,
  so lists showed it as running and its chat thread still resolved to it. The
  phase hook now marks the row succeeded through a new
  `POST /programs/{id}/complete` (running or blocked programs only, audited
  as a `delivery_program_patched` status change). A program with unfinished children stays
  open.
- The repo context for bug investigation and content comparison listed the
  first 200 paths alphabetically, so large repos were cut off early (this one
  stopped at `database/`). The tree is now sent whole when it fits in 60,000
  characters. A larger tree is cut at that budget after the paths the request
  names and the paths that best match its words, and `tree_truncated` says so.
  A directory listing covers the rest of the layout, and when nothing is
  named, the closest-matching source files are fetched (`bug_to_plan` and
  `content_to_plan` 1.3.0).
- The planner copied the template's `Repository: owner/name` placeholder into
  work orders. The template no longer carries it, and the planner contract
  rejects the placeholder so the stage retries.
- Session phase sync read the previous phase's workflow after a program
  advance and failed on a version lookup. It now reads the workflow being
  synced. A read-only definitions store logs one warning instead of a traceback.
- Approving the last gate of a program phase did not save that phase's
  outputs, so the next phase started without artifacts such as `bug_report`.
  Gate approval now completes the phase the same way a dispatched task does.
- Posting a specialist draft could block the task after the chat message was
  already sent, when session sync looked up a workflow version that is not on
  disk. Sync uses the task's own pin, falls back to the active definition when
  the workflow record matches it, and a sync failure no longer discards the draft.
- An intake-gate reply that answered the question and ended with "ready" or
  "draft the bug" was sent back as a revision. Those replies approve the gate,
  and the reply text is stored on the gate so the draft stage can read it.
- A later program phase could not find the web chat when the session binding
  had an extra `web:` segment or still pointed at the previous workflow.
  Presentation matches the thread id and the program.
- Clarify steps that listed several questions now post only the first one in
  chat. The skill tells the agent to ask one question and skip facts the
  report already states.

- A stage's `input_map` was applied only when an upstream stage's
  `on_complete: hydrate_input` hook listed it in `downstream`, and was ignored
  otherwise. `bug_to_plan` `clarify` and `content_to_plan` `compare_codebase`
  and `clarify` ran with empty input. The runner now resolves any unset
  `input_map` keys at dispatch; values already on the task win.
- A request that started a program from chat was cut to 240 characters before
  it was stored, so later stages lost file paths and details. Up to 8,000
  characters are kept, both when the intent is classified and when the
  proposal is stored; only the confirmation message is shortened.
- `bug_to_plan` 1.2.0 and `content_to_plan` 1.1.0 pass the request to every
  agent stage (`report` and `content`) through the new
  `$prior.<stage>.input.<field>` expression. Before, `investigate` and
  `compare_codebase` saw only the repo and could not diagnose anything.
- Bug context matched cited paths against only the first 200 files of the
  tree, so a path deeper in a large repo was never fetched. Cited paths are now
  found in the whole tree and listed first.
- Agent-facing skills, agent policies and runbooks named a real Jira project
  (`TFS`) as the place to file; agents copied it. They now refer to the
  tenant's `jira_filing.allowed_projects`, examples use placeholders, and the
  ticket picker's "Reply with a ticket key" hint uses a key from the list.
- Program phases now resolve the target Jira project once, at workflow
  creation: an explicit key, an epic or issue key, a single allowed project
  named in the request, or the only allowed project. The intake task receives
  `jira_project_key` and `jira_allowed_projects`; other stages map them with
  `$input.`. `jira_from_prd` 2.1.0 passes them to `plan_stories` and
  `file_jira`, and definition validation warns when a Jira-writing stage drops
  them.
- Gate messages and stored Markdown artifacts drop a wrapping code fence, a
  one-line lead-in and anything after a stray closing fence, by structure rather
  than phrase lists. A PRD's summary is built from its title, and its
  **Last updated** date is the real date.
- Every channel reply that leaves something awaiting confirmation says how to
  confirm it. Program and bridge proposals use the same fixed message as
  workflow proposals.
- Intake no longer merges a question's trailing hint with the next question.
- Rejecting a gate cancels the rejected gate as well as its pending dependents,
  so it no longer stays pending and the workflow can complete.
- A Google Doc published for a requester with no resolvable email is no longer
  reported as a sharing problem.
- A revise step's `present_prompt` is read from whichever gate revises it, not
  only a stage named `approve_prd`.
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
