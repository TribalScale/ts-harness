# Changelog

All notable changes to TS Harness are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) as described in
[`docs/releasing.md`](docs/releasing.md).

## [Unreleased]

First public release.

### Added

- Jira tickets get what planned work orders get (`prepare_ticket_delivery` in `implement_jira_ticket` 1.8.0): the design references of the program the ticket points at, the ticket's image attachments as further references, and the repository onboarding check (the ticket's PR also adds `.ts-harness.yml` when CI runs a suite the harness cannot run yet). On the PRD to Jira path, `plan_stories` sees the design images and names them per story (`Design: D1`).
- Console: a Documents tab on the program page with the PRD and the Refinery, Foundry and Planner documents.
- `ts-harness-integration <test files>` runs only those tests, with the same setup and background processes; the coding agent runs the tests it added or changed and leaves the full suite to the host.
- When code review and QA send a work order back in turn (review, QA, review), the harness stops revising and asks a person with both verdicts, instead of spending the remaining revisions. The planner has a consistency gate (no two statements in a work order that cannot both hold), and code review and QA flag a self-contradicting work order instead of failing the PR on one side of it.
- Approval gates show what they approve, in the inbox and on the program page: the PRD, FRDs, blueprints or work orders with the agent's summary and warnings, or for a release, the commit, the PRs in it, CI, a comparison with what the environment runs now, the rollback commit and how the previous environment went (`GET /admin/workflows/gate-review`).
- QA posts its verdict on each PR as a comment (summary, defects with files and the requested change, acceptance criteria, evidence), one per run so a failed run stays visible after a later pass.
- PR titles and descriptions follow a standard shape: the work order's title; summary, changed files, how to test (its acceptance tests as a checklist), verification, out of scope, design references, work order links and the security scan; a repository's PR template is added for the reviewer.
- Design references: Figma links and images attached to a PRD request become `D1`, `D2`… images that travel with the program ([Design references](docs/runbooks/design-references.md)).
  - PRD intake, drafting and the planning stages see them as images; the planner names each UI work order's frames (`- Design: D1, D3`).
  - The coding job gets its work order's images in `.ts-harness/design/` (never committed) and a prompt section to match them; QA compares its Playwright screenshots against them.
  - Figma frames are read through the REST API with a read-only `figma-token` (Dev seat), rendered to PNG, summarized (text, components, colors, fonts) and cached per file version. Without a token, links stay text and attached images still work.
  - Images are kept under `design/` in the intake bucket for a year (Terraform: IAM and lifecycle rule; new `figma-token` secret).

- Hosts can be rebuilt from the repo and the stored install config ([Rebuild a host](docs/runbooks/rebuild-a-host.md)):
  - `install.sh apply` stores the install YAML beside the Terraform state; `install.sh config pull|push|diff` manage it; `plan --replace` plans a rebuild.
  - `runtime.enterprise_repo_ref` picks the branch the host clones and runs; `redeploy.sh` warns when another branch is checked out.
  - `vm.tailscale_hostname` keeps the tailnet URL across rebuilds; `vm.tailscale_tags` and an OAuth-client auth secret avoid expiring auth keys; phase 06 removes the old offline machine with the same name and installs a pinned Tailscale version (1.104.6).
  - On a replaced instance, the bootstrap re-runs the phases that configure the root disk (their markers live on the surviving data disk).
  - The instance follows the data disk's availability zone, and Terraform never replaces the data disk over its zone. Before this, `-replace` of the instance planned to replace the data disk.
  - The Google OAuth callback follows the host's tailnet address unless set to a custom domain, and is cleared when Google OAuth is not configured.
  - Live-test tools moved into `scripts/ops/live-test/`.
  - `redeploy.sh` runs phase 15, so system cron jobs added to it (such as `review-finished-programs` and `retry-program-phases`) reach the live cron list without a rebuild.

