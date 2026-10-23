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

After PRD approval, delivery **forks** into one of two planning tracks (edges on
the `software_delivery` definition):

| Track | Planning phase | Implement skeleton | Best for |
| --- | --- | --- | --- |
| **Jira** | `jira_from_prd` | `implement_delivery` | Teams that plan in Jira epics/stories |
| **Work orders** | `work_orders_from_prd` | `implement_work_orders` | Teams using governed TS work-order planning (`prd-to-work-orders-governed`) |

Both tracks share the same program nodes (`prd` → planning → `implement` → `release`). `implement` advances automatically to `release` once the merge SHA is known and every child program has finished. `rollback` is a startable node for redeploying the last good SHA.

Each change is one **root** program; `content_to_jira_work_orders`, `bug_to_jira_work_orders`, and `jira_to_pr` runs for that change are **child** programs (`parent_program_id`). See [`prd-first-delivery-flow.md`](../architecture/prd-first-delivery-flow.md#one-program-per-change).

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

- Jira secrets in Secrets Manager: `jira-url`, `jira-token`, and `jira-username`.
- Target Jira project `TFS`.

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
| `implement` | "Implement TFS-100 through TFS-104" (Jira track) | `jira_epic_key` |
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
| Jira tickets filed | `pm-agent` | Only in `TFS` and only from approved PRD |
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

Expected: `planning_track=jira`, `jira_from_prd` materialized at `create_jira`.

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
- `pm-agent` creates one epic, stories, and tasks in `TFS`.
- Program artifact `jira_epic_key` is patched on phase completion.

If the PRD would create more than 10 tickets, use P0/P1 priority, or cross project boundaries, the agent must stop and request extra approval.

### A2. Start Development (Jira)

```text
Implement TFS-123. Follow the ticket acceptance criteria, open a PR, and hand it to QA.
```

Expected behavior:

- Program advances to `implement` with `implement_delivery`.
- `dev-agent` fetches the Jira ticket, branches, implements, opens a PR.
- `dev-agent` does not merge.

For an epic, prefer a small controlled batch:

```text
Start the first two implementation-ready tickets under TFS-100. Open separate PRs and hand each one to QA.
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
| `deliver_work_order` | `dev-agent` | `verified-merge-governed` + `run_coding_backend` | OpenCode implement + commit/push/PR incl. host verify; output must include `work_order_id` + `pr_url` |
| `await_pr_checks` | orchestrator | — | Waits for `github.pr_checks` on the PR head SHA; required checks come from `merge_readiness` (see [Merge readiness policy](#merge-readiness-policy)) |
| `review_precheck` | orchestrator (system step) | — | Collects the PR diff, applies `diff_limits`, and checks a declared `## Files` list. See [Code review](#code-review) |
| `code_review` | `review-agent` | `governed-code-review` | Reviews the diff against the work order and returns findings |
| `finalize_code_review` | orchestrator (system step) | — | Computes the verdict, posts the review and `harness/code-review` status; a blocking verdict sends the PR back to `dev-agent` |
| `qa_gate` | `qa-agent` | `governed-qa-gate` | Automatic acceptance review of the delivered PR; emits canonical QA envelope. A `failed` envelope blocks here and the merge wait never arms |
| `ensure_fresh` | orchestrator (system step) | — | Confirms each PR contains its base branch head. See [Branch freshness](#branch-freshness) |
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

When the budget runs out, the blocked step waits for a human instead of failing
the work order. The task gets `_awaiting_human_decision` in its input, the wave
join keeps waiting, the program thread lists the options, and the admin Inbox
shows a **Stuck step** item. Deciding needs the `approve_gate` permission.

| Decision | Chat reply | Effect |
| --- | --- | --- |
| Revise again | `/revise WO-003 <guidance>` | Sends the PR back to OpenCode with the review feedback plus your guidance. Grants a fresh budget of `max_revisions` (the same limit as the original), counted from this revision. |
| Accept as is | `/accept WO-003` | Completes the step with its reviewed output and continues the delivery. For `finalize_code_review` it also sets `harness/code-review` to success, marked as a human override. Not offered for steps that wait on an external event. |
| Stop | `/stop WO-003` | Blocks the step terminally (`stopped_by_human: ...`); the wave counts the work order as failed. |

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
- `code_review` runs `review-agent` with the `governed-code-review` skill. It can only read (`read_file`, `recall_memory`) and returns findings with a severity (`blocker`, `major`, `minor`, `nit`) and a category (`correctness`, `security`, `scope`, `tests`, `maintainability`). `validate_code_review_envelope` checks that the PR set matches and the enums are valid. After a refresh revision the reviewer focuses on the merge delta.
- `finalize_code_review` (system step) merges in the precheck scope findings and computes the verdict: any `blocker`, or a `major` in correctness, security or scope, blocks. The model's own verdict is advisory; a mismatch is a warning. It posts one `COMMENT` review per PR with inline comments and sets the `harness/code-review` commit status. It never approves or requests changes. Posting errors are warnings, not failures.
- A blocking verdict sends the PR back to `dev-agent` with the findings as `review_feedback`, then runs checks, review, QA, freshness and merge again. Code review and QA share the budget of three revisions.
- A code review revision may edit only the files its blocking findings named (`revision_files`), plus tests, test fixtures and BDD feature files or step definitions; OpenCode is told to revert anything else and the run fails with `revision_scope: <files>` if it does not. QA revisions and a human "revise again" have no file limit, since QA defects and guidance often need other files (for example wiring a component into the app).
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

A PR is only offered for merge when it contains the current head of its base branch.

- After QA passes, the `ensure_fresh` system step compares every QA'd PR with its base (`GET /compare/<base>...<head>`). Merged or closed PRs are skipped. A PR that is behind blocks the step with `stale_base: <url> behind_by=N`.
- While a PR waits in `await_human_merge`, a merge of any other PR on the same repo and base branch blocks that wait with `base_advanced` if the PR is now behind. This fan-out runs from the `github.merge` webhook subscriber (even for PRs TS Harness does not own) and from the merge poller.
- Both blocks start a refresh revision of the implement stage on dev-agent with `refresh: true`. `run_coding_backend` in `mode: "refresh"` first asks GitHub to update the branch (`PUT /pulls/<n>/update-branch`). On a conflict it merges the base locally, has OpenCode resolve only the conflicted files, checks that no conflict markers remain, re-runs host verification, then pushes to the same PR. It never opens a new PR.
- The refresh push creates a new head SHA, so checks, code review, QA, `ensure_fresh` and `await_human_merge` run again.
- Refreshes use their own revision budget (`budget: "refresh"`, 5 per workflow). They do not count against the shared code review and QA budget, and those revisions do not count against them. When the refresh budget runs out, the stage stays blocked for a human.

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

`implement` advances automatically to `release` (`deploy_release` 2.0) once `merge_sha` is known and every child program has finished.

| Stage | What it does |
| --- | --- |
| `plan_release` | Resolves the repo and its two environments from `release.environments` (default `staging`, `production`), and records each environment's current SHA as its rollback target |
| `await_ci_on_sha` | Waits for checks on the merge SHA; a failure blocks the release |
| `approve_promote_staging` | Human gate with the environment's `required_roles`; recorded as a `promote` decision |
| `trigger_deploy_staging` | Runs the repo's `workflow_dispatch` workflow or creates a GitHub deployment for the SHA |
| `await_deployment_staging` | Waits for `github.deployment_status` (webhook or `poll_github_deployments`) |
| `record_release_staging` | Marks the release succeeded, or failed with rollback guidance |
| `…_production` | Same four stages for production |

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
