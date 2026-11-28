# Changelog

All notable changes to TS Harness are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) as described in
[`docs/releasing.md`](docs/releasing.md).

## [Unreleased]

First public release.

### Added
- The harness files bugs against itself and proposes fixes (the harness-level self-improvement loop). When the stuck-step arbiter labels a step "Harness issue", or an ops alert points at the harness itself (coding jobs lost at the alert threshold, the LLM proxy failing its health check), a `harness-self-issues` system job (every 10 minutes) files a GitHub issue on the harness's own repository (`TS_HARNESS_SELF_REPO=owner/name`; unset means off), deduplicated by a fingerprint of the failure class (source, harness component, normalised error signature), and comments the new occurrence count on a known one (at most every 6 hours). Issues carry harness-side facts only: a sanitiser replaces client repository names with roles (`<web repo>`), ticket keys with `<ticket>` and names with `<name>`, drops code, quoted text, non-harness paths and text copied from work orders, and withholds anything still client-looking. A fixable code bug whose files are not off limits gets one self-fix: a `deliver_work_order` run against the harness repository into `TS_HARNESS_SELF_REPO_BRANCH` (default `dev`), made a draft PR labelled `harness-self-fix`, linked to the issue, with a required regression test; a person merges it. At most 2 self-fix PRs open, 3 a week and one per issue; configuration, operations and off-limits cases are labelled `needs-human`. Self-fixes check and charge the improvement budget and pause when it is used up. `TS_HARNESS_SELF_ISSUES` and `TS_HARNESS_SELF_FIX` switch filing and self-fixes (both default on once the repository is set). The Metrics tab lists the issues and PRs and what an admin still has to configure. New table `harness_self_issues` (migration 057). See `docs/runbooks/self-improvement.md`.

- Self-improvement foundation. Every agent step and coding job records the versions it ran with on its input as `_versions` (workflow and definition version, fragment versions, the step's skill versions, the agent, coding, pre-push review and gate models, the pre-push review variant and any ticket variant set with `set_ticket_variant`); the agent never sees it and tagging never fails a dispatch (`TS_HARNESS_RUN_TAGGING=off` turns it off). The Live → **Metrics** tab adds **Merged without a person** (the share of finished tickets merged with no decision, `/revise`, PR change request, change request, accept or stop by a person; Jira replies to readiness questions count as a person), **Problems after merge** (reverts of the PR on its base branch, Jira bugs linking to or mentioning the ticket, new nightly smoke failures for the repository, within 14 days, plus how often a person overruled the arbiter; GitHub and Jira are read bounded and cached and show "unknown" when they can't be), a weekly autonomy column, a "Person stepped in" column and **Group by** any recorded version (`GET /admin/metrics?group_by=variants.pre_push`). Decisions on stuck steps are now logged on the step (`_decision_log`), including when they overrule the arbiter. Improvement work gets its own monthly model-cost cap, `budgets.improvement_monthly_usd` (default 150 USD), kept in a new `improvement_spend` table (migration 055) and shown on the Metrics tab; at the cap improvement work pauses until next month and delivery is unaffected. `ts_harness.improvement.off_limits.OFF_LIMITS` lists what the loop may never change on its own (security, gate blocking rules, permissions, budgets, the arbiter's limits, the metrics and guard code). See `docs/runbooks/self-improvement.md`.
- Knowledge notes are drafted from runs that failed and were then fixed. Every hour `draft_failure_notes` looks at finished programs for a repeated review or QA finding a later revision fixed, CI that passed after a revision, a stuck step a person's or the arbiter's guidance got through, and a coding run that failed in the work and then succeeded. For each such program one call to the revision diagnosis model writes at most two lessons ("In org/api, X; do Y") as proposed project notes (org only when the lesson holds for any repository), with kind, `applies_to` and links to the run, PR and tasks (`source` `drafted`). Near-duplicates of active or proposed notes are skipped. Nothing is activated: drafts go to the Inbox. At most three model calls per run; each program is drafted from once (`notes_drafted_at`); no diagnosis model means nothing is drafted. See "Drafts from failed runs" in `docs/runbooks/knowledge-notes.md`.
- Hybrid recall for knowledge notes. Notes are ranked against a run's summary and work order by Postgres full-text search and pgvector similarity, fused with reciprocal rank fusion, after the `applies_to` match and before kind and recency, within the same budget. Notes are embedded on create and change with the memory service's embedding provider and backfilled hourly (migration 054 adds `search_tsv`, `embedding`, `embedding_hash`). Without embeddings recall is full-text only, and a recall failure never fails a run. `GET /notes/search` is the explicit search. See "Hybrid recall" in `docs/runbooks/knowledge-notes.md`.
- A **Memory** page in the console (`#/memory`) lists every knowledge note you can read: hybrid search, filters for scope, project, status and kind, a summary per note, and a detail view with the full text, `applies_to`, evidence (including the run a note was drafted from), who proposed and approved it, and its history. Approve, reject, edit and archive follow the existing permission rules. There is no graph view. See `docs/runbooks/admin-console.md`.
- A Jira ticket a person wrote (no pointer to a planning program) is checked before anything is built. The harness gathers its description, comments, parent epic, linked tickets, related PRs and the repository's files, and the diagnosis model writes a work order (requirements, acceptance tests, scope, likely files) with its assumptions marked minor or behaviour-changing. A ticket that is ready, or needs only minor assumptions, is built from that work order, which is posted on the ticket. A ticket that isn't ready, or needs a behaviour or scope assumption confirmed, waits. Its questions go to the ticket, the chat and the Inbox (**Needs product decision**). It resumes when someone replies on the ticket, uses `/retry <answers>` or Retry in the Inbox; "go ahead" accepts the assumptions. Acceptance criteria derivable from the description are enough. Conservative projects confirm any assumption; `TS_HARNESS_TICKET_READINESS=off` turns it off. See "Hand-written tickets: readiness check" in `docs/runbooks/governed-delivery-flow.md`.
- The arbiter now triages every step that stops for a person, not only review/QA disagreements. Every 2 minutes (`arbitrate-stuck-steps`) it reads a stuck step's error, coding job, file changes, review and QA findings and the work order, and classifies it. It retries a cause outside the work and revises with its own guidance when the agent went off track, on its own, at most twice per step. It can override a blocking code-review finding only when every one contradicts the work order or a person's request, or is already fixed, with evidence, and never for security: QA still runs, a person still merges, and the reasoning is posted on the PR. Everything else stays in the Inbox labelled **Harness issue**, **Needs product decision** or **Arbiter handed off**, with the diagnosis and an **Apply recommendation** button. Its actions show on the ticket timeline. Conservative projects get recommendations only; `TS_HARNESS_STUCK_ARBITER=off` turns it off. See "Stuck steps: the arbiter" in `docs/runbooks/governed-delivery-flow.md`.
- Merge-time freshness per repo, working on any GitHub plan. Each repo and base branch is detected read-only as `merge_queue` (a ruleset merge queue), `require_up_to_date` (strict required checks in a ruleset or classic protection) or `harness` (neither, or the settings can't be read). In the `harness` mode the merge poller brings an approved PR that fell behind up to date and posts `harness/up-to-date`; the ticket's **Ready to merge** step says which mode applies and, for `harness`, that merging early is not blocked. When a waiting PR's branch is updated, the harness statuses are copied to the new head if the PR's own changes are unchanged. Override with `merge_readiness.merge_mode`. See "Merge-time freshness per repo" in `docs/runbooks/governed-delivery-flow.md`.
- Live feed of file changes in coding runs. The worker watches its checkout every 15 seconds and reports each file created, edited, deleted, renamed or put back as it was on the base, with the step it happened in, line counts and a diff excerpt of at most 200 lines (secret-looking values masked before they leave the worker). Changes the harness makes (a protected-file revert, a base merge) are marked as the harness's; many files changed at once under one folder (a formatter) become one grouped event. Events go to the memory API with the job token (`POST /coding-jobs/<id>/events`, table `coding_job_events`, migration 053) and are deleted after the coding-log retention period. The console shows them on each running ticket's card and on a new Live → **Files** tab across every running ticket the person can read, refreshed every 5 seconds; each opens a page with the diff, the file's history in the run, checks that failed on it and the PR link. Access follows program read access. See `docs/data-handling.md`.
- A placeholder in a work order's file list (`features/<JIRA-KEY>-draft-badge.feature`, `{TICKET}`) is matched as a glob: the feature file the coding agent named after the ticket satisfies it. The required-files check compared the literal text and failed a run whose checks all passed; the code review scope check reads it the same way.
- Cancelling a program's only running phase (from chat or the workflow's own Cancel) now cancels the program too. It used to stay "running" with nothing left to run and keep a person's in-flight slot. A program with another phase running, or a ticket queue with tickets left, is left alone. The program page shows phases before the first one that ran (a program started at a later phase) as "skipped", not "not started".
- Program flow fixes found running real requests through the console:
  - Ticket selection: a request naming several tickets or an epic ("implement the tickets in epic PROJ-33 except PROJ-45, and PROJ-47") becomes a confirmed queue of exactly those tickets: epics expanded into their tickets, excluded and done tickets listed with why, blockers first. A key after "except", "do not implement" and similar is never picked. An epic is never coded: ticket delivery refuses it, and "implement PROJ-33" on an epic queues its tickets. Routing and the program proposal force such requests to the queue whatever entry the model suggests. Before this, one request coded the epic itself and another coded the one ticket it was told to leave out.
  - Stuck steps: Retry takes guidance (Inbox and `/retry <guidance>` in chat), which the rerun's agent sees in its prompt (`human_guidance`). The Inbox shows the error that stopped the step under "Why it is stuck" (the last attempt's summary moves to "Last attempt") and a summary that fits planning steps. "Accept as is" refuses output that doesn't fit the step's output schema; content checks can still be accepted.
  - Plan hand-off: an approved plan returned under `artifacts` is still saved; design frames and screen specs follow an approved plan into the work orders and filing (`content_to_jira_work_orders` 1.2.0); notes left when approving a plan or PRD ("approve with these changes") reach the refinery and the story writer as `prd_approval_notes`, which they must apply (`work_orders_from_prd` 1.3.0, `jira_from_prd` 2.3.0).
  - The planner's API contract check no longer treats endpoints called in acceptance tests (or a work order saying "Endpoints: none") as endpoint changes.
  - Program binding keys no longer stack `web:` prefixes when a program starts from a chat that already has one.
  - The Jira routing rule's hint changed (`config/ts-harness-runtime.yaml.tmpl`): re-render the runtime config on deploy (`scripts/ops/sync-runtime-config.sh`).
