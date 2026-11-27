# Governed Delivery Flow Runbook

Use this runbook to operate TS Harness as a governed multi-agent delivery system. The flow keeps humans in control of PRD approval, PR merge, and each promotion to staging and production. Deploys run through the target repo's own GitHub workflow or Deployments API; TS Harness never touches infrastructure.

## Topology is data-defined

Phase order, forks, workflow selection, and artifact handoffs come from the
**program definition**, not hardcoded `PHASE_*` constants:

- Built-in default: [`config/programs/software_delivery.json`](../../config/programs/software_delivery.json)
- Managed overrides: `/workflow-definitions/programs` (see
  [`program-builder.md`](program-builder.md))
- Each running program pins a definition snapshot + workflow versions at start;
  later advances read that pin

After PRD approval, delivery **forks** into one of three planning tracks (edges on
the `software_delivery` definition):

| Track | Planning phase | Implement skeleton | Best for |
| --- | --- | --- | --- |
| **Jira** | `jira_from_prd` | `implement_delivery` | Teams that plan in Jira epics/stories |
| **Work orders** | `work_orders_from_prd` | `implement_work_orders` | Teams using governed TS work-order planning (`prd-to-work-orders-governed`) |
| **Work orders in Jira** | `work_orders_from_prd`, then `file_work_orders_as_jira` | `jira_to_pr` (`implement all <PROJECT>`) | Detailed, unattended delivery tracked in Jira |

Both tracks share the same program nodes (`prd` → planning → `implement` → `release`). `implement` advances automatically to `release` once the merge SHA is known and every child program has finished. `rollback` is a startable node for redeploying the last good SHA.

### Work orders in Jira (`software_delivery` 1.5.0)

Say "file work orders in jira" (or "plan and file in jira", "track in jira") after the PRD.
The program runs the full planning stages (Refinery, Foundry, Planner, each approved), then
files every work order as a Jira ticket (`file_jira`): the work order section unchanged, a
pointer block (`WO:`, `Repo:` from the work order's `Repository:` line, `Program:` the root
program), and Blocks links for its dependencies. The tickets are assigned to the requester
(their sign-in email, through the Settings mapping or Jira's user search), so
`implement all <PROJECT>` lists them and builds them one after another in dependency order.
Each ticket's coding job loads the program's planning documents and design references
through `Program:`. Link the Jira project to a project with its repositories first (System →
Projects) so the planner splits work per repository.

The planner slices for parallel delivery at the right size (`prd-to-work-orders-governed`
1.4.0): shared foundation first, features split along independent seams, coupled pieces kept
in one work order, dependencies only where one work order needs another's code, and no work
order over about 15 files or 12 requirements. Each work order carries a `Complexity:
Low/Medium/High` line; `High` ones are coded on the stronger model, and a filed ticket that
lost the line gets it back from its work order. The Planner approval shows the delivery plan
first ("9 work orders in 3 waves (WO-001 → WO-002, WO-003 → ...)") and warns when several
waves run a single work order or a work order looks too large.

Repositories linked to a project (System → Projects) count as allowed for coding, releases and cleanup without also adding them to the repo allowlist; the GitHub org restriction still applies. A queue or ticket that waits for a repository's bootstrap PR continues on its own a few minutes after the PR merges (the runner checks every 5 minutes); `/retry` runs it at once.

Filing targets the Jira key of the program's project (Project settings), unless the request
names another allowed project; that key must be in `jira_filing.allowed_projects` (System
Settings → Jira filing → Allowed projects), or filing stops and says so.

**Parallel delivery.** `implement all <PROJECT>` builds tickets one at a time when the
project's **Parallel delivery** setting (Project settings; `max_parallel_tickets`, 1-8,
default 2) is 1. Above 1 the `implement` phase runs `implement_jira_pool`: every ticket
whose Jira blockers are done starts as its own `implement_jira_ticket` child, up to the
limit, preferring repositories with nothing running. Each time a child finishes (merged
and closed, or stopped) the pool starts the tickets that became ready, so a slot never
waits for a whole wave. Children post to the program's thread as usual. The pool finishes
when nothing runs and nothing more can start; tickets left behind a stopped blocker are
reported in a blocked step for a person.

A ticket waiting for a person to merge its PR (its checks, code review and QA passed) no
longer holds a slot: when it parks at the merge wait it wakes the pool, which starts the next
ready ticket. At most `TS_HARNESS_POOL_MAX_WAITING_MERGE` (default 4) of a pool's PRs wait for a
merge at once; beyond that nothing new starts until one merges. Tickets that depend on a
waiting PR still start only after it merges.