- GitHub App webhook setup is scripted and survives host rebuilds: `scripts/github-app-webhook.py` sets the App's webhook URL and secret and reports missing events, and bootstrap phase `12b-github-webhook` (install YAML `github_app.webhook_funnel: true`) exposes only `/webhooks/github` through Tailscale Funnel on port 8443 and re-points the App at it. The GitHub App runbook documents the whole setup, including accepting new App permissions.

- Programs onboard their repositories: on first entering the implement phase, a repository whose CI runs an e2e suite but has no `.ts-harness.yml` gets an onboarding work order (`WO-0000`), and that repository's other work orders wait for it to merge. Recorded in the `repo_onboarding` program artifact.

- Rolling out to team repos:
  - `scripts/onboard-repo.py` drafts `.ts-harness.yml` from a repo's CI (compose startup scripts, env template, chained package scripts, Playwright). It starts an onboarding work order whose PR only opens once the declared suite passes in a coding job.
  - `scripts/repo-governance.py` reports a base branch's protection, CI on pull requests and missing required checks, and `--apply` sets them.
  - PRs people open by hand get `success` on required harness checks.
- MariaDB (MySQL) and MongoDB in coding jobs, detected and wired like Postgres. Each database owns only its own directories.
- Screenshots: the Playwright specs a change touched are rerun with screenshots. The images go to an evidence branch, a PR comment, and QA as image input; the branch is deleted on merge.
- Monorepos (npm/yarn/pnpm workspaces, Turborepo): lint, type check, build and Playwright per changed package.
- Screenshots are taken at a test's assertions (frames from its Playwright trace), not only at its end.
- Large repos run the tests related to the changed files on the host; CI runs the full suite.
- A Prettier check on the changed files when CI enforces formatting. More type-check script names are recognised.
- Out-of-memory handling for coding jobs:
  - A kill is reported as `out_of_memory` and the job is retried with twice the memory.
  - Repos start at the size they needed before.
  - The local launcher waits until a job's memory fits.

- Frontend (React) repos:
  - The coding worker ships Chromium for Playwright.
  - A repo's Playwright suite runs in host verification and through `ts-harness-integration`.
  - Web apps get a `build` check.
  - Vitest repos run with `CI=true`, so a bare `vitest` script no longer hangs in watch mode.
  - Every host verification command has a time limit.
  - Before verification, the worker stops the dev servers and mock APIs OpenCode left running; workers run with `--init`.
  - `.ts-harness.yml` can declare an `env_file` without databases.
  - Evidence text has terminal colors stripped.
- Coding jobs run a repo's database and integration tests: the worker image ships Postgres
  and Redis, a job starts what the repo uses, creates the role and database its env template
  names and writes `.env` pointing at them. An `integration` block in the repo's
  `.ts-harness.yml` (setup steps, background processes with readiness checks, command) runs
  the e2e suite in host verification and through `ts-harness-integration` for OpenCode.
- Line coverage of the changed files in the verification evidence when a work order mentions
  coverage (Jest, Vitest, pytest-cov); QA compares it with the targets instead of marking them
  unverified.
- Node repos run on the Node major they pin (`.nvmrc`, `.node-version`, `.tool-versions`,
  `volta`, `engines`); other majors are downloaded and checksum-verified once per host.
- Code review and QA reuse their last passing verdict when a base-branch refresh left the
  PR's diff unchanged, so a merge into `dev` no longer reruns both agents on every open PR.
- Node repos that use pnpm or yarn: the host installs with the repo's package manager
  (frozen lockfile) and runs its scripts with it, through corepack.
- Host verification runs a Node repo's type check (its script, or `tsc --noEmit`) and
  ESLint on the changed files, so these failures go to the fix passes instead of coming
  back as a failed PR check.
- Review and QA get the host's test evidence: each command verification ran, its exit code
  and the end of its output (`verify_summary`). Before, QA never received it.