- UI work orders are built and checked against every design frame they name, not just the prose. A schedule screen passed review and QA while missing a side panel, the blocks' content, the drag from its Carry-over lane and its page colours, because the work order named 19 frames (an agent call sees 6), its prose described each screen in one line, QA ignored colour differences, and a frame with no screenshot passed as "unverified". Now:
  - Planning: one UI work order per screen, at most 6 frames (`TS_HARNESS_MAX_FRAMES_PER_WO`), each with a `## Frame checklist` entry (route, state, and everything the frame shows, colours included), written from the screen specs, which now reach the planner and the story writer (`screen_specs`). The planner contract rejects a work order without them.
  - Coding: one `@frame-D7` scenario per frame; verification runs each at its frame's size (recorded in `## Design frames`) and screenshots its last state; captures go to the evidence branch and `frame_captures`.
  - QA (`governed-qa-gate` 1.10.0): each frame next to its capture in one image, plus colours sampled from both (`design_checks`). Every listed frame is a criterion; a missing element or state, a different colour token, or a missing capture fails.
  - Tests rewritten to fit: `review_precheck` lists removed, added and reversed test names and assertions (`test_changes`); code review (1.10.0) blocks a reversal the work order did not ask for, and QA treats it as a defect.
  - Every night: frames QA passed are recorded per web repository; the nightly smoke run (`react-web` 1.5.0) captures each route at its frame's size against the real API, and `POST /webhooks/smoke/frames` compares them with the design and proposes one bug for what drifted (`GET /webhooks/smoke/frames` lists what to capture).
  - Realistic data: `node-api` 1.3.0 adds `src/lib/relativeDates.ts` and `scripts/dev-seed.ts` (`npm run seed:dev`, also run by `start:smoke`) for reference data a fresh environment needs; `react-web` 1.5.0 resolves `{today+3}`-style dates in seeded test data; the planner asks for both.
  - `verified_merge` 1.8.0 (`deliver_work_order` 1.7.0, `implement_jira_ticket` 2.2.0). The runtime now depends on Pillow. See `docs/runbooks/design-references.md` and `docs/runbooks/nightly-smoke.md`.