A ticket is never built twice. The pool skips a ticket another run is already building
(another program, or a single `implement PROJ-7`) and says so ("Left to the run already
building it: PROJ-7"); starting a single ticket that a run is building blocks with "already
being built by another run". The pool keeps waiting, instead of finishing with tickets left
behind, while a remaining ticket is being built elsewhere or waits on one; when that ticket
merges, its run wakes every pool whose queue holds it (`POST /workflow/running-tickets` on
memory-api lists running deliveries and pools).

Three other limits decide how many coding jobs really run at once; raise them with the
project limit:

| Limit | Where | Default |
| --- | --- | --- |
| Host memory (local launcher) | (host memory − 2.5 GB) ÷ `CODING_WORKER_MEMORY` (2 GB) | 2 on an 8 GB host |
| `coding_max_running` | deployment profile / `CODING_MAX_RUNNING` | 3 |
| `coding_per_program` | System Settings, scheduling | 3 |

For 6-7 tickets at once on one host, use about 16 GB of memory for the jobs plus the
reserve (for example a 32 GB, 8 vCPU instance), set `coding_max_running` and
`coding_per_program` to 7, or move coding jobs to ECS ([coding-workers-ecs.md](coding-workers-ecs.md)).

**Refile.** Filing ends the program, but it may run again: in the program's thread say
"refile the work orders in <PROJECT>" (also "refile the tickets", "refile in jira") and
confirm. The filing step runs again from the same work orders. It files a new epic and new
issues (it never reuses the earlier ones), replaces the program's recorded tickets, assigns
them as before, and comments "Refiled as <EPIC>" on any earlier tickets that still exist. It
deletes nothing. Programs created before `software_delivery` 1.6.0 keep the definition they
were created with, so refile is not offered there.

Each change is one **root** program; `content_to_jira_work_orders`, `bug_to_jira_work_orders`, and `jira_to_pr` runs for that change are **child** programs (`parent_program_id`). See [`prd-first-delivery-flow.md`](../architecture/prd-first-delivery-flow.md#one-program-per-change).

## Stopping after the PRD or Jira

A requester does not have to take a program all the way to release. From
`software_delivery` 1.4.0, the `prd` and `jira` nodes are marked `may_end`:

- When the PRD is approved (or the Jira tickets are filed) and nobody has asked for
  the next step, the program is marked **succeeded** and records
  `ended_at_phase`. The console shows it as **Done at PRD** (or **Done at Jira**).
- The thread is told the program is complete and given the phrases that continue
  it: "create jira" or "work orders" after the PRD, "start implementation" after
  Jira. A chat approval carries this in its reply; a console approval posts it to
  the thread.
- Continuing later, in the thread or with **Continue to …** on the program page,
  reopens the same program with its approved PRD and history. The audit log
  records `delivery_program_reopened`.
- Only a program that ended this way can be reopened. One that reached release,
  or was cancelled or failed, is refused: start a new request instead.
- Child programs (started by a parent program) never end early at a `may_end`
  node, so a parent does not continue on a child that stopped at its PRD.

Other program definitions can opt in with **Program may end here** in the
[program builder](program-builder.md). It is refused on terminal nodes and on nodes
whose next step is automatic.

### Programs created before 1.4.0

Programs pin their definition when they start, so older programs still stay
`running` after an approved PRD. Close them once per environment after deploying,
after applying migration
[`045_program_may_end_audit.sql`](../../database/migrations/045_program_may_end_audit.sql):

```bash
python scripts/close-ended-programs.py              # dry run: lists what would close
python scripts/close-ended-programs.py --apply      # closes them
```

It selects root `software_delivery` programs that are `running` at `prd` or `jira`,
whose workflow for that phase succeeded, with nothing running or blocked and no open
child program, and no workflow activity for `--idle-days` days (default 7). Each is
marked succeeded with `ended_at_phase`, audited as
`delivery_program_closed_by_backfill`, and can be continued like a new one. Running
it again closes nothing new. It uses the memory API's database settings
(`TS_HARNESS_DB_*`).

For sign-off testing of the work-orders path, see [`work-orders-delivery-smoke-test.md`](work-orders-delivery-smoke-test.md).
Authoring custom topologies: [`program-builder.md`](program-builder.md).

## Prerequisites

- `multi_agent_enabled = true` in Terraform.
- TS Harness agent-runtime is reachable over Tailscale.
- Apply migrations [`019_delivery_programs.sql`](../../database/migrations/019_delivery_programs.sql) through [`027_program_releases.sql`](../../database/migrations/027_program_releases.sql) before deploy (`scripts/ops/migrate-db.sh`).

Two PRD/delivery programs stay isolated when they have different `project_id`s (for example Project A vs Project B). They share an approved brief only when they are in the same tenant + project (and not excluded) or when a user creates a one-way explicit link.
- GitHub token is available to `dev-agent` for branch, push, and PR operations.
- GitHub branch protection requires the repo's CI checks, `harness/code-review`, and `harness/security-scan`. `harness/independent-review` is posted and stays optional. A human approval is still required, and no agent may approve or merge. See [`github-branch-protection.md`](github-branch-protection.md).
- Tenant bindings, merge readiness, release environments, and wave size are set in [System Settings](system-settings.md#delivery-keys).

**Jira track additionally requires:**

- Jira secrets in Secrets Manager: `jira-url`, plus either `jira-client-id` and `jira-client-secret` for a service account, or `jira-username` and `jira-token`. See [`jira-service-account.md`](jira-service-account.md).
- The target Jira project listed in `jira_filing.allowed_projects` ([System Settings](system-settings.md)).

**Work-orders track additionally requires:**

- Work-order skills present under `skills/workflows/` and `skills/integrate/` (shipped in this repository and the runtime image).
- `pm-agent` assigned `workflows` skill category; `dev-agent` assigned `workflows` + `integrate`; orchestrator assigned `workflows`.

## Enter at any phase (delivery programs)

Users can start mid-pipeline without running earlier phases in the same thread:

| Entry phase | User example | Required artifacts |
| --- | --- | --- |
| `prd` | "Create a governed PRD" | none |
| `jira` | "File Jira from this approved PRD" | `approved_prd` |
| `work_orders` | "Plan work orders from this approved PRD" | `approved_prd` |
| `implement` | "Implement PROJ-100 through PROJ-104" (Jira track) | `jira_epic_key` |
| `implement` | "Deliver WO-001" (work-orders track) | `planner_complete`, `next_work_order_id`, `work_orders_handoff` |
| `release` | "Release the merged change" | `merge_sha` |
| `rollback` | "Roll production back" | `rollback_environment` |

Orchestrator flow: `propose_start_program` → user confirms → `start_program`. Holistic status: `program_status`. Phase transitions: `propose_advance_program` → `advance_program` (or legacy bridge tools after PRD approval).

Program artifacts (contract):

| Artifact | Set when | Used for |
| --- | --- | --- |
| `planning_track` | Fork after PRD (`jira` \| `work_orders`) | Select implement skeleton |
| `approved_prd` | PRD gate | Both planning tracks |
| `jira_epic_key` | Jira phase complete | Jira → implement |
| `planner_complete` | Planner stage complete | Work orders → implement |
| `work_order_ids` | Planner / prepare | WO queue |
| `next_work_order_id` | `prepare_work_order_delivery` | First WO of the current wave |
| `next_work_order_ids` | Implement re-entry | Every WO in the current wave |
| `work_orders_handoff` | Prepare step | Implement entry (work-orders track) |
| `completed_work_order_ids` | Each WO merged | Re-entry vs implement complete |
| `pr_url` / `pr_urls` | PR opened / merged | Merge wait, release inputs |
| `merge_sha` | Last merge of the implement phase | Release entry (CI on this SHA, then promote) |
| `released_environments` | Release complete | Rollback targets |
| `compensation` / `compensation_status` | Program cancelled | Cancel cleanup results and failures |

## Who can see and act on a program

Programs are private to their owner by default. The owner is the signed-in person who started the program (for chat, the directory person linked to the Slack user). The owner shares it from **Programs → Share** with people or teams at one of four levels:

| Level | Allows |
| --- | --- |
| Can view (`read`) | Status, evidence, phase activity, the program's chat |
| Can contribute (`write`) | Advance phases, department memory |
| Can approve (`approve`) | Approve, reject and decide gates (also needs the `approve_gate` permission) |
| Can manage (`admin`) | Cancel, edit project and links, sharing and visibility |

A team approver on a team with `write` or higher gets `approve`. **Public** gives everyone in the tenant `read`. Sharing a program shares its child programs. Harness admins can open any program; each first visit per hour is audited as `program_admin_access`.

`TS_HARNESS_PROGRAM_ACCESS` controls enforcement: `audit` (default) logs `program_access_would_deny` and blocks nothing, `enforce` returns 404 to people who cannot read and 403 to readers who need more, `off` checks only the tenant. Run in `audit` for a week and review the would-deny events before switching to `enforce`.

Gate notifications still go to the thread that started the program. People the program is shared with see it in their Inbox and Programs list, and can approve from there or from a Slack thread once their Slack user is linked. With OIDC, an unlinked Slack user is asked to sign in to the console once with the same email.

## Governance Summary

| Gate | Who Advances It | Agent Boundary |
| --- | --- | --- |
| PRD draft ready | `pm-agent` | No Jira writes; no work-order files until planning track chosen |
| PRD approved | Human | Required before Jira **or** work-order planning |
| Planning track chosen | Human (via reply) | Orchestrator bridges; no auto-pick of Jira after PRD |
| Refinery / Foundry / Planner (WO track) | `pm-agent` + human gates | Uses `prd-to-work-orders-governed`; Validator off critical path |
| Work orders approved | Human (`approve_work_orders`) | Required before prepare handoff |
| Prepare delivery (WO track) | `orchestrator` | Uses `source-work-order`; sets `next_work_order_id` |
| Jira tickets filed | `pm-agent` | Only in `jira_filing.allowed_projects` and only from approved PRD |
| PR opened | `dev-agent` | No merge |
| Code review passed | `review-agent` findings, verdict computed by `finalize_code_review` | Read-only; posts a `COMMENT` review and the `harness/code-review` status, never approves, requests changes or merges |
| QA gate passed | `qa-agent` | No PR approval or merge |
| External checks green | GitHub CI (`github.pr_checks` wait) | Merge wait does not arm until required checks pass on the head SHA |
| Independent review | GitHub reviewer other than the PRD approver | `harness/independent-review` status; required by branch protection |
| PR merged | Human or GitHub rules | Agents wait for confirmation |
| Promote to staging / production | Human with the environment's `required_roles` (`approve_promote_staging`, `approve_promote_production`) | Recorded `promote` decision; TS Harness triggers the repo's deploy, never infrastructure |

## PRD revision (reject loop)

When a human rejects `approve_prd` on a **`prd_request`** workflow, the v1.1
`on_reject` hook creates `revise_prd` and a new `approve_prd` gate dynamically.
Use `workflow_reject_with_feedback` with comments; no bespoke revision code path
is required. After approval, the orchestrator asks how to continue — **Jira tickets or work orders** — then call `propose_advance_program` (or `propose_bridge_workflow`) with the chosen target.

## Work-order planning: answering and revising at each approval

`work_orders_from_prd` (1.3.0) treats the Refinery, Foundry and work-order approvals the
same way as the PRD approval:

- **Approve with notes.** An approval that says more than "approve" (for example
  "Approve. ASM-001 confirmed; ASM-012 is 500 characters; split by phase") is kept as the
  gate's `reviewer_notes`. Foundry reads the Refinery notes, and the Planner reads both
  the Refinery and Foundry notes. The notes outrank the earlier stage's assumptions, and
  the agent names each assumption it changed in its warnings. The earlier stage's
  documents are not rewritten. The console approve route takes the same text as `comments`.
- **Reject with comments.** The stage is redrafted from its earlier output with the
  comments applied, its approval is asked again, and every later stage runs again from
  the new output. Use this when the earlier stage's documents themselves must change.

Notes given when approving the work orders have no later agent stage to read them: to
change the work orders, reject them with comments instead. Programs started before 1.3.0
keep the version they started with.

---

## Shared: Start With Requirements

Send the orchestrator a request like:

```text
Create a Draft PRD from these requirements. Do not create Jira tickets yet.

Requirements:
- <requirement 1>
- <requirement 2>
- <requirement 3>
```

Expected behavior:

- Orchestrator calls `propose_start_workflow(prd_request)` (or `propose_start_program(entry_phase=prd)`).
- Workflow runs `intake` → `draft_prd` → `approve_prd`.
- `pm-agent` returns a Markdown PRD; human gates control approval.
- No Jira tickets or work orders are created until the user chooses a planning track after approval.

## Shared: Review and Revise the PRD

If the PRD needs changes, reply with concrete comments:

```text
Revise the PRD with these comments:
- Make the primary persona the operations manager.
- Add a non-goal that this does not change billing.
- Tighten the acceptance criteria for export failures.
```

Expected behavior:

- Orchestrator routes feedback through the `revise_on_reject` loop.
- `pm-agent` returns a revised draft; loop continues until acceptable.

## Shared: Approve the PRD and Choose Planning Track

When satisfied, approve explicitly:

```text
PRD approved.
```

Expected behavior:

- Orchestrator sets `workflow_phase=approved`.
- Channel ack asks an **open question**: file Jira tickets or generate work orders?
- User reply disambiguates the fork (examples below).

**Choose Jira track:**

```text
Create Jira tickets from this approved PRD.
```

Expected: `planning_track=jira`, `jira_from_prd` materialized at `review_prd`. The story
plan from `plan_stories` waits at `approve_plan`; reply `no` with feedback to revise it.
Nothing is written to Jira until `file_jira` runs after approval.

**Choose work-orders track:**

```text
Plan work orders from this approved PRD.
```

Expected: `planning_track=work_orders`, `work_orders_from_prd` materialized at `refine_requirements`.

Orchestrator tools: `propose_bridge_workflow` → confirm → `bridge_workflow`, or `propose_advance_program` → `advance_program` with explicit `target_phase`.

---

## Track A — Jira Planning and Delivery

### A1. File Jira from Approved PRD

After choosing the Jira track (or entering at `entry_phase=jira`):

Expected behavior:

- `pm-agent` checks for duplicates in Jira.
- `pm-agent` creates one epic, stories, and tasks in the target project from `jira_filing.allowed_projects`.
- Program artifact `jira_epic_key` is patched on phase completion.

If the PRD would create more than 10 tickets, use P0/P1 priority, or cross project boundaries, the agent must stop and request extra approval.

### A2. Start Development (Jira)

```text
Implement PROJ-123. Follow the ticket acceptance criteria, open a PR, and hand it to QA.
```

Expected behavior:

- Program advances to `implement` with `implement_delivery`.
- `dev-agent` fetches the Jira ticket, branches, implements, opens a PR.
- `dev-agent` does not merge.

For an epic, prefer a small controlled batch:

```text
Start the first two implementation-ready tickets under PROJ-100. Open separate PRs and hand each one to QA.
```

### A3. QA, Merge, Deploy (Jira track)

See sections **QA Gate (program stage)**, **Merge Gate**, and **Release and rollback** below. On the Jira track, dynamic implement PRs are aggregated by the `collect_delivery_results` barrier, then a single `qa_gate` reviews the full PR set before `await_human_merge`.

---

## Track B — Work Orders Planning and Delivery

### B1. Planning stages (`work_orders_from_prd`)

Workflow stages (each human gate requires explicit approval):

| Stage | Agent | Skill | Output |
| --- | --- | --- | --- |
| `refine_requirements` | `pm-agent` | `prd-to-work-orders-governed` (Refinery) | JSON `artifacts`: `frds_markdown`, `feature_tree_markdown`, etc. |
| `approve_refinery` | orchestrator | — | Human gate |
| `design_blueprints` | `pm-agent` | `prd-to-work-orders-governed` (Foundry) | JSON `artifacts`: `blueprints_markdown`, `architecture_decisions_markdown` |
| `approve_foundry` | orchestrator | — | Human gate |
| `plan_work_orders` | `pm-agent` | `prd-to-work-orders-governed` (Planner) | JSON `artifacts`: `work_orders_markdown`, `context_graph_markdown`, `execution_phases_markdown`; `work_order_ids` |
| `approve_work_orders` | orchestrator | — | Human gate (execution phase) |
| `prepare_work_order_delivery` | orchestrator | `source-work-order-governed` | Sets `next_work_order_id`, `work_orders_handoff=true` from JSON |

Approve each gate with explicit language (e.g. "approved", "proceed to foundry").

Pass criteria for planning complete:

- Task output includes `planner_complete=true` and `work_order_ids` with at least one `WO-NNN`
- Program artifacts: `planner_complete`, `work_order_ids`, `next_work_order_id`, `work_orders_handoff`

### B2. Deliver work orders in waves (`implement_work_orders` 2.0)

After prepare, the program **auto-advances** to `implement` (`work_orders_from_prd.auto_advance=true`). Skeleton: `implement_work_orders` 2.0.

| Stage | What it does |
| --- | --- |
| `deliver_wave` | Selects up to `delivery.max_parallel_work_orders` (default 3) dependency-ready work orders and forks one `deliver_work_order` child workflow per work order (`parent_workflow_id` links them). Children use the `deliver_work_order` version that was current when the parent started, so activating a new version mid-program does not change later waves |
| `join_wave` | Waits for `workflow.child_terminal`. Each child notifies the parent when it finishes; the `poll-github-merges` cron re-checks joins as a fallback. Resumes when every child succeeded; blocks with the failed work-order IDs when any child failed |

Each `deliver_work_order` child runs the per-work-order pipeline:

| Stage | Agent | Skill / tool | Notes |
| --- | --- | --- | --- |
| `deliver_work_order` | `dev-agent` | `verified-merge-governed` + `run_coding_backend` | OpenCode implement + commit/push/PR incl. host verify; output must include `work_order_id` + `pr_url`. A run that fails for good (no PR) waits for a human: `/revise WO-n <guidance>` runs it again, `/stop WO-n` stops it |
| `await_pr_checks` | orchestrator | — | Waits for `github.pr_checks` on the PR head SHA; required checks come from `merge_readiness` (see [Merge readiness policy](#merge-readiness-policy)). Failing checks send the PR back to `dev-agent` with the end of each failed job's log (`checks` budget, 3 revisions), then wait for a human decision (`verified_merge` 1.7.0) |
| `review_precheck` | orchestrator (system step) | — | Collects the PR diff, applies `diff_limits`, and checks a declared `## Files` list. See [Code review](#code-review) |
| `code_review` | `review-agent` | `governed-code-review` | Reviews the diff against the work order and returns findings |
| `finalize_code_review` | orchestrator (system step) | — | Computes the verdict, posts the review and `harness/code-review` status; a blocking verdict sends the PR back to `dev-agent` |
| `qa_gate` | `qa-agent` | `governed-qa-gate` | Automatic acceptance review of the delivered PR; emits canonical QA envelope. A `failed` envelope blocks here and the merge wait never arms |
| `ensure_fresh` | orchestrator (system step) | — | Sends back PRs that conflict with their base; a PR that is behind but merges cleanly goes on. See [Branch freshness](#branch-freshness) |
| `await_human_merge` | orchestrator | — | Depends on `ensure_fresh`; waits (`match_all` over QA PR set) for `github.merge` via outbound `poll-github-merges` cron, `POST /webhooks/github`, or `resume_external_event` |

**Config:** `implementation.repos.implement_work_orders.url` / `base_branch`, plus Secrets Manager `coding-backend-api-key`, `coding-backend-provider`, and optional `coding-backend-model`. See [implement-work-orders-opencode.md](implement-work-orders-opencode.md).

**Waves:** When a wave completes and more work orders remain, the program **re-enters implement** with the next ready wave (`next_work_order_ids`) — a new `implement_work_orders` workflow under the same `program_id`. `max_parallel_work_orders: 1` delivers one work order at a time. When all WOs are in `completed_work_order_ids`, the program advances automatically to `release` with the last `merge_sha`. In-flight 1.x workflows keep their pinned serial pipeline.

After the PR is merged, the GitHub webhook (or manual resume) continues the wait:

```text
Resume github.merge for the open PR URL.
```

### B3. Merge (work-orders track)

After `qa_gate` passes, Slack asks the human to merge. Then:

1. A reviewer other than the PRD approver approves the PR, so `harness/independent-review` turns green (when governance keeps it on).
2. Human or GitHub merges the PR (or inject `github.merge` event for testing).
3. `await_human_merge` completes and the child workflow finishes; the wave join resumes once every child is done.
4. If more WOs remain, the next wave starts automatically. When all WOs are done, the program advances to `release`.

---

## QA Gate (program stage)

QA is a **program stage** (`qa_gate`), not an ad-hoc dispatch. On both implement
skeletons it runs automatically after delivery and **before** `await_human_merge`:

- **Work orders:** `deliver_work_order`, `await_pr_checks`, code review, `qa_gate`, `ensure_fresh`, `await_human_merge`.
- **Jira:** dynamic implement tasks, `collect_delivery_results` (barrier), then the same checks, code review, QA, freshness and merge stages.

`qa_gate` is **not** host verify. Host verify is the build/lint/typecheck/test run
performed by `run_coding_backend` during `deliver_work_order`, proving the change
compiles and the delivered suite passes on the host. `qa_gate` is an independent
acceptance review of the resulting PR(s) using the `governed-qa-gate` skill; by
default it **reuses the host verify summary as evidence** rather than re-running
the full suite.

Expected behavior:

- `qa-agent` evaluates each delivered PR against its work-item acceptance criteria.
- It returns the canonical QA envelope: top-level `status` is `success` only when
  every per-PR `status` is `success`; the output PR URL set must equal the input
  PR URL set exactly (enforced by `validate_qa_pr_url_set`).
- `qa-agent` does not approve or merge the PR.
- The runner sets the `harness/qa` commit status on each PR head: `pending` ("QA running") before it dispatches the gate, then `success`, `failure` ("QA failed: N blocking of M defects") or `error` when QA could not finish. A human "accept as is" sets it to `success` with "accepted by <actor> (human override)". Like `harness/code-review`, the PR checks wait ignores it unless it is named as a required check.
- On the program page, clicking a stage card shows that stage's activity under the cards: its own steps (with Approve/Reject on a pending gate for `approve_gate` holders) and every workflow it started. For `implement` that is one row per work order with its current step in plain words ("QA running", "OpenCode revising (revision 1)", "Ready to merge", "Stuck at code review: waiting for your decision"), its PR, revision count and expandable step list. The current stage is selected by default and the panel refreshes every 15 seconds while the program runs (`GET /admin/programs/<id>/phases/<phase>/activity`).

**Failure behavior:** a schema-valid `status: failed` produces a **blocked** stage
outcome. `qa_gate.on_block` then runs `revise_on_reject`: it cancels the blocked
gate and the unarmed `await_human_merge`, spawns a new `deliver_work_order` with
`qa_feedback` plus the existing `pr_url`, then new checks, code review, QA,
freshness and merge stages. OpenCode updates the existing PR (same `wo/*`
branch). QA and code review revisions share one budget of three
(`max_revisions: 3`).

```text
QA rejected the PR; sending it back to OpenCode (revision 2/3).
```

**Review depth.** `review_precheck` adds each PR's `review_round` and `review_depth` to the reviewer's input. The first review of a large PR (20 or more files or 1,500 or more changed lines: `TS_HARNESS_DEEP_REVIEW_FILES`, `TS_HARNESS_DEEP_REVIEW_LINES`) is a `full_sweep`: file by file, with the deep checks (input coercion, storage precision, change detection and idempotency, partial failure, concurrency, error paths, boundaries, permissions). Later rounds are `follow_up`: the reviewer gets the last round's blocking findings (`previous_findings`) and the files changed since that review (`changed_since_last_review`, from GitHub's compare), checks each finding is fixed, reviews the changed files in full, and raises only blockers in code the last round already reviewed. When the comparison is not available the round is a `full` review (`governed-code-review` 1.7.0).

**Guided revision.** When code review or QA raises the same finding again after a
revision (same file within 40 lines, or a closely matching message), another plain
revision usually repeats the mistake. Instead, once per budget, the harness asks a
stronger model (`TS_HARNESS_REVISION_DIAGNOSIS_MODEL`, default the PM agent's model)
why the attempts failed. It reads the finding history, each attempt's summary, the
work order and the current code of the files the findings name. The next revision gets
that diagnosis (approach, what not to do, the test that proves it) plus every round's
findings, and runs on the stronger coding model (`TS_HARNESS_GUIDED_CODING_MODEL`,
default the diagnosis model; `off` keeps the usual model). This happens even when the
budget is used up. If the finding still comes back, the step waits for a human. The
stop message then includes the diagnosis and a ready-made `/revise` reply, and says
when the diagnosis thinks the finding is outside the work order. Set
`TS_HARNESS_REVISION_DIAGNOSIS_MODEL=off` to turn guided revisions off.

**Triage at the limit.** When the three automatic revisions are used up, the
harness triages before it asks anyone. A strong model classifies each open finding
(in scope, major or minor, security-sensitive, clear enough to fix) and whether the
rounds are converging. The decision follows the project's **Revisions** setting
(Project settings; `revision_autonomy`):

| Setting | Progress, open findings specific and in scope | Only minor or out-of-scope findings left | Otherwise |
| --- | --- | --- | --- |
| Autonomous (default) | Continue | Accept; each finding becomes a follow-up ticket under the epic, linked to the ticket | Ask |
| Standard | Continue | Ask | Ask |
| Conservative | Ask | Ask | Ask |

"Continue" sends one more revision with consolidated guidance covering every open
finding, on the stronger coding model (`TS_HARNESS_GUIDED_CODING_MODEL`, default Opus
via the PM agent). Every revision runs on the stronger model (`TS_HARNESS_REVISION_CODING_MODEL`), since the coding job's own pre-push review already ran on the first attempt.
Two rules always hand the decision to a person: a finding that touches security
(authentication, authorization, permissions, secrets, tokens or data exposure), and
the ceiling of **8 revisions** per work order. An automatic accept is recorded as a
decision by `harness-triage`. When the harness asks, the message says why it did not
continue and includes the triage's suggested `/revise` text.

When the budget runs out, the blocked step waits for a human instead of failing
the work order. The task gets `_awaiting_human_decision` in its input, the wave
join keeps waiting, the program thread lists the options, and the admin Inbox
shows a **Stuck step** item. Deciding needs the `approve_gate` permission.

| Decision | Chat reply | Effect |
| --- | --- | --- |
| Retry | `/retry WO-003` | Runs the stuck step again with the same input. Use it once the cause is fixed outside the code: a lost coding job, a missing permission or allowlist entry, a service that was down. It does not spend the revision budget. Offered for every stuck step. |
| Revise again | `/revise WO-003 <guidance>` | Sends the PR back to OpenCode with the review feedback plus your guidance. Grants a fresh budget of `max_revisions` (the same limit as the original), counted from this revision. |
| Accept as is | `/accept WO-003` | Completes the step with its reviewed output and continues the delivery. For `finalize_code_review` it also sets `harness/code-review` to success, marked as a human override. Not offered for steps that wait on an external event. |
| Stop | `/stop WO-003` | Blocks the step terminally (`stopped_by_human: ...`); the wave counts the work order as failed. |

**From the PR.** The same three commands work as a comment on the work order's PR:
`/revise <guidance>`, `/accept` or `/stop`. The commenter's GitHub login must be mapped
to their email in System Settings (Identity, GitHub logins); they need the
`approve_gate` permission and, with OIDC, approve access to the program. The harness
replies on the PR with what it did, or why it did not (login not mapped, missing
permission). When no step is waiting for a decision, `/revise` keeps its earlier
meaning at the merge wait (send the PR back with the comment), and `/accept` or `/stop`
does nothing.

**Lost coding jobs.** A coding job whose worker stops sending heartbeats is marked lost
and its step blocks with `transport_error:`. The runner retries such infrastructure failures
3 times on top of the step's own retry budget, with backoff, before it stops for a person
(who can then **Retry**). Redeploys start the coding launcher only after the new
agent-runtime is healthy (`start_stack_launcher_last`), because a worker launched while
agent-runtime is being recreated joins the old container's network and is lost.

**After a restart.** If the runner restarts between a gate blocking and its revise loop
running (a redeploy), the step would stay blocked with no revision and no decision
request. Once a minute the runner's leader replays the block handling for such steps,
after they have been blocked for 90 seconds, so the ticket continues as if nothing had
happened (`workflows/block_recovery.py`).

The work order can be left out of the reply when the thread has only one stuck
step. The Inbox uses `POST /admin/workflows/decide` with
`{workflow_id, task_id, action, guidance}`. With a shared gateway-token
sign-in the server has no personal identity, so the thread says "An admin"
rather than a name; single sign-on (OIDC) records the person.

## Code review

After the PR's checks finish and before QA, every PR gets an automated review by a separate, read-only agent.

- `review_precheck` (system step) reads the PR files from GitHub and applies `program_defaults.merge_readiness.policy.diff_limits`:

  ```yaml
  merge_readiness:
    policy:
      diff_limits:
        max_files: 40
        max_changed_lines: 800        # additions + deletions
        exclude: ["**/*.lock", "package-lock.json", "**/generated/**"]
  ```

  Excluded files are left out of both the limits and the review. A PR over a limit blocks with `diff_limit_exceeded: <url> files=N lines=M` for a human to split or approve; no revision is started. If the work order has a `## Files` (or `## Touches`) bullet list of path globs, each changed file outside it becomes a `major` `scope` finding. Without that list the reviewer judges scope from the In Scope / Out of Scope sections. Files `run_coding_backend` writes itself (`.opencode/**`, `.gitignore`, and the planning docs under `docs/prd`, `docs/refinery`, `docs/foundry`, `docs/planner`) are left out of the limits, the scope check and the review, and counted as `diff_stats.pipeline_files`.
- `code_review` runs `review-agent` with the `governed-code-review` skill. It can only read (`read_file`, `recall_memory`) and returns findings with a severity (`blocker`, `major`, `minor`, `nit`) and a category (`correctness`, `security`, `scope`, `tests`, `maintainability`). `validate_code_review_envelope` checks that the PR set matches and the enums are valid. After a refresh revision the reviewer focuses on the merge delta; after a conflict resolution it reviews only the files in `review_scope`.
- `finalize_code_review` (system step) merges in the precheck scope findings and computes the verdict: any `blocker`, or a `major` in correctness, security or scope, blocks. The model's own verdict is advisory; a mismatch is a warning. It posts one `COMMENT` review per PR with inline comments and sets the `harness/code-review` commit status. It never approves or requests changes. Posting errors are warnings, not failures.
- A blocking verdict sends the PR back to `dev-agent` with the findings as `review_feedback`, then runs checks, review, QA, freshness and merge again. Code review and QA share the budget of three revisions.
- When code review and QA send a work order back in turn, an arbiter (the revision diagnosis model) reads the work order and every round of both gates' feedback and rules on it: independent problems are all fixed, and a real contradiction is settled by the acceptance criteria (then correctness over style). The next revision runs with one consolidated instruction (`_arbitration` on the task). A person is asked only when the project's `revision_autonomy` is `conservative`, the work order has had 8 revisions, a finding touches security, the work order does not settle the dispute (a product decision), or no arbiter model is available.
- A revision of an existing PR first merges the latest base branch (`coding_revision_base_merged`); when that merge conflicts, it is aborted and OpenCode is told to merge and resolve it first (`coding_revision_base_conflict`). The PR is a draft while the revision runs (`gh pr ready --undo`) and is marked ready when it ends. A new commit pushed to a PR while its CI wait is armed moves the wait to that commit (GitHub `pull_request` `synchronize`).
- A PR waiting for CI that conflicts with its base branch gets no CI from GitHub. The `poll-github-merges` system job checks every minute and blocks such a wait with `external_blocked:github.pr_checks base_conflict`; the stage's revision then merges the base, resolves the conflicts, runs the checks and pushes, and the re-gate waits for CI on the new head.
- A code review revision is told to edit the files its blocking findings named (`revision_files`), plus tests, test fixtures and BDD feature files or step definitions. Files it changes beyond those are kept, logged as `coding_revision_out_of_scope` and listed in the job result (`out_of_scope_files`); the code-review gate reads the whole diff and decides whether they belong. (They used to be reverted by another OpenCode session, which also undid correct fixes.) A revision that leaves the branch identical to the reviewed head fails with `revision produced no changes` instead of going back to review unchanged. QA revisions and a human "revise again" have no file limit, since QA defects and guidance often need other files (for example wiring a component into the app).
- A revision must implement: `run_coding_backend` refuses `mode: "refresh"` unless the delivery task was created with `refresh: true` (the freshness and merge loops), so a review or QA revision cannot pass by only merging the base branch.

```text
Code review blocked the PR; sending it back to OpenCode (revision 1/3).
```

Branch protection must require `harness/code-review`. TS Harness posts that status and never approves or requests changes.

## Merge Gate

After QA passes, Slack posts the QA summary plus a merge ask (no generic “let me know if you want to continue” footer):

```text
QA passed. Please merge the pull request. I'll continue automatically after GitHub merge.
- WO-001: <PR URL>
```

Expected behavior:

- A human or GitHub branch protection merges.
- No agent merges the PR.
- The merge wait arms only after the required checks passed on the PR head SHA. A revision push creates a new head SHA and a new checks wait.
- Branch protection requires `harness/code-review` and `harness/security-scan`. TS Harness posts `harness/independent-review` and that status stays optional: it is `success` only when an approving reviewer is not the PRD approver. The setting is pinned per program; see [`github-branch-protection.md`](github-branch-protection.md). No agent may approve or merge.
- On the work-orders track, merge completion finishes the child workflow and may complete the wave.

## Merge readiness policy

`await_pr_checks` decides which checks must pass from `program_defaults.merge_readiness`:

```yaml
merge_readiness:
  required_checks:                 # per-repo check names (always honoured)
    acme/app: ["ci / test"]
  policy:
    mode: warn                     # warn (default) | enforce
    required_categories: [build, lint, test, security]
    categories:                    # repo -> category -> check names
      acme/app:
        build: ["ci / build"]
        lint: ["ci / lint"]
        test: ["ci / test"]
        security: ["codeql", "dependency-review"]
    missing_check_grace_seconds: 900
    require_branch_protection: true
    host_security:                 # see "Host security scan"
      secret_scan: true            # cannot be disabled in enforce mode
      dependency_audit: true
      audit_severity: high         # low | moderate | high | critical
```

- Required checks are the `required_checks` names plus every name under `categories` for the repo. Without either, every check reported on the head SHA must pass, as before.
- Without a `policy` block nothing else changes.
- With a `policy`, a category in `required_categories` with no mapped check is recorded as a `policy_incomplete` warning on the PR's program ref. In `enforce` mode the wait does not arm; the stage blocks with `wait_external setup failed: policy_incomplete:...` for a human to fix the configuration.
- If branch protection does not require every mapped check, or does not require `harness/code-review` and `harness/security-scan`, that is a warning. In `enforce` mode with `require_branch_protection: true`, it blocks the same way. `harness/independent-review` stays optional. Do not change the default `mode` (`warn`) or `require_branch_protection` (`false`); an org tenant turns both on.
- A required check that never reports fails the wait with `required_check_missing:<name>` once every other required check has finished and `missing_check_grace_seconds` has passed since the wait armed. Without a `policy`, a missing check stays pending until the wait deadline.
- The checks output carries `policy`, `category_results` (worst state per category) and `policy_warnings`, which flow into the QA envelope and evidence export.

## Host security scan

Host verification in `run_coding_backend` scans every change before it is pushed: on first delivery, on code review and QA revisions, and on refreshes that merge locally.

- **Secret scan (always on).** Only lines the change adds relative to `origin/<base>` are scanned. With `gitleaks` on `PATH` (it is in the images), those lines are scanned with `gitleaks dir --redact`. Without it, a built-in scanner looks for AWS access keys, GitHub and Slack tokens, PEM private keys, and high-entropy `key` / `secret` / `token` / `password` assignments. Both honour `.gitleaksignore` entries of the form `path:rule:line`. A finding adds the verify reason `secrets_detected`, and OpenCode gets a fix pass telling it to move the value to configuration or an environment variable. Logs, prompts and the PR body show file and line only, never the value.
- **Dependency audit.** Runs only when a manifest or lockfile changed (`requirements*.txt`, `pyproject.toml`, `poetry.lock`, `uv.lock`, `package.json`, `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`). It uses `osv-scanner` when present, otherwise `pip-audit` and `npm audit`. Vulnerabilities at or above `audit_severity` add `vulnerable_dependency` and a fix pass asking for a patched version. `pip-audit` reports no severity, so all of its findings count. A missing scanner, or one that cannot reach its advisory source, is the warning `dependency_audit_unavailable`. Verify still records that warning and does not start a fix loop. `harness/security-scan` is posted as `failure`, so branch protection blocks the merge. `no_manifest_changes` stays `success`.
- The result goes into the tool output and implement stage output as `security`, and into the PR body as a "Security scan" section. When `await_pr_checks` pins the PR head it publishes `harness/security-scan` on that SHA: `success`, or `failure` with the finding counts. With `require_branch_protection: true`, branch protection must require that status.
- Settings: `host_security` lives under `merge_readiness.policy`. `secret_scan: false` is rejected when `mode: enforce`.

## Minimum CI for a TS Harness repo

The host scan is a floor, not a replacement for repo CI. Every repo TS Harness delivers to should run, on pull requests:

| Category | Suggested checks |
| --- | --- |
| `build` | Compile or package the project |
| `lint` | The repo's linter and formatter check |
| `test` | The unit and integration suite |
| `security` | Two separate jobs: a secret scan (for example `gitleaks`) and a dependency audit (for example `dependency-review` or `pip-audit`) |

`security` is two GitHub checks, not two steps of one job. A secret-scan failure must not skip the dependency-audit job. Map each job's check name under `categories.security`. The names below are examples; TS Harness does not hard-code `security` or `dependency-audit`.

```yaml
merge_readiness:
  policy:
    mode: enforce
    require_branch_protection: true
    required_categories: [build, lint, test, security]
    categories:
      owner/repo:
        build: [build]
        lint: [lint]
        test: [test]
        security: [security, dependency-audit]
```

Map the check names per repo under `merge_readiness.policy.categories` (see [Merge readiness policy](#merge-readiness-policy)) and make them required in branch protection, along with `harness/code-review` and `harness/security-scan`. `security` is in the default `required_categories`, so a repo without a mapped security check gets a `policy_incomplete` warning in `warn` mode and blocks in `enforce` mode.

## Branch freshness

A PR is brought up to date with its base only when it conflicts. A PR that is behind but merges cleanly goes on to the merge wait as it is. How the code that lands on the base gets tested together depends on the repo (see [Merge-time freshness per repo](#merge-time-freshness-per-repo)).

- After QA passes, the `ensure_fresh` system step reads each QA'd PR (`GET /compare/<base>...<head>` and the PR's `mergeable`). GitHub computes `mergeable` after the base moves, so the step reads the PR up to 4 times, 2 seconds apart. Each PR is `fresh`, `behind` (merges cleanly), `behind_unknown` (GitHub has not decided yet), `conflict` or `skipped` (merged or closed). Only `conflict` blocks the step, with `stale_base: <url> behind_by=N conflict`. The output also has each PR's merge mode.
- While a PR waits in `await_human_merge`, a merge of any other PR on the same repo and base branch blocks that wait with `base_advanced` if the PR now conflicts. This fan-out runs from the `github.merge` webhook subscriber (even for PRs TS Harness does not own) and from the merge poller. GitHub usually has not computed `mergeable` yet when the merge arrives, so the poller (`poll-github-merges`, every minute) also checks every PR waiting to merge and sends back the ones GitHub reports as conflicting (`send_back_conflicting_merges`). It compares only those, so it costs one PR read per waiting PR per minute.
- Both blocks start a refresh revision of the implement stage on dev-agent with `refresh: true`. `run_coding_backend` in `mode: "refresh"` first asks GitHub to update the branch (`PUT /pulls/<n>/update-branch`). On a conflict it merges the base locally, has OpenCode resolve only the conflicted files, checks that no conflict markers remain, re-runs host verification, then pushes to the same PR. It never opens a new PR.
- After a refresh, checks run again. Code review and QA reuse their last passing verdict when the PR's own diff is unchanged (`carry_forward`, `TS_HARNESS_CARRY_FORWARD_VERDICTS`). After a conflict resolution, `review_precheck` sets `review_scope` to the PR files that differ from the last passing review, and the reviewer reviews only those (both sides kept, no markers left, the work order still met). QA runs again in full.
- `merge_readiness.refresh: always` in program defaults restores the earlier behaviour: any PR that falls behind is refreshed, conflict or not.
- Refreshes use their own revision budget (`budget: "refresh"`, 5 per workflow). They do not count against the shared code review and QA budget, and those revisions do not count against them. When the refresh budget runs out, the stage stays blocked for a human.

### Merge-time freshness per repo

TS Harness runs on any GitHub plan, so it finds out per repo and base branch how a PR is kept current at merge time. It only reads settings, caches the answer for an hour, and never changes them. A read that fails (a plan without the feature, or no permission) means the `harness` mode, never a failed run.

| Mode | Detected from | What happens | Enforced by GitHub |
| --- | --- | --- | --- |
| `merge_queue` | A ruleset with a merge queue (`GET /repos/<o>/<r>/rules/branches/<base>`, readable with read access) | GitHub tests the PR with the latest base before merging it. TS Harness does nothing extra. | Yes |
| `require_up_to_date` | A ruleset whose required status checks are strict, or classic branch protection with `strict` required checks (reading classic protection may need admin access) | GitHub blocks merging a PR that is behind; a person clicks **Update branch** and CI runs. | Yes |
| `harness` | Neither of the above, or the settings can't be read | Once a person approves the PR on GitHub, the poller updates a branch that is behind (server-side merge) and posts `harness/up-to-date` on the new head. Before approval the status is pending: "Behind <base>; merges cleanly. Brought up to date once approved." | No: merging earlier is not blocked |

- The **Ready to merge** step on the ticket card says which mode applies, and for `harness` that merging early is not blocked and that branch protection or a merge queue would enforce it.
- A merge queue set up through classic branch protection rather than a ruleset is not visible to the rules API, so it is detected as one of the other modes. Set the override for those repos.
- Override detection in program defaults: `merge_readiness.merge_mode: harness` (every repo) or `{"default": "auto", "org/repo": "merge_queue"}`. Values: `auto` (the default), `merge_queue`, `require_up_to_date`, `harness`.
- When a waiting PR's branch is updated (**Update branch** or the harness), its new head has none of the harness statuses. On the `synchronize` webhook, TS Harness copies `harness/code-review`, `harness/qa`, `harness/security-scan` and `harness/independent-review` from the old head when the PR's own changes are still the ones reviewed (same patches, ignoring hunk positions; `change_identity` from `review_precheck`). Required harness statuses then don't block the merge. If the PR's own changes differ, nothing is copied.

## Human rejection

While a PR waits in `await_human_merge`, the merge poller (`poll-github-merges`, every minute) also reads reviews and comments. A merge always wins.

| Signal on the PR | What happens |
| --- | --- |
| Closed without merging | The wait blocks with `external_blocked:github.pr_closed by <login>`. The work order stops (no revision), its wave fails, and the bound channel gets "<login> closed the PR without merging; the work order is stopped." |
| "Request changes" review on the current head commit | The wait blocks with `external_blocked:github.pr_changes_requested by <login>`. The review body and its inline comments (`path:line: comment`) go to OpenCode as `review_feedback`, on the same PR, then checks, code review, QA, freshness and the merge wait run again. |
| Comment starting with `/revise` posted after the current head commit | Same as "Request changes", with the text after `/revise` as the feedback. Use it when you cannot request changes, for example on a PR opened by your own token account. |

- Only reviewers and commenters with write access (`write`, `maintain` or `admin`) count. TS Harness's own reviews are `COMMENT` reviews and never count.
- Both signals are tied to the head commit, so the revision's push makes them stale. Review the new commit to reject again; a later approval or dismissal clears a "Request changes".
- Human requests amend the work order. Code review, QA, and every later OpenCode revision receive them as `human_change_requests` (oldest first, from `$runtime.human_requests:<implement stage>`); where a request conflicts with the work order or an earlier request, the latest one wins.
- Human revisions use their own budget (`budget: "human"`, 3 per workflow), separate from the code review/QA and refresh budgets. When it runs out the stage stays blocked for a human.
- Reopening a closed PR does not restart the work order.
- A revision only updates the PR it was started for. If that PR is closed or merged before the revision pushes, the revision fails with `pr_closed:` (or `pr_merged:`) and the work order stops; it never opens a replacement PR.

## Release and rollback

`implement` advances automatically to `release` (`deploy_release` 3.0) once `merge_sha` is known and every child program has finished.

`deploy_release` starts one `release_track` workflow per repository that merged work in the program (`plan_release_tracks`) and waits for all of them (`join_tracks`). A single-repository program gets one track, which behaves like the 2.0 release. Each track releases the latest merge SHA of its repository.

Tracks are ordered by work-order dependencies. When a work order in `acme/web` depends on one in `acme/api`, the `acme/web` track waits before each environment until `acme/api` has recorded a successful release there (the program page shows "Waits for api production"). Tracks with no dependency between them release in parallel. If an upstream track fails, only the tracks that depend on it block; the others carry on. A dependency cycle between repositories blocks `plan_release_tracks` before any track starts. Missed notifications are picked up by `poll_release_upstreams`, which runs with the GitHub merge poller.

Each track runs these stages:

| Stage | What it does |
| --- | --- |
| `plan_track` | Resolves the repo and its two environments from `release.environments` (default `staging`, `production`), and records each environment's current SHA as its rollback target |
| `await_ci_on_sha` | Waits for checks on the merge SHA; a failure blocks the release |
| `await_upstream_staging` | Waits until every repository this one depends on has released to staging; immediate when there are none |
| `approve_promote_staging` | Human gate with the environment's `required_roles`; recorded as a `promote` decision |
| `trigger_deploy_staging` | Runs the repo's `workflow_dispatch` workflow or creates a GitHub deployment for the SHA |
| `await_deployment_staging` | Waits for `github.deployment_status` (webhook or `poll_github_deployments`) |
| `record_release_staging` | Marks the release succeeded, or failed with rollback guidance |
| `…_production` | Same five stages for production, starting with `await_upstream_production` |

A program or node may set `required_permission`. When it is omitted, starting and advancing still require `start_program`, and reads stay on `view`. A node value wins for the node being entered. Add a custom name to a role bundle; the default `admin` and `owner` bundles receive every name listed there unless that role's bundle is replaced:

```yaml
identity:
  role_bundles:
    delivery_lead: [view, start_delivery]
```

```json
{ "id": "implement", "required_permission": "start_delivery" }
```

A release environment may set `required_permission` as well. The approver still needs `approve_gate`; the extra name is required in addition. Existing programs do not name a new permission.

Releases are stored in `program_releases`. If a deploy fails, start the `rollback` node (`rollback_release`) with `rollback_environment`; it redeploys the recorded rollback SHA behind `approve_rollback`. TS Harness never touches infrastructure directly.

## Status Command

At any point, ask:

```text
Give me program status.
```

Or with context:

```text
Give me governed delivery status for this thread.
```

Expected response includes (via `program_status` / `workflow_status` operability fields):

```text
Delivery program status:
- Current phase: prd | jira | work_orders | implement
- definition_version: <skeleton version>
- active_step / active_task_status
- waiting_on.kind: approval | external | agent
- waiting_on.event_type / age_seconds / match
- waiting_on.escalated / escalate_at / expires_at
- lease.expires_at / lease.expired
- pr_urls / qa_status
- last_error / recommended_action
- compensation_status / failed_stage (when technical failure compensation ran)
- compensation (cancel cleanup status, actions, failures)
- legacy_no_qa_gate: true for already-materialized pre-QA graphs

Narrative mapping operators can use:
- QA: derived from qa_status (success|failed|missing)
- Merge: waiting_on.kind=external + github.merge, or merged when wait cleared
- Release: `release` phase; promotions wait on approve_promote_* gates

Operator path when stuck:
1. Call program_status / workflow_status and read waiting_on + recommended_action.
2. For external waits, also use find_external_waits / merge poller
   (`poll-github-merges` and `escalate-stale-waits` are system jobs in
   config/runtime-cron-jobs.json, materialized by scripts/phases/15-automation-cron.sh).
3. For expired leases, rely on workflow-runner stale recovery.
4. For blocked QA, the runner sends the PR back to OpenCode with qa_feedback.
   If the revision limit is reached, read the findings and decide in the Inbox
   or the thread: revise again, accept as is, or stop (see Code review above).
```

## Wait deadlines and escalation

External waits escalate after their `timeout_seconds` (or `escalate_after_seconds`) and expire only when the stage sets `expires_after_seconds`. Approval gates take both deadlines from `approval.expires_after_seconds`, counted from when the gate became ready. Merge waits set no expiry, so they escalate and never fail.

The `escalate-stale-waits` system job runs every 15 minutes:

- A wait past `escalate_at` is announced in the bound Slack thread (or the parent workflow's thread for wave children), mentioning the users who hold `escalate_to_role`. Audit: `workflow_wait_escalated`.
- A wait past `expires_at` stays blocked. The job records a `recommended_action` on the task (for example `review_and_merge_or_cancel_pr` or `decide_or_reassign_expired_gate`), which `program_status` and the portfolio board show. Audit: `workflow_wait_expired`.

## Cancel and cleanup

Cancelling a program (Admin Console **Cancel**, `POST /admin/programs/{id}/cancel`) cancels its active workflows and child programs, then runs cancel compensation over `program_external_refs` for the whole tree:

| Ref | Action |
| --- | --- |
| Jira issue | Adds the `harness-cancelled` label and a comment. Transitions with `compensation.jira_close_transition` only when `compensation.jira: close` |
| Open PR | Comments and closes. Merged PRs are left alone |
| Branch | Deleted only when TS Harness created it, its PR is closed and not merged, and `compensation.delete_branches: true` (default false) |
| Deployment | Never touched; use `rollback` |

Cancel needs the admin or owner role. PRs and branches in repos outside the tenant binding (`github.org`, `repo_picker`) are skipped as `repo_not_bound`, and with no binding configured nothing on GitHub is closed or deleted. Jira issues outside `jira_filing.allowed_projects` are skipped as `project_not_bound`.

The result (`completed`, `partial`, `failed`, or `skipped`) and every failure are stored in the program's `compensation` artifact and audited as `program_compensation_recorded`. The cancel response and program detail show them; a partial result sets `recommended_action: retry_failed_cancel_compensation`.

## Evidence export

Admin Console **Download evidence** on a program (`GET /admin/programs/{id}/evidence`, admin or owner only; another tenant's program returns 404) downloads `program-<id>-evidence.json`: the governance record for the program and all its child programs.

| Section | Contents |
| --- | --- |
| `program`, `children`, `workflows` | Program rows (with pinned definition) and every workflow in the tree |
| `decisions` | Gate approvals, rejections, and overrides with actor, roles, and reason |
| `pull_requests`, `jira`, `other_refs` | `program_external_refs`: PRs/branches with check and merge metadata, Jira issues, deployments |
| `releases` | Environment promotions and rollbacks |
| `compensation` | Cancel cleanup outcome, if cancelled |
| `audit` | `session_audit_log` rows whose `details` reference the tree's program, workflow, or task ids, oldest first (`audit_truncated: true` past 5000 rows) |

The file is canonical JSON. `sha256` is the SHA-256 of the document with the `sha256` field removed, serialized with sorted keys and `(",", ":")` separators; it is also sent as `X-Evidence-SHA256` and shown (first 12 characters) in the console. Verify a file:

```bash
python3 -c 'import hashlib,json,sys; d=json.load(open(sys.argv[1])); h=d.pop("sha256"); print("ok" if hashlib.sha256(json.dumps(d,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()==h else "MISMATCH")' program-<id>-evidence.json
```

Each export is audited as `program_evidence_exported` with the hash and the exporting user; if that audit write fails the download fails too. Requires migration `028_audit_details_program_index.sql`.

## Pre-pilot checklist

Before the first real program on the pilot account:

- [ ] Migrations applied through `028_audit_details_program_index.sql` (`scripts/ops/migrate-db.sh`).
- [ ] Branch protection, GitHub App permissions, and `GITHUB_WEBHOOK_SECRET` set per [`github-branch-protection.md`](github-branch-protection.md).
- [ ] Tenant bindings set (`github.org` / `repo_picker`, `jira_filing.allowed_projects`) per [`system-settings.md`](system-settings.md#delivery-keys); without them release planning and cancel cleanup skip GitHub.
- [ ] `terraform apply` in `infra/aws` created the `system-jobs-stale` alarm, and the `alerts` SNS subscription is confirmed.
- [ ] `/healthz` shows `scheduler_jobs` `ok` a few minutes after boot.
- [ ] RDS restore drill passed once: [`db-restore.md`](db-restore.md#rds-snapshot-restore-drill-run-once-before-the-pilot).
- [ ] An evidence export from a trial program verifies with the command above.

## Failure Handling

- If requirements are vague, `pm-agent` asks clarifying questions instead of drafting speculative tickets or work orders.
- If Jira is unavailable, stop at the approved PRD and retry Jira creation later — or switch to the work-orders track if appropriate.
- If work-order planning fails a gate, reject with feedback; `pm-agent` revises the relevant stage output.
- If `prepare_work_order_delivery` finds no ready WO, orchestrator reports dependency blockers from `source-work-order` — do not advance to implement.
- If a ticket or work order lacks acceptance criteria, return to `pm-agent` or the human before implementation.
- If tests fail, route back to `dev-agent`.
- If QA fails twice for the same reason, escalate to the human with the failure history.
- If a deploy fails, the release records the failure with the rollback SHA; start `rollback` for that environment.
- If a wave work order fails, `join_wave` blocks with the failed work-order IDs; fix or cancel that child, then resume.

## Related docs

- Architecture: [`docs/architecture/prd-first-delivery-flow.md`](../architecture/prd-first-delivery-flow.md)
- Program builder: [`program-builder.md`](program-builder.md)
- Branch protection: [`github-branch-protection.md`](github-branch-protection.md)
- Delivery settings: [`system-settings.md`](system-settings.md#delivery-keys)
- Compatibility / cutover: [`../compatibility.md`](../compatibility.md)
- Work-orders smoke sign-off: [`work-orders-delivery-smoke-test.md`](work-orders-delivery-smoke-test.md)
- Orchestrator skill: [`skills/orchestrator/governed-delivery.md`](../../skills/orchestrator/governed-delivery.md)
- Skills catalog: [`skills/README.md`](../../skills/README.md)