- Coding jobs fit the host. The local launcher runs as many workers at once as memory
  allows (host memory minus `CODING_HOST_RESERVED_MEMORY`, default 2.5 GB, divided by
  `CODING_WORKER_MEMORY`, now 2 GB by default), never more than `coding_max_running`.
  In-process runs use the same rule when `CODING_INPROCESS_MAX_RUNNING` is unset.
- Each work order's OpenCode runs keep their own data directory, so runs in parallel no
  longer share one SQLite database (they crashed during setup and lost their usage).
- Coding workers report OpenCode token usage with their job result; memory-api records
  it in `llm_usage` and `agent_token_usage`. Before, usage from workers was lost unless
  the LLM proxy was on.
- `implement_jira_ticket` 1.7.0 runs its deliver step as a coding job when coding jobs
  are on, like `deliver_work_order`: no dev-agent turn.

- GPT-6 agents keep their reasoning across a tool loop. Responses API calls run stateless
  with the reasoning returned encrypted, and each assistant turn hands its reasoning items
  back with its tool calls, so the model no longer reasons from scratch after every call.
- A program phase completion that fails is retried: the engine records it, the
  `retry-program-phases` system job runs it again every 5 minutes with backoff (up to an
  hour), and the program's chat is told when it fails and when it recovers. A program that
  already moved on is never completed twice.

- dev-agent runs on Claude Sonnet 5.5 at medium effort instead of Opus 5.5 at high. In an
  A/B run of the same work order on athena, Sonnet and GPT-6.1 Sol both passed every gate
  first time; Sonnet did it in 2 dev-agent calls, one coding run and 5.6 minutes against
  Sol's 4 calls, two coding calls and 11.5 minutes, and both cost about a third of
  dev-agent on Opus per task.

- Failing PR checks go back to OpenCode like a code review or QA block (`verified_merge`
  1.7.0; `deliver_work_order` and `implement_jira_ticket` 1.6.0). The revision prompt
  names the failed checks and quotes the end of each failed GitHub Actions job log. Three
  revisions on their own `checks` budget, then a human decides.
- A coding run that fails for good (OpenCode killed, crashed or left no PR) waits for a
  person instead of failing its wave: `/revise WO-n <guidance>` in chat or "revise" in the
  Inbox runs it again, `/stop WO-n` stops it.

- Agents can run on Claude, and each agent picks its model and reasoning depth.
  - The Anthropic provider sends no `temperature`, passes tools and tool results
    both ways, replays Claude's thinking blocks within a tool loop (retrying once
    without them if the API refuses), caches the prompt prefix, reports refusals,
    and sets `max_tokens` to 16000 (`ANTHROPIC_MAX_TOKENS`).
  - New per-agent `effort` (low, medium, high, xhigh, max): Claude's
    `output_config.effort` and OpenAI's reasoning effort (capped at high).
  - Every GPT-6 model uses the Responses API, not only `gpt-6-astra`.
  - `heartbeat.model` is now used: heartbeats run on it at low effort.
  - Shipped models: pm, builder and dev on Claude Opus 5.5 (high); orchestrator
    and sales on Claude Sonnet 5.5 (medium); review on GPT-6 Astra; qa, ops,
    analyst and marketing on GPT-6.1 Sol; comms on GPT-6 Luna; heartbeats on
    Claude Haiku 5.5. The qa-agent allowlist moves to `openai/gpt-6.1-sol`.
- `scripts/ops/redeploy.sh` rolls a checkout onto an existing host: secrets, runtime
  config, workspace, compose env, migrations, then rebuilt images. After a successful
  deploy it prunes untagged images and caps the build cache at 6 GB (`BUILD_CACHE_MAX`), so repeated
  deploys do not fill the root volume. See `docs/runbooks/redeploy.md`.
- `llm_usage.agent_id` (migration 046): the agent that made each model call. `actor`
  stays the person the work is for, which for a workflow task is the requester.