- API types stay tied to the backend's spec. A web app's types written by hand "until the backend's openapi.json is copied in" passed every test against a fake API built from the same types and broke in production. Now: the planner contract rejects deferred work ("until X exists", "for now", "later") without a `Follow-up: WO-NNN` work order that depends on the producer, an API work order that changes endpoints without updating `openapi.json`, a web work order that calls them without depending on it or generating its types, and hand-written API types (`prd-to-work-orders-governed` 1.5.0, bug intake too); code review (1.9.0) and QA (1.9.0) block a web PR with hand-written API types or calls outside the spec; `react-web` 1.3.0 adds `npm run check:api` (in `npm run lint` and CI), which fails on API types declared by hand under `src/api/`, and a test that a path outside the spec does not compile; and a merge that changes an API's `openapi.json` starts a refresh run on each web repository that reads it (`contract.openapi.refresh_on_change`, default true), one open refresh PR per web repository, updated by later changes. See `docs/runbooks/testing-policy.md`.
- Nightly smoke run for web repositories (`react-web` template 1.4.0, `node-api` 1.2.0): each night and on demand, `.github/workflows/nightly-smoke.yml` builds the web app and runs its `@smoke` features against the real API (checked out with a read-only deploy key, started with `npm run start:smoke`: migrations, deterministic seed data, fixture adapters) on a real Postgres. It signs in through the API's new CI-only test issuer (accepted only with `AUTH_TEST_ISSUER=true` outside production; the API refuses to start with it in production; `npm run token:test` mints a token with the same roles claims as Entra ID), opens every static route of the router, makes one write, and checks every real response against the API's `openapi.json`. The normal acceptance suite is unchanged. On failure it uploads screenshots and traces and posts a report to the new `POST /webhooks/smoke` (per-repository token derived from `SMOKE_REPORT_SECRET`), which de-duplicates by (repository, scenario, route) and proposes one `bug_to_jira_work_orders` program per report at `bug_intake`, so a person approves before anything reaches Jira. The bootstrap keeps the workflow only for a web repository linked to its API and sets up its access (the `API_REPO` variable, the deploy key and its secret, the report secrets); encrypting GitHub secrets needs PyNaCl, which the harness does not ship, so until it does the secrets are a documented manual step. Funnel (phase 12b) exposes `/webhooks/smoke` next to `/webhooks/github`. See `docs/runbooks/nightly-smoke.md`.
- API contract check for web repositories (`react-web` template 1.2.0): every acceptance scenario checks the fake API against the backend's `openapi.json`, so a path, request or response the backend would not accept fails the scenario. The coding job fetches the backend's current spec (named in the web repository's `.ts-harness.yml`, `contract.openapi`) and sets `OPENAPI_CONTRACT`; template-started projects get the entry in the bootstrap PR. It found two gaps in the template's own fake on day one (seeded rows lacked `createdBy` and `body`). See `docs/runbooks/testing-policy.md`.
- Screenshots for repositories whose acceptance tests are Gherkin features run by Playwright (`playwright-bdd`): when a change adds or edits `.feature` files and the BDD check passes, the harness generates their Playwright specs (`bddgen`) and reruns them with screenshots on, so the PR gets the same evidence as plain Playwright specs (up to 6 images, frames at each assertion).
- Coding job quotas are install-config settings: `runtime.coding_quota_per_user`, `coding_quota_per_program` and `coding_quota_per_team` (unset keeps the defaults of 2, 3 and 8; 0 removes a limit). They reach the launcher through the runtime config, so a redeploy no longer resets them. With one person starting all the work, the per-requester default of 2 had capped the install at 2 running jobs regardless of `coding_max_running`.
- A PR is marked draft while a revision runs and ready again when the run ends, whatever its outcome, so it does not look reviewable or mergeable on the previous commit's checks.
- Disagreements between code review and QA are arbitrated instead of always stopping for a person. When the gates send a work order back in turn, a strong model reads the work order and both gates' feedback, and the next revision runs with its ruling as one consolidated instruction. A person is still asked for conservative projects, at 8 revisions, for security findings, when the work order does not settle the dispute, or when no arbiter model is configured. See `docs/runbooks/governed-delivery-flow.md`.
- The coding run card lists the timed steps inside each phase. A long "Pre-push review" now shows, for example, reviewing (round 1) 1 m 40 s, fixing 2 review findings 9 m, quick re-check of the fix 1 m. "Push" shows checking the changes, scanning for secrets, pushing the branch, updating the pull request and publishing screenshots. Each verification pass is its own step ("running checks after fix pass 1"), and finished runs show the same steps in the ticket story. Steps come from the worker heartbeat and are stored in the job result (`step_timings`). An oversized heartbeat now drops the step list instead of storing cut-off JSON.
- Each coding job records where OpenCode spent its time (`result.opencode_timeline`): per session, model time against tool time, and the slowest tool calls, with anything that looks like a credential masked. OpenCode's own database is deleted with the worker, so this is the only record. `scripts/ops/coding-job-timeline.py <work order>` (or `--last N`) prints a ticket's runs: queue wait, harness phases, the verification checks and the OpenCode sessions.
- Coding jobs can run on ECS Fargate without `scale_out`. Set `runtime.coding_workers_on_ecs: true` to build the worker stack: cluster, task definition, ECR and the internal memory API load balancer. With `network_mode: default` it goes in the default VPC, where workers get a public IP for outbound traffic only, no inbound rules, and egress on 80 and 443. There is no egress proxy there. `runtime.coding_launcher` (`local` or `ecs`) chooses where jobs run, and `runtime.coding_max_running` how many run at once. On the host, `scripts/ops/coding-launcher.sh local|ecs|clear|status` switches without Terraform: it waits until no job is active, then recreates only `coding-launcher`. `redeploy.sh` pushes the worker image it built to ECR (`scripts/ops/push-coding-worker-image.sh`), so Fargate workers run the host's code. Redeploys now also wait for running ECS jobs. Without the LLM proxy, Fargate workers get the same provider keys as local workers. See `docs/runbooks/coding-workers-ecs.md`.
- A pre-push review fix gets the quick re-check only when the repository runs CI on pull requests; otherwise the full verification runs again, so a merged commit always has a full test run behind it. The migration-order contract test now checks the whole list (it only checked the 02x migrations).
- Program LLM budgets: every delivery program can have a budget for its whole life (`budgets.program_default_usd`, default 0 = none), which an admin, or a manager of the program's team up to `budgets.manager_max_monthly`, can change on the program page (`PUT /admin/budgets/programs/{id}`, audited as `program_budget_set`). The program page shows spend against the budget and this month's spend. At `budgets.warn_at_percent` the program's Slack thread and the ops channel are told once; at 100% `budgets.program_action` applies and the message says which: `downgrade` (default; revisions, complex first runs and the pre-push review use the default coding model) or `stop` (the program's new coding jobs wait with `held_reason = 'program budget reached'` and each held ticket's thread is told how to release it). Coding usage without a program on the job is now recorded against its workflow's program, and migration 051 fills in older rows. See `docs/runbooks/program-budgets.md`, which also notes that `policy.budget_warn_usd` and `budget_hard_stop_usd` do not limit spend.
- Security: tenant isolation and role review ([`docs/security/tenant-isolation-review.md`](docs/security/tenant-isolation-review.md)), with these fixes:
  - Tool approvals (`/admin/approvals`, decide, and the Inbox) are limited to the caller's tenant.
  - Console gate approve and reject check that the task belongs to the caller's tenant, service tokens included.
  - A project note can be promoted only from its own project, and note history is tenant-scoped.
  - `GET /admin/workflows/{id}` and the program budget check program read access.
  - Deleting a workflow fragment draft needs `edit_config`. So do the channel `send-test` route and unsigned GitHub webhook deliveries.
  - An OIDC token without a tenant claim is refused in multi-tenant mode instead of taking the tenant from the body.
  - The coding job checks its repo against the workflow's tenant.
  - Agent workflow tools refuse another tenant's workflow id.
  - `/skills/doctor` ignores a `tenant_id` query parameter.
  - Google OAuth: `/oauth/google/start` requires a sign-in and connects only the caller's own email in their tenant, the OAuth state is HMAC-signed (`TS_HARNESS_OAUTH_STATE_SECRET`, else the client secret), and the callback's error page is escaped. Consent links issued before the upgrade stop working; ask for a new one.
  - A contract test fails when a gateway route skips authentication or a permission check.
- Data retention: a daily `apply-data-retention` system job trims and deletes stored client data in small batches (500 rows per category per run): coding job log tails, check output and progress after 30 days (status, timings, usage, PR link and the pre-push review summary stay), transcript messages after 180 days, large task inputs and outputs (and the coding job spec) of workflows finished over 365 days ago, audit events after 730 days (approval and decision events keep their own window, forever by default), and design images 180 days after their program finished unless another program or open workflow uses them. Running and blocked workflows, unfinished programs and unfinished coding jobs are never touched. Each window is a `TS_HARNESS_RETENTION_*` variable (0 keeps forever). `scripts/ops/purge-program-data.py <program_id> --dry-run` / `--yes` deletes one finished program's data on request and records a `program_data_purged` audit event with counts only. `docs/data-handling.md` describes what is stored where, what goes to model providers, the defaults, the deletion procedure and the logging rules. Migration `051_data_retention.sql`.
- Opt-in network egress allowlist for local coding workers (`CODING_EGRESS_ALLOWLIST=on`, phase 12). Workers run on an internal docker network and reach the internet only through a new `egress-proxy` (squid). It allows GitHub, the npm, PyPI and Go registries, Node toolchains, every project registry host and the domains in `CODING_EGRESS_ALLOW`. Model providers are allowed only when the LLM proxy is off. Workers still reach memory-api, the runtime and the LLM proxy. The proxy logs refused requests as `TCP_DENIED`. `scripts/ops/coding-egress.sh check` proves the policy from a worker, and `scripts/ops/coding-egress.sh denied` lists refused hosts. Off by default; see [Coding jobs](docs/runbooks/implement-work-orders-opencode.md#coding-jobs-opencode-outside-agent-runtime).
- Production alerts in the ops Slack channel. A `check-alerts` system job runs every 5 minutes and posts when a step has waited for a decision longer than 4 h, 2 or more coding jobs were lost in the last hour, a work order's coding step has run 5 or more times and it is still open, LLM spend passes $20 in the last hour or $150 since 00:00 UTC, the disk is over 85% full, or the memory API or LLM proxy fails its health check. An alert is posted once and repeated only after 6 h (`TS_HARNESS_ALERT_REPEAT_HOURS`), with a "Resolved" line when it clears; state lives in `runtime_kv`. Thresholds are `TS_HARNESS_ALERT_*` variables. The job is skipped unless an ops channel and Slack bot token, or `SLACK_OPS_WEBHOOK_URL` (secret `slack-ops-webhook-url`), is configured. `redeploy.sh` posts its result (`redeploy done <sha>` or the failed step) through the webhook with the new `scripts/ops/notify-ops.sh`. See `docs/runbooks/ops-alerts.md`.
- **Setup checklist** on each project's page (System → Projects → a project, `GET /admin/projects/<id>/checklist`): checks live that the Jira key is set and allowed, Jira is reachable and the project exists, repositories are linked with the GitHub App installed and the base branch present, repositories are inside the tenant org, branch protection requires the harness checks, CI runs on pull requests, approvers have GitHub (and Jira) mappings and the project settings are valid. Each item is ready, warning or failing with how to fix it and a link to the console page; **Re-check** runs it again. Checks are time-boxed and one that errors shows "Could not check" without failing the others.
- End-to-end regression and console browser tests. `scripts/ops/e2e-regression.py` drives a fixed sample PRD (`tests/fixtures/e2e/sample_prd.md`) through a running staging harness the way a person does (chat on `/session/message`, gates on `/admin/inbox` and `/admin/workflows/approve`): it approves each planning gate, files the work orders in a sandbox Jira project, runs `implement all <PROJECT>`, merges each PR in the sandbox repository once it is Ready to merge, and writes a JSON and Markdown report (each stage completed, PRs created and merged, nothing stuck or failed, no coding job lost, total time and coding cost), exiting non-zero on a failure. It refuses to run unless `E2E_SANDBOX=1` and the repository and Jira project are the configured sandbox ones; `--dry-run` checks config and connectivity only, and the run cancels its programs and closes its leftover PRs afterwards. A nightly cron entry is documented (opt-in). Playwright tests (`npm run test:e2e` in `admin-ui`, also run in CI) cover the Inbox (approve a gate, revise a stuck step with guidance, cancel a chat proposal), Live (current run's phase breakdown, ticket story with findings) and System Settings → Identity (add and remove a GitHub login mapping) against a mocked API. See [`docs/runbooks/e2e-regression.md`](docs/runbooks/e2e-regression.md).
- Console **Metrics** tab on the Live page (`#/live/metrics`, `GET /admin/metrics?days=&project=&repo=&running=`): delivery quality over a period, filterable by project and repository like History: first-try pass rate, revisions per ticket split by trigger, time to merge and time waiting for a person, the pre-push review's catch rate, how often code review blocks a run the pre-push review passed, cost per ticket and the share on the stronger model, and lost coding jobs; a weekly trend, and the tickets costliest first, each opening its story.
- Coding worker hardening: a job's GitHub token is limited to its repository and to what a coding job needs (`workflows` only when the work order is about CI); changes to `.github/workflows` and `CODEOWNERS` are reverted unless the work order asks for them; every coding prompt carries security rules against instructions hidden in tickets, comments, notes or files; the code review gate (`governed-code-review` 1.8.0) and the pre-push review flag text that addresses a reviewer and code that exfiltrates secrets or weakens checks (`docs/security/coding-workers.md`).
- Release process: `scripts/ops/release.py prepare|tag|notes` promotes `dev` to `main` with the changelog cut and version bump, tags the release and publishes its notes; `scripts/ops/rollback.sh v<version>` moves a host to any release. New migrations must be additive (a contract test refuses drops, renames and type changes without a `-- destructive:` reason), so rolling the code back never needs the database rolled back (`docs/releasing.md`).
- A code review revision may also edit the module glue beside the flagged files (`index`, `types`, `constants`, `schema` files in the same folder) and the files they import directly; the coding prompt and the editor's permissions use the same list. Before, a fix that had to widen a type or export a function was refused twice (`revision_scope`) and parked for a person.
- A coding job may run 2 hours before it is marked lost (was 60 minutes, which cut off a complex first run in its third pre-push review round), and the pre-push review stops starting rounds after 25 minutes (`TS_HARNESS_PRE_PUSH_REVIEW_BUDGET_SECONDS`) and pushes, leaving the rest to the code review gate.
- In a parallel run, a ticket waiting for its PR to be merged frees its slot, so independent tickets keep going; at most 4 PRs (`TS_HARNESS_POOL_MAX_WAITING_MERGE`) wait for a merge at once.
- Generated files CI checks are regenerated by the coding job: a CI step that runs a generator and then `git diff --exit-code <files>` (for example `npm run openapi` and `openapi.json`) now runs during verification, so a change ships with those files up to date instead of failing CI. Only package scripts and `npx prisma generate` run; deploy-like scripts never do. Those files are not reverted as undeclared or flagged as out of scope.
- Chat proposals can be cancelled: `/cancel` in the conversation, or **Cancel proposal** in the Inbox, which now lists every offer still waiting for a yes or no (with when it was made). Closes #109.
- Approving a gate twice no longer starts the next phase twice: the second approval returns `already_decided` (it is re-checked under the workflow lock), the Inbox removes the item at once and ignores it on refresh, and the program page replaces the buttons with "Decided; the next step is starting…".
- A queue or ticket waiting for repositories' bootstrap PRs continues on its own once they merge, instead of asking for a new run.
- Repositories linked to a project are allowed for coding, releases and cleanup without also adding them to the repo allowlist (the GitHub org restriction still applies).
- A pre-push review fix is re-checked quickly (related tests, static checks and the security scan on the changed files) instead of rerunning the whole verification, saving a full test-suite run (4-6 minutes on a mid-size repo) per review fix; CI still runs everything. Each verification check records its duration.
- A ticket is never built twice: the ticket pool skips a ticket another run is already building and starting a single ticket that is in progress elsewhere blocks with a clear message. A pool whose remaining tickets wait on a ticket another run is building keeps waiting instead of finishing with tickets left behind, and that ticket merging wakes it (`POST /workflow/running-tickets`).
- Clearer ticket queue messages: the confirmation is headed "Confirm the <PROJECT> ticket queue"; the parallel run's start says what started and why the rest wait ("Started PROJ-7. Up to 2 tickets run at once. PROJ-8 and PROJ-23 wait on PROJ-7; 19 more wait on later tickets.") instead of "in parallel" for a single ticket; a ticket that names no design frames says "Design: none" instead of listing every frame of its program (only named frames ever reached the coding job and QA).
- The ticket queue orders ready tickets by issue number, not as text: PROJ-7 now comes before PROJ-22 (text order put PROJ-22 first).
- Ticket story on Live and the program board: **Details** shows where a ticket's time went (coding, queue, CI, review, QA, waiting for you, coding cost) and everything that happened in order, in local time: each coding run with its trigger, model, duration, cost, phases and pre-push review result; CI results; code review and QA verdicts with their blocking findings; waits for you and who decided with their guidance. A running coding job shows a live phase breakdown (setup, coding, running tests, pre-push review, push) on its card, reported with each heartbeat (migration 050). Inbox decisions record the person's email.
- System Settings → Identity: GitHub logins, roles, departments, role permissions, identity provider group roles and service permissions are edited as rows with Add and Remove, like the Slack and Jira mappings, instead of JSON text boxes. Adding a mapping needs both fields; emails are lowercased.
- Faster ticket delivery. **Pre-push review**: once a coding run's tests pass, a stronger model reviews the change inside the job with the code review gate's checklist, and its blocking findings are fixed and verified locally (up to 2 rounds) before the push, instead of costing a full round trip through CI and the gate (`TS_HARNESS_PRE_PUSH_REVIEW_MODEL`, `TS_HARNESS_PRE_PUSH_REVIEW_ROUNDS`). **Workspace reuse**: a ticket's coding jobs share one named volume, so a revision keeps its clone, dependencies and toolchain (`CODING_WORKSPACE_REUSE`, `CODING_WORKSPACE_TTL_HOURS`, default 48); coding results record seconds per phase (`timings`). **Model routing**: every revision runs on the stronger coding model (`TS_HARNESS_REVISION_CODING_MODEL`), and work orders the planner marks `Complexity: High` start on it (`TS_HARNESS_COMPLEX_CODING_MODEL`). **Lost jobs found sooner**: a running coding job with no heartbeat for 90 seconds is lost (was 5 minutes); a job still starting gets 5 minutes (`CODING_JOB_START_TIMEOUT_SECONDS`). **Fewer waves**: the planner slices for parallel delivery without oversized work orders (`prd-to-work-orders-governed` 1.4.0), and the Planner approval shows the delivery plan with warnings for long single-work-order chains and large work orders. Code review goes deep once and then follows up: the first review of a large PR is a file-by-file sweep with deep checks (input coercion, storage precision, change detection, partial failure and more; `governed-code-review` 1.7.0), and later rounds check the last round's findings and the files changed since then, raising only blockers in code already reviewed. The pre-push review applies the same deep checks. `TS_HARNESS_STRONG_MODEL_FROM_REVISION` (default 1) sets the first revision on the stronger model. The Live page no longer counts the wait for a decision as a revision's coding time (a revision's step starts when it is created, not when the attempt it revises ended).
- **Retry** for any stuck step (`/retry` in chat or as a PR comment, Retry in the Inbox): runs the step again with the same input once its cause is fixed, without spending the revision budget. Lost coding jobs and other `transport_error` blocks are retried 3 times beyond the step's own budget. Redeploys and setup start the coding launcher only after the new agent-runtime is healthy and will not be recreated again (`start_stack_launcher_last`; `recreate_ts_harness_services` drains and holds the launcher while it recreates agent-runtime, as phases 14 and 15 do), so workers launched during a redeploy are no longer lost. A retried step no longer shows as "Stuck … waiting for your decision" while its coding job runs: a retry clears the decision flag, and a step waiting on a coding job or CI never counts as awaiting a decision.
- Console **Live** page and program **board**: every ticket being built across running programs with its current step, time in step and in total, revisions, who it is waiting on (you, the agent, CI, a coding slot), the coding job's latest output and a per-step timeline; and, per program, the ticket queue as columns with Jira blockers, timelines (every attempt) and where the time went; and a **History** tab of finished runs across programs with their outcome and who decided, filterable by period, project, repository and outcome and searchable (`/admin/live`, `/admin/history`, `/admin/programs/{id}/board`).
- Parallel ticket delivery: `implement all <PROJECT>` runs a rolling pool (`implement_jira_pool`, `jira_to_pr` 1.3.0) when the project's new **Parallel delivery** setting (`max_parallel_tickets`, migration 049, default 2) is above 1. Every ticket whose blockers are done starts as its own child up to the limit, preferring idle repositories, and a finished ticket's slot is refilled at once. Child workflows forked by the pool post to the program's thread.
- `/revise`, `/accept` and `/stop` work as GitHub PR comments on a work order whose step is waiting for a decision, for mapped GitHub logins with `approve_gate` (and program approve access under OIDC); the harness replies on the PR. A restart that interrupts a gate's revise loop no longer leaves the ticket stuck: the runner replays the block handling for such steps once a minute.
- Triage at the revision limit: when a work order has used its three automatic revisions, the harness classifies the open findings and decides by the project's new **Revisions** setting (`revision_autonomy`, migration 048; default autonomous). It continues while the PR improves (consolidated guidance, stronger coding model for revisions 4-8), accepts when only minor or out-of-scope findings remain (filing follow-up tickets under the epic), or asks a person. Security-sensitive findings and the 8-revision ceiling always go to a person. Code review reports every blocking issue in one pass (`governed-code-review` 1.6.0).
- Revisions no longer strand the steps after the merge wait: a step such as `close_ticket` that still waits on a step a revision cancelled is recreated to wait on that step's replacement. Before, a revised ticket never closed and the ticket queue stopped after it.
- Coding runs with a long prompt (a large work order plus review history or guidance) no longer fail with "Argument list too long": a prompt over 64 KB is written to `.ts-harness/prompts/` and attached to OpenCode with `-f`, since Linux caps one argument at 128 KB.
- Guided revisions: when code review or QA raises the same finding again after a revision, the harness asks a stronger model why the attempts failed, then sends one revision with that diagnosis (approach, what to avoid, the test that proves it) and the full finding history to a stronger coding model, even when the revision budget is used up. If the finding still comes back, the stop message carries the diagnosis and a ready-made `/revise` reply. Settings: `TS_HARNESS_REVISION_DIAGNOSIS_MODEL`, `TS_HARNESS_GUIDED_CODING_MODEL` (`off` disables).
- The PR checks wait no longer waits for `harness/qa` (or other TS Harness statuses published by later stages) when branch protection requires it; it was waiting for a status the QA gate only posts after the wait, so the delivery never moved on.
- Coding runs: a run whose output ends a moment before OpenCode exits is no longer killed and reported as "opencode exit -15"; the harness waits up to 60 s for it to exit and keeps the real exit code. The integration-test helper (`python -m ts_harness.implementation.test_services integration`) no longer crashes with `NameError: playwright_config` (its entry point ran before the helpers below it were defined).
- System Settings: activating a draft copied from the active settings no longer fails with "cannot reuse historical definition_version". Activation raises the draft's patch version until it is unused (for example 1.0.1 to 1.0.2).
- Refile work orders in Jira: in a program that filed its work orders, "refile the work orders in <PROJECT>" (after a confirm) runs the filing step again from the same work orders. The filing ends the program but may now run again (`software_delivery` 1.6.0, `content_to_jira_work_orders` 1.1.0, `bug_to_jira_work_orders` 1.2.0: a manual, confirmed self-edge on `file_jira`). A refile files a new epic instead of reusing the recorded one, replaces the program's recorded tickets, and comments on any replaced tickets that still exist. Program validation allows a self-edge only when it is manual, confirmed and has signals.
- System Settings: Activate now activates a draft that Validate already saved. Validate saves the draft and clears the unsaved-changes flag, so Activate used to send no draft and failed with "runtime_settings_draft_id or routing_draft_id is required".
- System Settings shows and edits `jira_filing.allowed_projects` ("Allowed projects" in the Jira section), and editing any other Jira filing field keeps the list. The page no longer opens a draft saved before the current settings were activated: it starts a fresh draft from the active settings, and activating such an old draft is refused, because it would undo the newer activation.
- Jira filing stays in the right project. Filing uses the Jira key set on the program's project (unless the request names another allowed project) instead of falling back to the only allowed project; when that key is not allowed, filing stops and says so. The filed keys are read from the filing step's structured output (top level or `artifacts`), and keys read from its summary are kept to the filed project, so text such as "invariants AD-1 to AD-6" is no longer assigned or recorded as tickets.
- An approval can be decided only once the step it approves has finished. A program's waiting approval (and so the console Inbox) shows a gate only when the steps before it have succeeded, and every approve or reject path refuses an earlier one with "<gate> is not ready yet: <step> has not finished". Before, approving one gate made the next gate of the workflow appear at once, so repeated clicks approved later stages before they had produced anything.
- Work-order planning approvals take the reviewer's words (`work_orders_from_prd` 1.3.0, `prd-to-work-orders-governed` 1.3.0): approving Refinery or Foundry with notes keeps them as `reviewer_notes`, which the later stages read and follow over the earlier assumptions; rejecting Refinery, Foundry or the work orders with comments redrafts that stage and runs the later stages again. Before, approval notes were dropped and a reject stopped the stage.
- Full PRDs frame the project in detail (`prd-drafter` 1.6.0): the Problem Statement has a table of the problem per persona with evidence and its source, Current state, Cost of doing nothing and Why now; Goals are a table (goal, who benefits, measure, baseline, target, by when, the test it serves); Non-Goals are a table with a category, reason and revisit date. Every row cites a source; no invented numbers. The PRD checks flag a Full PRD whose framing is missing these, a goal without a measure, baseline or target, and a non-goal without a reason. Lite PRDs keep the short form.
- Screen specs use an attached design inventory: a Markdown source with a screens table (number and `Screen` columns) groups the Figma frames by its screen numbers, gives each screen's row to the vision model as the designer's intent, and turns every item marked partial into a high-impact question under Known gaps, asked first.
- Screen specs from Figma (`prd_request` 1.7.0, `draft_screen_specs`): a PRD request with Figma frames drafts one spec per screen (frames grouped by name) with the vision model: what the frames show, and questions with recommended answers for what they can't (behaviour, data sources, rules, phone and tablet). The specs are posted before intake; intake asks the open screen questions as `S1`… next to its own (`prd-intake` 1.4.0), and the draft and revisions build each screen's requirements from the specs and the answers (`prd-drafter` 1.5.0). A step whose output says `skipped` (no frames) posts nothing.
- Projects are picked, not typed: the console chat box has a project picker (sent as `project_key`; proposals show "Project: Name (KEY).", the program a start creates gets it, and a linked program without a project gets it, as a user-set project), and the program page's Project field is a dropdown of System → Projects.
- PRD checks read a whole requirement for its 'Specified in' and 'Verified by' links, not only its first line: a long requirement written as bullets was reported as unlinked and untested.
- Claude replies are streamed and may run to 64k output tokens (`ANTHROPIC_MAX_TOKENS`, was 16k including thinking; `ANTHROPIC_STREAM=0` turns streaming off). A full PRD revision ran past 16k and was cut off mid-JSON, which blocked the stage; streaming keeps a long reply from hitting the request timeout. A reply that still hits the limit is logged as cut off.
- Routing: bug requests need bug-report phrases ("is broken", "getting an error", "throws an error", "stopped working"...); bare "broken" and "error" matched specifications ("the PRD will be broken into work orders", "error states") and sent PRD requests to the bug program. PRD requests also match "a prd" and "prd for", and only "quick prd" / "quick draft" (not any "quick") makes one informal.
- Declining a proposal keeps the chat's attachments for the next request (people decline a wrong route and ask again), and every proposal lists the sources it will read ("Sources: fact-sheet.md, 2 Google Drive links, 2 Figma links").
- Intake answers wait for "ready" or "yes", as the intake questions say: a reply with answers or files is recorded on the waiting task ("Got it… say ready when you want the draft"), and the draft gets every recorded reply plus "use the recommended answer" for the rest. A reply that only attached a file used to start the draft at once.
- Long outputs published to Google Docs are imported as Markdown by Drive, so headings, bold, bullet, numbered and checkbox lists, tables, links and code render; the old writer (the fallback) styled headings only and left the rest as raw Markdown.
- Figma links typed in a chat message now reach the workflow it starts: message parsing picked up Google Drive links only, so a PRD request's Figma links were dropped before `gather_sources` (which reads them) ran. A Figma link in a reply to a started workflow is left in the reply text and not read then, since frames become design references only at workflow start.
- Planning keeps a repository's conventions in every state: an empty repository with a template carries the template's (`template_status: pending`), one already started from a template keeps `template` (`applied`, from its `.ts-harness.yml`) with its own `AGENTS.md`, and any repository with an `AGENTS.md` shares it as `conventions`. Merging a bootstrap PR before planning no longer drops the template's conventions from planning. `template_conventions` is now `conventions`.
- Testing policy (docs/runbooks/testing-policy.md). **BDD per project** (System → Projects → Testing: Auto, Always, Never): each program decides per repository once and keeps it, every work order gets a `## Tests` section (`BDD: required` or `BDD: off`) that Jira tickets copy, and verification and the coding agent follow it instead of whatever the repository held at the time; a repository needing BDD without a Gherkin runner gets one in its first ticket. **CI safety net**: a repository with code but no CI on pull requests gets an "Add CI" work order first, and its jobs become required checks once it merges (`require_ci_checks: true`). **Unit tests**: the templates enforce 80% line coverage of the code that holds logic in `npm test`; code review blocks new logic without unit tests (`governed-code-review` 1.5.0); QA fails a change whose unit tests failed or did not run (`governed-qa-gate` 1.8.0); related-test runs in large repositories turn coverage off (CI still enforces it). Migration `047_project_bdd_mode.sql`.
- Repository templates (`templates/node-api`, `templates/react-web`): an empty project repository (only a README, `.gitignore` or LICENSE) starts from a tested template through a bootstrap PR the harness opens before `implement all <PROJECT>` or a ticket runs on it; feature work waits until it merges, then the template's CI jobs (`checks`, `bdd`, `docker`) become required checks. Planning sees the template's conventions for empty repositories and plans on top of it. Both templates ship BDD (Cucumber / Gherkin via Playwright), unit tests, CI, Docker, Entra ID and the roles Admin and User; harness CI runs their checks. Settings `program_defaults.repo_templates`; `program_defaults.jira_tracking` is now an accepted setting too (it was rejected by validation).
- Figma page and section links read every screen inside them (up to 200 per link, `TS_HARNESS_FIGMA_MAX_FRAMES`; up to 200 design references per request, `TS_HARNESS_MAX_DESIGN_REFS`): sections open however deeply nested, frames render a few per request four at a time with a longer timeout, a failed batch is retried frame by frame, a short rate limit is waited out, and a frame that still fails is noted while the rest are kept. Frames that render alike stay separate references. Several Figma links in one request are all read, in order, never skipped by the intake time or text budget; a frame reached by two links is one reference, and frames over the total limit are reported. Each work order that names frames gets a `## Design frames` section with the frames' titles linked to Figma, which the Jira tickets carry (a ticket filed without it gets it back from its work order). A Figma network failure is an intake source error, not a failed intake (a large page timed out rendering every section as one image).
- Jira ticket status follows delivery: In Progress when coding starts, Done (with a comment linking the PR) when the PR merges (`close_ticket`, `implement_jira_ticket` 2.1.0), configurable in `program_defaults.jira_tracking`. Merged tickets stayed To Do and `implement all` would have built them again.
- Filed work-order tickets are assigned to the requester by a deterministic step (`assign_tickets`, `file_work_orders_as_jira` 1.4.0): the filing agent's Jira tools cannot set an assignee. It reports what it assigned, and does nothing when no assignee is asked for. A chat linked to a finished workflow follows its program's active one, so a reply reaches a gate in a phase reached automatically.
- A phase reached automatically that opens on a gate now says so in the thread (for Jira filing: how many tickets, which project or epic, assigned to whom). Programs created by promoting a chat workflow keep the requester's email too.
- PRD → work orders → Jira (`software_delivery` 1.5.0): "file work orders in jira" after the PRD runs the full planning stages and files each work order as a Jira ticket (work order section, pointer block with the work order's own repository and the root program, Blocks links), assigned to the requester so `implement all <PROJECT>` builds them in dependency order with the program's planning documents and design references. Program signals now prefer the most specific match ("work orders in jira" over "work orders"), the phase strip hides phases reachable only through branches not taken, and `$workflow.root_program_id`, `assignee_account_id` and `design_refs` resolve (an unknown `$workflow.` reference resolved to nothing; a test now checks every one in the definitions).
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

- A PR that conflicts with its base while waiting for CI gets the narrow conflict fix, like a conflict in the merge wait: a refresh revision with its own budget, in which GitHub merges the base in when it can, or OpenCode resolves only the conflicted files. Code review then looks only at those files, and QA runs again. It used to be a full coding revision with a full review that also used up one of the ticket's CI revisions (verified_merge 1.10.0).
- The pre-push review gets the code review gate's inputs as well as its rubric: the scope check (files outside the work order's list), the test-change list (removed, reversed or weakened tests) and the design frames the work order names (opened and compared), all computed from the local branch each round, plus the gate's "API types in a web PR" and "Tests that change meaning" sections. In the trial it missed 6 of 20 blocks, all requirement or design problems the gate saw with those inputs. Results record the variant `gate_inputs`; `scripts/ops/pre-push-review-trial.py` reports it separately with a "missed" count. `TS_HARNESS_PRE_PUSH_REVIEW_INPUTS=off` goes back to rubric only.
- Planning documents go into the planning program's own folder, `docs/planning/<program>-<id8>/`, and only missing files are written: the program's first PR adds them and later tickets read them. Before, every ticket rewrote the shared `docs/refinery|foundry|planner` files with its program's snapshot, which churned every PR, let one program overwrite another's documents, and caused merge conflicts between parallel tickets. The coding agent is told not to rewrite them. Added planning files are left out of the code review; edits to them, and any change to the old top-level folders, are reviewed. Existing documents stay where they are.
- A PR waiting to merge is brought up to date with its base only when it conflicts, not every time another PR merges. `ensure_fresh` passes a PR that is behind but merges cleanly, the merge fan-out skips it, and the merge poller sends back waiting PRs once GitHub reports a conflict. `merge_readiness.refresh: always` keeps the earlier behaviour. After an agent resolves a conflict, code review reviews only the files that changed since its last passing review (`review_scope`; governed-code-review 1.11.0, verified_merge 1.9.0); QA runs again in full.
- The pre-push review reviews with the code review gate's own rubric (the judging sections of `governed-code-review`, passed in by the workflow runner) instead of a hand-written approximation, as a trial: over 25 tickets the old review never got a first push past the gate. `TS_HARNESS_PRE_PUSH_REVIEW_VARIANT=legacy` restores the old prompt; results record the variant, and `scripts/ops/pre-push-review-trial.py` measures first-try gate passes against the agreed rule (fewer than 3 in 10 turns the review off).

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

- A coding run that OpenCode didn't finish because it was stopped by a signal from outside ("opencode exit -15", a container stop, a host restart) is queued again automatically, continuing from its pushed work, like stalls, time limits and provider interruptions (at most twice per step, shared with those). It used to wait for a person. A memory kill still retries with more memory, a cancelled job is never retried, and a plain failure still blocks.
- A coding run no longer dies as "opencode exit -15" when a tool prints bytes that aren't valid UTF-8 (test-runner progress output). The harness read OpenCode's output strictly, so one such byte silently stopped the reader; the harness then took the run for finished and killed it. Every subprocess read in the coding worker now replaces undecodable bytes.
- A web coding job finds its backend's `openapi.json` when the API repo's default branch (often `main`) doesn't have it: without a declared ref, the harness reads the job's own base branch first (usually `dev`, where the API work lands), then the project's branch, then the default. Web jobs had been blocked with "cannot read the API repository".
- Terraform creates the nightly smoke report secret (`smoke-report-secret`, a random value) with the other harness secrets, so the instance role can read it; before, phase 10 was refused (AccessDenied) and smoke reports stayed off. An optional secret that cannot be read is now a warning in the deploy log, not an "ERROR" (AWS and GCP adapters).
- CI's secret scan checks a pull request's own commits; pushes to `dev` and `main` still scan the whole history. The full-history scan on a PR read every open branch, so one branch's fake test value failed every other PR.
- A coding job is marked lost after 5 minutes without a heartbeat (`CODING_JOB_HEARTBEAT_TIMEOUT_SECONDS`, was 90 seconds): a worker busy with parallel type checks missed 90 seconds of beats while still working and was stopped.
- The coding prompt caps parallel OpenCode sub-agents at 4 (`CODING_MAX_SUBAGENTS`): seven at once, each running type checks, starved a 2 vCPU worker until its heartbeat stopped.
- A stall records why: whether the activity check was on (and its error if it turned itself off), when it last saw activity and of which kind, and the commands OpenCode was running with how long each had run (secrets redacted), in the log, the `opencode_stalled` event and the job's error. The work-in-progress commit says why the run stopped instead of always blaming the model provider.
- OpenCode is no longer killed as stalled while its sub-agents work: files changing in the checkout or in OpenCode's session data count as progress, not only the main session's output (a run with seven busy sub-agents was killed after 15 silent minutes and lost 155 changed files). A first run also pushes a work-in-progress snapshot every 15 minutes while it codes (`CODING_CHECKPOINT_SECONDS`), and a real stall keeps its work and is queued again (`stalled`, up to twice), continuing from the branch.
- The coding agent's edit fence is retired. OpenCode may edit any file; files beyond the work order's Files list stay in the change, are recorded (`out_of_scope_files`) and listed in the PR, and the code review gate judges them (the precheck passes them to the reviewer as `minor` information instead of blocking them as `major`). Only CI workflows and code-owner files the work order is not about are still reverted. The fence, parsed from the work order's wording, had kept OpenCode out of files the work needed five times.
- A web repository's coding job gets its backend's `openapi.json` from the harness. The worker's GitHub token covers only its own repository, so a job asked to regenerate API types from the backend spec could not read it and delivered tooling instead. The workflow runner now reads the spec (the backend named in the web repository's `.ts-harness.yml`, else the one API repository in the program's project) and passes it in the job; the worker writes it to a file, sets `OPENAPI_CONTRACT` and `OPENAPI_SOURCE`, and tells OpenCode where it is.
- OpenCode can say it is blocked. When something outside its control stops it (no access to a repository or file it needs, an edit permission refusing a required file, missing information), it writes the reason to a file outside the repository and stops; the run ends as `blocked: <reason>` instead of pushing part of the work.
- Coding and fix prompts ask OpenCode to run only the tests for what it changed, not the whole unit suite, BDD suite or build, which the host runs right after; long runs spent about a third of their time running the same suites twice.
- A coding run no longer "succeeds" without doing the work. The work order's Files list is a scope only when every entry is a path: an entry that describes files ("every file that imports the old types") makes it no limit, where before it became junk entries that kept OpenCode out of the files the fix needed. Bare names after a path take its folder (`src/api/health.d.ts`, `hub.d.ts`). Verification fails, and the fix pass is told which, when a file the list says to create, change or delete is untouched. The drafting skills write Files as paths or folders only.
- A long coding run no longer loses its work at the time limit. A first run pushes a work-in-progress snapshot to its work branch after coding and after each verification fix pass (secret-scanned, built without touching the checkout). About 20 minutes before the limit it stops cleanly as `time_limit` and is queued again, and the next run continues from that branch. The limit is 4 hours by default (was 2) and can be set with `runtime.coding_job_max_runtime_minutes`, which now reaches the agent runtime (the `CODING_JOB_MAX_RUNTIME_SECONDS` setting was read but never passed to it).
- Security: the deploy's `docker compose` calls no longer pass the compose environment (database password, API keys, tokens, the GitHub App key) as `env KEY=value` arguments, which any user on the host could read with `ps` for the whole build. They go through a file only the admin user can read, sourced and deleted before compose runs. Anyone who could run `ps` on a host during a build may have seen them; rotate the credentials if other users have access to the host.
- A coding run the model provider cut off (a content-filter block, an overload, a rate limit, a 5xx on OpenCode's final error line) is queued again, up to twice per step, instead of blocking the step. A first run's work in progress is committed and pushed to the work branch first (never onto an open PR, never when the secret scan finds anything), and the next run continues from that branch with a note to finish rather than start over.
- A ticket pool no longer blocks with "a blocker was not delivered" for queued tickets another run already delivered (Done in Jira), and a pool that gave up is reopened only when something changed, so a program whose tickets all merged finishes instead of re-posting the same notice every minute. When a pool wave ends, its queued tickets that are Done in Jira count as completed, so the program finishes instead of starting a wave for tickets another run delivered, and a pool that cannot start names only busy tickets from its own queue. A pool that waits for a child event after every child already finished (the event was lost to a restart) is woken after 5 minutes. A blocked notice is posted once per reason: web threads skip a repeat since the person last wrote, other channels remember the last block per step.
- Chat confirmations: "yes, start it", "yes please" and "ok go ahead" confirm a proposal ("yes but …" still does not). The reply that says how to confirm is kept in the thread (it was lost when the page reloaded the transcript), and while a proposal waits the assistant is told that the person starts it with a yes, so it no longer says the proposal cannot be started from the channel.
- A workflow's posts (repo pick, clarify question, drafts, work orders, filing results) go to the chat linked to the workflow, then to its program, then to the newest chat on the binding. A browser reuses one binding across chats, so a new program's posts went to an older chat.
- A phase whose next phase starts only when asked says what to reply ("Reply **draft the work order** to start work order") in the thread and in a chat approval's reply.
- Revisions: sending feedback says the revised version will be posted in the thread; the redraft is posted as "Revised with your feedback"; the program page reads "Revising <step> with your feedback" while it runs.
- The inbox and the program page show a repository picker on a repo pick and an answer box on an intake gate, sent with the approval; any gate takes an optional reply or rejection feedback.
- Bug and content intake see the requester's own messages (the declared-outputs adapter dropped the chat context, leaving only the proposal summary), and the bug context fetches a bare file name that only one file has and the files of a named folder listed by name (up to twelve files).
- The bug work-order skill keeps tooling checks (type check, contract checker, generation scripts) out of Gherkin: they are unit tests under Requirements.
- The integration check no longer reruns the BDD suite when it is the same suite (a `playwright-bdd` repository declaring `npm run test:bdd`, or Playwright running the generated specs) and the BDD check passed; it saved about two minutes per verification pass.
- A branch-refresh run reports its phases (setup, merging the base, resolving conflicts, running tests, push) on the run card instead of staying on "Waiting for the worker's first progress report". The card's last-output line no longer shows raw terminal colour codes (`[0m`).
- A revision starts from the latest base: when the base branch moved, it is merged into the PR branch before OpenCode runs (a conflicting merge is left to OpenCode with the files named), so the revision does not push a PR that conflicts with what merged since. When a PR gets a new commit while the harness waits for its CI (a hand-resolved conflict, a pushed fix), the wait moves to the new commit instead of waiting for checks that will never come.
- QA reports every unmet acceptance criterion in one pass: the gate's instructions say so, and its output is rejected (and repaired) when it marks more criteria failed than it lists defects, or fails a PR without a defect. A second QA pass that only then mentioned a gap that was there all along cost a full revision.
- A step whose model call fails for the provider's reasons (overloaded, rate limited, 5xx, timeout) runs again instead of blocking. A QA gate once recorded `server_is_overloaded` as its verdict, which sent the ticket back to OpenCode and then stopped it as a review/QA disagreement.
- A ticket pool no longer gives up on a ticket whose step blocked moments ago (its automatic revision may still be on the way): a block counts as a failure only after 10 minutes. A pool that did stop because a ticket failed reopens by itself, within a minute, once that ticket is retried, revised or merged, and starts the tickets it left behind.
- The security check that makes revision triage and review arbitration ask a person no longer matches the verb "authorize" in prose ("the spec does not authorize this order"); it matches security terms such as authentication, authorization, unauthorized, secrets, tokens and credentials.
- Coding runs no longer fail at start with "Model not found" for a model OpenCode's catalog drops (seen on Fargate workers, which start without a cached catalog: `anthropic/claude-opus-5-5`). Each run declares its coding and review models in OpenCode's config, with limits from the downloaded catalog when it has them.
- A PR that conflicts with its base while waiting for CI no longer waits forever (GitHub runs no CI on a conflicting PR). The GitHub poller, every minute, sends such a PR back to OpenCode with instructions to merge the base branch and resolve the conflicts, through the same revision path as failed CI; the re-gate then waits for CI on the new head, and the run's own security scan covers it.
- A running ticket pool now reads the project's `max_parallel_tickets` each time a slot frees, instead of keeping the value it started with, so raising the limit in Project settings takes effect without restarting the program.
- Code review revisions no longer revert files outside the cited list. A fix often needs a supporting file (a package script, a repository method); the OpenCode session that reverted them also checked out the cited files, so correct fixes were erased and the same findings came back revision after revision. Extra files are now kept, logged (`coding_revision_out_of_scope`) and listed in the result (`out_of_scope_files`) for the code-review gate. A revision that ends identical to the reviewed head fails (`revision produced no changes`) instead of reporting success.
- The quick re-check after a pre-push review fix never ran: the coding job never set `repo_has_pr_ci`, so every fix re-ran the full verification, even on repositories with CI on pull requests.
- Request sources were cut at 80,000 characters each (150,000 in total): a plant's 275,000-character scheduling-rules document reached the PRD only up to its fourth section. The defaults are now 300,000 and 400,000, adjustable with `intake_max_doc_chars` / `intake_max_total_chars` (Terraform and install YAML, through the runtime-config store) or `TS_HARNESS_INTAKE_MAX_DOC_CHARS` / `TS_HARNESS_INTAKE_MAX_TOTAL_CHARS`. The intake agent tells the requester when a source was still cut (`prd-intake` 1.3.0).
- `scripts/ops/live-test/cancel.py --program` cancelled only the program's workflows and left the program "running" in the console. It now cancels the program the way the console does (`cancel_program`, cascading to its workflows); `--compensate` also runs the console's cancel compensation.
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