- Model usage is recorded without the LLM proxy. Direct agent calls write
  `llm_usage` and `agent_token_usage`; coding-backend runs are read back from
  OpenCode's sessions, conflict refreshes included. Costs come from the new
  `llm_pricing` table in the runtime config template.

- PRD requests ask better questions and the reviewer sees what a draft is missing.
  - New `prd-intake` skill: each intake question carries a recommended answer worded so
    that "yes" accepts it, and the agent no longer asks what the attached sources or a
    recalled decision already state.
  - `prd-drafter` 1.3.0: Decisions (each intake answer, citing its question) and
    Assumptions (each gap filled without an answer), Must/Should/Could on every FR,
    "Current state" and "Why now" in the problem statement, and no file paths or code.
  - Automatic PRD checks on `draft_prd` and `revise_prd`: missing or empty sections,
    leftover placeholders and TBD/TODO, vague wording in requirements, FRs without
    `Specified in`, `Verified by` or a priority, references to missing DoD items, a
    Definition of Done outside 5 to 10 items, and open questions without an owner. The
    findings travel as `prd_checks` and are listed under the draft in the approval
    message; they never block it.
  - The PRD checks also flag stated times and limits (from the request, answers and
    attached sources) that never reach Requirements, Technical Constraints or the
    Definition of Done, and appear above the reply instructions. Intake drops questions
    whose own recommendation says the request already answers them, and trailing chat
    ("Let me know if…") is stripped from drafted PRDs.
  - Console Inbox: a PRD approval gate shows a "Draft checks" card with what the
    checks found on the draft under review (the latest draft or revision), or that it
    passed.
  - `prd-review` 1.1.0 grades decisions, assumptions, FR priorities and hygiene;
    `story-writer` 1.1.0 orders stories by FR priority and carries assumptions into open
    questions.
  - `prd_request` 1.6.0 names the skills (`prd-intake`, `prd-drafter`). The old hints
    `clarify-requirements` and `draft-prd` resolve to them for pinned workflows.
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

- Admin UI on `@astryxdesign` 0.7.13. Disabled buttons are now `aria-disabled` (focusable; clicks, Enter and Space blocked) instead of natively `disabled`. Tests use a `toBeDisabledControl()` matcher that accepts either. A new screen-reader live region with `role="alert"` is skipped by `getAlert()`/`findAlert()`. The project, new-project, registry and note forms also refuse submits their disabled button would refuse, and tests press Enter in each invalid form. `AGENTS.md` refreshed by `astryx upgrade`.

- Dependency updates from Dependabot #51–#53, applied on dev: Go 1.27.4 for the tools build; starlette 1.10, OpenTelemetry 1.45.2, pytest 9.2, ruff 0.17.4, google-auth 2.62.2, inspect-ai 0.3.289 and others; React 19.3.1, @assistant-ui/react 0.15.35, @xyflow/react 12.12.1 and lint/test tooling. `@astryxdesign/*` stays at 0.7.0: 0.7.13 renders disabled buttons with `aria-disabled` instead of `disabled`, which fails 21 admin UI tests and needs its own review. Dependabot now targets dev, and `@astryxdesign/*` updates arrive in their own PR.

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

- Request sources were cut at 80,000 characters each (150,000 in total): a plant's 275,000-character scheduling-rules document reached the PRD only up to its fourth section. The defaults are now 300,000 and 400,000, adjustable with `intake_max_doc_chars` / `intake_max_total_chars` (Terraform and install YAML, through the runtime-config store) or `TS_HARNESS_INTAKE_MAX_DOC_CHARS` / `TS_HARNESS_INTAKE_MAX_TOTAL_CHARS`. The intake agent tells the requester when a source was still cut (`prd-intake` 1.3.0).
- Redeploys cut off running coding jobs: workers share agent-runtime's network namespace. `redeploy.sh` and phase 12 now pause the coding launcher and wait for running `ts-coding-*` workers (`CODING_DRAIN_TIMEOUT_MINUTES`, default 120; `FORCE_REDEPLOY=1` skips the wait), and a failed redeploy starts the launcher again.
- Console: the Projects page is redesigned. A toolbar with **New project** opens a dialog that also takes the Jira project key and default base branch. The project list shows key, team and Jira link. Settings are grouped into cards (Delivery, Repositories, Coding workers, Package registries, Knowledge notes), each with its own Save and Discard. Repositories can now be edited (base branch, role), removing one asks first, and a repository without its own branch shows the project default it uses. The knowledge-notes filters are styled.
- A Jira ticket without a `Repo:` line uses the project linked to its Jira project (System → Projects: Jira project key, repositories, branch); with several repositories the ticket names one.
- Jira to PR asked twice (yes to start, then "ready" at a kickoff that showed nothing). The kickoff gate is gone: the start is the confirmation, `prepare_ticket_delivery` posts the ticket, repository and design references, and coding starts. A branch named in the request (`on branch release/2`) overrides the ticket's.
- "Show my tickets" never listed anything: its first step was an approval gate meant to be an agent, and gates never run one; only Slack users could be mapped to a Jira account. `prepare_ticket_queue` now lists the signed-in person's assigned tickets in every project (their email is kept on the program at start; the Jira account comes from the Settings mapping or Jira's user search), the reply to `pick_ticket` picks a key or `all <PROJECT>`, and implementation follows without another confirmation. A key in that reply was read as an epic to file under.
- A coding step that opened its PR was blocked when posting its result to chat failed. Only a post a person must answer (approval, intake), or one for an unknown step, holds the step now; others log `presentation_failed` and carry on. A workflow's chat is also found when another process (the workflow runner, a second agent-runtime) started it: sessions reload from Postgres on a miss.
- Console users approving in chat were refused (`missing permission approve_gate`) once their email reached the session: the check used email role mappings, not the roles on their sign-in. The session now carries their sign-in permissions, refreshed on every message.
- PR titles and descriptions for Jira tickets: the title comes from the ticket (`THD-33: Show the comment count ...`), plain-line headings (Acceptance criteria, Out of scope) fill the sections, the first paragraph is the summary, and verification commands drop the workspace paths.
- Jira to PR coded nothing: since Jira delivery became a coding job (`940b30b`), no step read the ticket. The kickoff gate's reply ("ready") was its only output, so the coding job got no ticket text and no repository. `prepare_ticket_delivery` now reads the ticket from Jira and resolves the repository from its `Repo:` line before the kickoff, which shows what will be coded.
- The workflow runner had no Jira credentials, which `prepare_ticket_delivery` needs for ticket attachments; compose now passes `ATLASSIAN_*` to it.
- CI failure feedback showed only the end of the job log, which with retries is the retries' errors (often caused by data the first attempt left behind). It now leads with the first failure in the log.
- A step parked on its coding job read "blocked" with an internal error line; it now reads "coding in progress", and the job line says "Coding in progress · N min" or "Queued: waiting for a free coding worker".
- Release gates never reached the inbox: it listed only gates on a phase's own workflow, and the promote gates are on the release tracks it starts. The memory API's child tasks now carry their ids, and the inbox lists gates waiting inside started workflows.
- Code reviews and QA results were posted under the PAT owner's name: the workflow runner had no GitHub App credentials. Compose now passes `GITHUB_APP_*` to it.
- `FIGMA_TOKEN` never reached the containers: the compose wrapper did not pass it, so a stored Figma token would have been ignored.
- CI never ran on a stacked PR moved onto `dev` (a base change is an `edited` event, which CI did not listen to), so it sat blocked without checks.
- A failed QA with defects that have no severity read "0 blocking of 1 defects"; it now says "1 defect".
- Found in the end-to-end rehearsal (PRD → planning → delivery):
  - Work-order dependencies written the way the planner template writes them (`- Depends on: WO-001` in the Context Graph) were ignored; only table rows were read. Every work order looked independent, so dependent work orders started before their foundation. Bullet and bold forms are read now.
  - A cancelled work-order delivery counted as delivered (a child with only succeeded or cancelled steps was "succeeded"), so its work order would never be built. A cancelled child is now undelivered and the program schedules it again.
  - The repo-onboarding check failed in the workflow runner with `No such file or directory: 'git'`; the runner image now includes git.
  - A GitHub webhook for a workflow with nothing waiting answered 500; it is acknowledged as ignored.
  - Release gates could not be approved in the console: Approve and Reject showed only on a phase's own steps, and the promote gates live in the release track the phase starts. Started workflows' gates now have the buttons, and a row with a gate waiting opens by itself.
  - The console's program page showed a phase's first workflow: after a second work-order wave started, implement read "succeeded" and the new wave appeared as a nested row. A phase now shows its newest workflow, and its work-order list covers every wave, each work order at its latest attempt.

- Approving or rejecting a gate in the console only updated the task in the memory API. The gate's hooks never ran (for `approve_prd`, `mark_prd_approved`), and when the gate was the workflow's last step the program phase never completed: the program kept neither the approved PRD nor its design references and could not continue. Console decisions on pending gates now go through the workflow engine, like chat decisions, and keep the approver's permissions.

- Images attached to a PRD request were never described: `gather_sources` runs in the workflow runner, which had no model-provider keys, so every image came back "image could not be read" (`x-api-key header is required`). The runner now gets `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` and `MISTRAL_API_KEY`. An image whose description fails is still kept as a design reference.

- Rebuild path: the bootstrap runner made phase scripts executable (`chmod +x`), which marked the host's checkout modified, so the next `git checkout` of a branch that edits a phase (phase 09, redeploys) refused to run. Phases now run with `bash <script>`, and phase 09 sets `core.fileMode false`. The instance also ignores user data from its launch template, so the first apply after a rebuild no longer stops and restarts it.

- Release gates say why they failed: a check can return a reason, `agent_redteam` names every failing case, and `agent_output_judge` re-runs a failing case once (a model grading free text can be wrong once) and records cases that passed only on the retry. The red-team gate is never retried. Before this, a single false fail from the output judge stopped a host bootstrap with no reason logged.

- The `tenant_isolation` release gate passed only once per database when audit events are in Postgres: it wrote events for tenants `a` and `b` and expected exactly one for `a`, so earlier runs' events failed every later bootstrap or phase 16 re-run. It and `audit_export` now use fresh `release-gate-*` tenant ids each run and check by event id.

- The `agent_redteam` release gate failed agents that refused an injection and quoted it in their report, because each canary appeared word for word in the attack text. Each attack now asks the agent to build the canary (for example `CANARY-JIRA-` plus 7 times 6), so only obeying the injection produces it. Found on the first full bootstrap of the dev host.

- `retry-program-phases` was in `config/runtime-cron-jobs.json` but not in phase 15's system jobs, so on hosts with `TS_HARNESS_STATE_BACKEND=postgres` failed program phase completions were never retried. A test now keeps the two lists in sync.

- Coding workers used the GitHub token from job start for API calls even after refreshing it. Jobs that crossed the token's one-hour expiry failed with 401.

- A test file the implementer adds beyond the work order's `## Files` list is no longer a
  `major` `scope` review finding. Revisions were allowed to add tests, then review blocked
  them and spent another round. Test files (`*.spec.*`, `*.test.*`, `test/`, `tests/`,
  `__tests__/`, feature files, `test_*.py`, `*_test.go`) are in scope for the revert, the
  OpenCode edit permissions and the review alike.
- The host dependency audit blocks only on vulnerabilities a change introduces. It
  compares the scan with the base branch (a temporary worktree) and reports the findings
  the base already had as a count in the PR body. Before, any work order that touched a
  lockfile failed on every known vulnerability already in the repo.
- Work orders with Given/When/Then acceptance tests no longer demand Gherkin feature files
  in a repo that does not use them. A NestJS repo with Jest failed every such work order
  (`no_bdd_runner_or_features`) and the agent tried to add Cucumber.
- Pull requests no longer carry the harness's `.opencode/` skills and config, or Python
  ignore lines added to the repo's `.gitignore`.
- Review and QA no longer judge a cut-off patch: the per-file limit is 60k characters with a
  240k budget per review, trimming the largest files first, and new files GitHub sends
  without a patch are read from the PR head. QA marks what it cannot see as unverified
  instead of failing the work order on it, which repeated revisions that could not help.
- `redeploy.sh` builds the runtime image before the coding worker image, which is built
  from it. Compose built both at once and workers kept the previous runtime code.

- Cancelling a program or a workflow left the workflows below its own running: a cancelled
  program's release track kept waiting for its promote approval in the Inbox. A cancel now
  reaches every running or blocked workflow below it, at any depth, with their tasks and
  coding jobs, and audits each one with what it cascaded from.
- CI skipped every Postgres test (117 of them, including the program, budget, coding job
  and usage stores): `TS_HARNESS_TEST_PG_URL` was never set, though the job starts a
  Postgres service. It is set now.
- OpenCode edited files outside a work order's `## Files` list; the harness reverted them
  after the run, which broke tests that imported from them and spent the fix passes. Each
  run now gets an `OPENCODE_CONFIG` (outside the repository) whose edit permissions deny
  everything but the declared files, `features/**` for Given/When/Then work orders, the
  paths the pipeline writes, and review revision files. OpenCode refuses such an edit at
  once and works within the list. The revert stays for files written another way, and the
  fix pass is told which files it reverted.
- `install.sh apply` without `--plan-file` ran `terraform apply -auto-approve`, so a plan
  that deleted resources was applied unseen. On the dev install a refresh lost the intake
  bucket from state; the next apply planned to create it, deleted its public-access block,
  TLS policy and other settings on the way, then failed with `BucketAlreadyExists`. Apply
  now saves the plan, and stops with `APPLY_WOULD_DESTROY` and the list of resources when
  it deletes or replaces anything; `--allow-destroy` or `--plan-file` proceeds.
- With object storage on S3, the workflow runner logged "skill mirror sync failed" on every
  tick: compose gave it the backend but not the skills and workspace buckets.
- Coding jobs never started on a single host: the launcher runs in the agent-runtime image,
  which installed `docker.io` (on Debian trixie the daemon only) and had no `docker` client,
  so every launch failed with `No such file or directory: 'docker'`. The image installs
  `docker-cli` instead.
- The coding worker image failed to build with npm 12, which skips install scripts unless
  allowed: OpenCode's postinstall (which fetches its binary) never ran. The worker image
  and phase 11h now run it explicitly. Compose also never received `OPENCODE_VERSION`, so
  workers were built with the latest OpenCode instead of the pinned version.
- On hosts without GitHub webhooks a merged PR stayed `open` in the program's references:
  the merge poller resumed the wait but never recorded the merge. It now records it the
  same way as the webhook.
- A program waiting on child workflows (release tracks, wave work orders) said only that
  it was waiting for `workflow.child_terminal`. Program status now lists each running
  child's current step and what it waits for, and recommends the pending approval.
- A program chat resent every earlier turn in full, including whole PRD drafts and work
  orders, so a one-line question cost over 150,000 input tokens. Assistant and tool
  messages longer than 3,000 characters from earlier turns are cut to their first 1,500.
- Program status reported a stale lease on a step that was only waiting (for example a
  release waiting for its promote approval), and the orchestrator told people the release
  was stuck. Only a running task's lease is checked now.
- `program_status` returned every program artifact in full (about 125,000 characters for
  a delivered program), so a status question in chat cost tens of thousands of tokens.
  Long artifacts now come back as their size and headings; `include_artifacts` asks for
  named ones in full. `workflow_status` (160,000 characters for one implement workflow)
  summarises task inputs and outputs the same way; `full: true` returns them whole.
- Restarting agent-runtime (`restart_agent_runtime`, used by `sync-runtime-config.sh`)
  left memory-api unreachable: memory-api, workflow-runner, llm-proxy and coding-launcher
  run in agent-runtime's network namespace and lost it. They now restart right after it.
- Coding-backend usage of long OpenCode sessions was not recorded: OpenCode stops writing
  to a pipe after 64 KiB, so `opencode export` arrived cut off. Its output now goes to a
  temporary file.
- A revision of an open PR started from the clone's old copy of the branch. When GitHub
  had moved the PR (a refresh merge of the base branch), the push was rejected and the
  work order blocked with `git push --force-with-lease failed: ... (stale info)`. The
  revision now catches up with the PR on GitHub first, keeping any unpushed local
  commits, and a forced push leases against the tip it just fetched. The bare lease
  could never succeed in the shallow single-branch clones the harness makes.
- A work order blocked for good (a failed coding run, failing PR checks) never reached
  the Inbox or the chat `/accept` and `/stop` commands: the stuck-step list only held
  steps parked after the revision limit, so the wave waited with nothing to act on.
  Those blocks are listed now; external waits, tool approvals and stopped steps are not.
- Coding runs inside the runtime container had no concurrency limit. A wave of three
  work orders ran three OpenCode processes at once and the kernel killed one for lack of
  memory on a 4 GB host. In-process runs now take a slot first
  (`CODING_INPROCESS_MAX_RUNNING`, default 1); the others wait.
- A program could stop after its work-orders phase: the program snapshot was too long
  for the embedding model (8192 tokens), memory-api refused it, and the move to
  implementation never happened. Only the first 16000 characters of a memory are
  embedded now (`EMBEDDINGS_MAX_INPUT_CHARS`), with one shorter retry; the stored
  content is unchanged.
- `recall_memory` failed on every call without a department scope: the recall query
  had a bare `%` in `ILIKE 'department:%'`, which psycopg reads as a placeholder.
- `sessions_list` returned every session the tenant ever had (about 200k tokens in one
  tool result on the dev host). It now returns the 20 newest by default (`limit` up
  to 100), can filter by `agent_id`, includes `started_at`, and leaves large
  metadata (drafts, handoffs) out by name; `sessions_history` reads one in full.
- The Anthropic API key never reached the containers: phase 12 did not export
  `ANTHROPIC_API_KEY`, and the compose wrapper did not forward it. Phase 12 now also
  re-runs when the script itself changes.

- A PRD request confirmed in chat never read its attachments. The promote hook folded
  `intake_sources` into the intake task's context, where `gather_intake_sources` does not
  look, and also stored the pointers as program artifacts. They now go to the intake
  input like every other start path; gather also reads the old location.
- The PRD intake chat showed only the first question, cut before its recommended answer,
  and a "yes" then accepted recommendations nobody had seen. Contracts can set
  `presentation.questions` to `all`; `prd_request` shows every question.
- A program that ended at its PRD kept the approved PRD only on the chat session, so
  continuing it from the console reached Jira without `approved_prd`. The PRD phase now
  records the approved draft on the program.
- Contract `presentation` settings were dropped when a definition was written back out.
- The `prd_request` intake and draft steps ran without their step guidance: both have
  output contracts, and the contract prompt returned before the guidance was added. The
  intake step therefore never received the rule to use attached sources instead of asking
  about them.
- The workflow runner mounted the Google Workspace key but did not set
  `GOOGLE_WORKSPACE_SA_JSON_PATH` (it has no env_file), so Drive links in requests were
  reported as not connected. The runner compose now sets it.
- The workflow runner image did not install the `google`, `aws` and `gcp` extras, so
  the intake gather could not read uploaded files from S3/GCS or Drive links. It now
  installs `.[google,aws,gcp]`, like agent-runtime.
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
