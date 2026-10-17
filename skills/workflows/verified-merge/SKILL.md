---
name: verified-merge
description: Delivers a unit of work through scoping, implementation, local verification, PR review, CI, merge approval, and post-merge verification using a pluggable source adapter. Use when the user provides a Work Order id, Jira key, pasted unit content, or asks to ship, merge, or verify delivery end to end. Resolves source-work-order or ts-source-jira automatically.
type: workflow
disable-model-invocation: true
steps:
  - skill: source-work-order
    when: Input is a Work Order id or planner path, or delivery.source is work-order
  - skill: ts-source-jira
    when: Input is a Jira key or delivery.source is jira
  - skill: superpowers:brainstorming
    external: true
    when: Requirements are ambiguous or missing acceptance criteria
  - skill: superpowers:writing-plans
    external: true
    when: The unit requires multi-step implementation
  - skill: superpowers:using-git-worktrees
    external: true
    when: Creating an isolated implementation workspace
  - skill: superpowers:subagent-driven-development
    external: true
    when: Planned tasks can be worked independently in parallel
  - skill: superpowers:executing-plans
    external: true
    when: Planned tasks are tightly coupled or subagents are unavailable
  - skill: superpowers:test-driven-development
    external: true
    when: Implementing behavior changes
  - skill: superpowers:verification-before-completion
    external: true
    when: Making readiness, passing, fixed, merged, or verified claims
  - skill: superpowers:systematic-debugging
    external: true
    when: Investigating bugs, failed tests, build failures, or CI failures
  - skill: superpowers:requesting-code-review
    external: true
    when: Major features or completed plan tasks need agent review
  - skill: superpowers:receiving-code-review
    external: true
    when: Human review feedback arrives
  - skill: new-branch-and-pr
    external: true
    when: Opening a PR for an existing branch
  - skill: ts-pr-description
    when: Drafting the PR title and body
  - skill: review-and-ship
    external: true
    when: Reviewing the PR before human handoff
  - skill: loop-on-ci
    external: true
    when: Watching PR checks
  - skill: fix-ci
    external: true
    when: CI checks fail
  - skill: babysit
    external: true
    when: Keeping a PR ready while waiting on CI or review
  - skill: get-pr-comments
    external: true
    when: Pull request comments need to be fetched
  - skill: check-compiler-errors
    external: true
    when: Compile or type-check failures need focused diagnosis
  - skill: run-smoke-tests
    external: true
    when: User-facing flows or browser behavior changed
  - skill: verify-this
    external: true
    when: Before/after claims need measurable evidence
  - skill: ts-security-audit
    when: Security-sensitive changes (auth, secrets, input handling) or verify.audits includes security
  - skill: ts-dependency-audit
    when: Dependency manifests or lockfiles changed or verify.audits includes dependency
  - skill: ts-accessibility-audit
    when: UI or markup changed or verify.audits includes accessibility
  - skill: ts-performance-review
    when: Performance-sensitive paths changed or verify.audits includes performance
  - skill: ts-test-audit
    when: Test surface or behavior changed or verify.audits includes test
  - skill: ts-operational-readiness
    when: New service or major operational surface change, or verify.readiness is enabled
  - skill: ts-node-backend-scaffold
    when: Step 5 scaffold resolution matches new backend bootstrap and build.scaffolds is not off
  - skill: ts-frontend-scaffold
    when: Step 5 scaffold resolution matches new frontend bootstrap and build.scaffolds is not off
  - skill: ts-cicd-scaffold
    when: Step 5 scaffold resolution matches net-new CI/CD setup and build.scaffolds is not off
---

# Verified Merge

Deliver one unit of work end to end: resolve a source adapter, fetch normalized context, implement with tests, verify locally, open or update a PR, review, keep CI green, merge only with approval, verify after merge, and sync lifecycle events to the adapter.

This workflow is tracker-neutral. Jira, Work Orders, and future sources are adapters — not hard dependencies.

## Start Here

At invocation, announce:

> "I'm using verified-merge to deliver this unit of work through verified merge."

Then:

1. Read project config from [CONFIG.template.md](CONFIG.template.md) if present.
2. Resolve the source adapter using [adapter-contract.md](adapter-contract.md) routing rules.
3. Invoke the adapter's `fetch` to produce a NormalizedDeliveryContext.
4. Write or update `docs/verified-merge/delivery.json` for the unit.
5. Run the delivery pipeline below, emitting lifecycle events to the adapter at each checkpoint.

Read [adapter-contract.md](adapter-contract.md) before any adapter invocation.

## Governed work-order path (`implement_work_orders`)

For EC2 workflow dispatch, use **`verified-merge-governed`** (this filesystem skill is skipped on
governed dispatch). That path calls `run_coding_backend`, which clones, runs OpenCode, commits,
pushes, and opens the PR — then returns `{ "work_order_id": "...", "pr_url": "..." }`.

Merge completion for `await_human_merge` is external: `POST /webhooks/github` on a merged
`pull_request` event (preferred), or orchestrator `resume_external_event` with
`event_type=github.merge` and matching `pr_url`.

## Prerequisites

| Requirement | Purpose |
|---|---|
| Resolved source adapter | Fetch and sync unit context |
| GitHub CLI (`gh`) | PR creation, checks, merge |
| superpowers plugin | brainstorming, TDD, verification, debugging, planning |
| cursor-team-kit plugin | new-branch-and-pr, review-and-ship, loop-on-ci, fix-ci |
| `ts-pr-description` (this library) | Generate PR titles and descriptions |
| Atlassian MCP (Jira adapter only) | Fetch and transition Jira issues |

See [reference.md](reference.md) for the full dependency table.

## When to Use

Use this workflow when the user:

- Provides a Work Order id such as `WO-003` or a path under `docs/planner/**`.
- Provides a Jira key such as `PROJ-123` and asks to process, ship, merge, or verify it.
- Pastes unit content and asks for end-to-end delivery.
- Says "verified merge", "merged and verified", or "deliver this work order".

Do not use this for PRD creation (`ts-plan-prd`) or PRD-to-Work-Orders planning (`prd-to-work-orders`).

## Hard Gates

- One unit of work per branch and PR.
- Never merge without explicit user approval.
- Never emit `post_merge_verified` before post-merge verification succeeds.
- Never emit `in_development` before acceptance criteria are testable and oversized scope is resolved.
- Never claim ready, fixed, passing, merge-ready, merged, or verified without fresh evidence. **REQUIRED SUB-SKILL:** Use `superpowers:verification-before-completion`.
- Never implement ambiguous requirements by guessing. Use `superpowers:brainstorming` or ask focused questions.
- Never fix bugs, test failures, build failures, or CI failures without root-cause investigation. **REQUIRED SUB-SKILL:** Use `superpowers:systematic-debugging`.
- Never edit `docs/prd-to-work-orders/manifest.json` from this workflow.
- Scaffolds bootstrap **new** structure only; never invoke a build scaffold to modify an existing app (see Step 5).
- Never use `--no-verify`, `--no-gpg-sign`, force-push, or skip CI without explicit user request.
- Prefer updating the same PR for feedback unless the user explicitly asks for a new PR.

## Lifecycle Checkpoints

At each checkpoint:

1. Update `docs/verified-merge/delivery.json`.
2. Invoke the resolved adapter's `on_event` with the event name and payload.
3. Continue the pipeline only after the checkpoint completes.

Events: `context_accepted`, `in_development`, `pr_opened`, `pr_updated`, `ci_green`, `merged`, `post_merge_verified`, `blocked`.

## Pipeline

### 1. Resolve Adapter and Fetch Context

1. Run adapter routing from [adapter-contract.md](adapter-contract.md):
   - Auto-detect from input shape (`WO-###` -> work-order, `PROJ-###` -> jira).
   - Fall back to configured `delivery.source` (default: work-order).
   - Ask if no adapter resolves.
2. **REQUIRED:** Load and follow the resolved adapter skill (`source-work-order` or `ts-source-jira`).
3. Produce a NormalizedDeliveryContext.
4. If fetch fails, ask the user to paste details or fix adapter setup.
5. Emit `context_accepted` and initialize `docs/verified-merge/delivery.json`.

If the user pasted content without an id, treat pasted text as the description and proceed only when acceptance criteria can be made testable.

### 2. Scope and Requirements Check

Classify the unit before implementation:

- Ambiguous acceptance criteria: **REQUIRED SUB-SKILL:** Use `superpowers:brainstorming`.
- Multiple independent subsystems: stop and propose splitting into separate units or PRs.
- Multi-step implementation: **REQUIRED SUB-SKILL:** Use `superpowers:writing-plans`.
- Work Order with unresolved dependencies: stop and report blocking dependency ids.
- Straightforward change with clear acceptance criteria: proceed with a concise implementation plan.

Do not start code changes until scope and acceptance criteria are clear enough to test.

### 3. Mark In Development

When the unit is clear enough to proceed:

1. Emit `in_development`.
2. Let the adapter sync external tracker status if configured.
3. If sync fails, report available options and continue without inventing status.

### 4. Create Isolated Workspace

- **REQUIRED SUB-SKILL:** Use `superpowers:using-git-worktrees` (default).
- If the user prefers branch-only work, create the branch directly.
- Branch name must include the unit id when available.
- Verify baseline before changing behavior.

Step 4 creates the branch or worktree only. PR creation happens in Step 7.

### 5. Implement

**Scaffold Resolution (optional):**

When `build.scaffolds` is not `off`, consult `skills/build/build-scaffold-contract.md` before TDD:

1. Run each registered scaffold's `detect()` against the fetched `NormalizedDeliveryContext` (`ts-node-backend-scaffold`, `ts-frontend-scaffold`, `ts-cicd-scaffold`).
2. **Zero matches** — skip scaffolding; proceed to TDD below (default for most feature Work Orders).
3. **One match** — when `build.scaffolds` is `ask` (default), confirm with the user; when `auto`, invoke immediately; when `off`, skip.
4. **Multiple matches** — ask the user which scaffold to run; do not invoke more than one per unit.
5. **REQUIRED:** Load and follow the selected scaffold skill; record created paths in delivery notes or PR body.
6. Continue to TDD for remaining behavior in the same unit.

Hard rule: scaffolds bootstrap new structure only. Do not invoke scaffolds for incremental feature work on existing apps.

For planned work:

- Independent tasks: **REQUIRED SUB-SKILL:** Use `superpowers:subagent-driven-development`.
- Tightly coupled tasks: **REQUIRED SUB-SKILL:** Use `superpowers:executing-plans`.

For all behavior changes:

- **REQUIRED SUB-SKILL:** Use `superpowers:test-driven-development`.

### 6. Verify Locally

Before opening or updating a PR:

1. Run focused tests for changed behavior.
2. Run compile, type-check, lint, or smoke checks for the risk level.
3. Use `check-compiler-errors`, `run-smoke-tests`, or `verify-this` when applicable.
4. Use `superpowers:verification-before-completion` before any readiness claim.

**Risk-Based Audits (optional):**

When `verify.audits` is configured in project config, invoke the listed audit skills at Step 6. Audits report; this workflow acts on findings.

1. Determine which audits to run from config (`verify.audits`) and change risk (security, deps, UI, perf, tests).
2. **REQUIRED:** Load and follow each configured audit skill (`ts-security-audit`, `ts-dependency-audit`, `ts-accessibility-audit`, `ts-performance-review`, `ts-test-audit`).
3. Each audit writes an AuditReport to `docs/verify/` per `skills/verify/audit-report-contract.md`.
4. Consume reports read-only; record report paths and blocking-eligible finding ids in the PR body or delivery notes.
5. Fix Critical/High findings before merge, or obtain explicit user override documented in the merge approval.
6. Do not skip configured audits without user approval.

When `verify.audits` is empty or unset, skip this sub-step (default behavior unchanged).

**Operational Readiness (optional):**

When `verify.readiness` is enabled in project config, or the unit introduces a new service or major operational surface, invoke `ts-operational-readiness` at Step 6. Readiness reports; this workflow acts on gaps.

1. Determine whether to run readiness from config (`verify.readiness`) and change risk (new service, new deploy surface, missing runbooks).
2. **REQUIRED:** Load and follow `ts-operational-readiness` when enabled or triggered.
3. Readiness writes an OperationalReadiness report to `docs/operate/` per `skills/operate/operational-readiness-contract.md`.
4. Consume the report read-only; record report path and blocking-eligible check ids in the PR body or delivery notes.
5. Resolve Critical/High readiness gaps before merge, or obtain explicit user override documented in the merge approval.
6. Do not skip configured readiness without user approval.

When `verify.readiness` is off or unset, skip this sub-step unless the unit clearly introduces a new service and the user requests readiness (default behavior unchanged).

### 7. Open or Update Pull Request

Use `new-branch-and-pr` if no PR exists. Use `ts-pr-description` for title and body.

The PR body must include:

- Unit id and title.
- Summary of behavior changes.
- Test plan with commands actually run.
- Known gaps or risks.

Emit `pr_opened` with branch and PR URL.

### 8. Agent Review

- Use `superpowers:requesting-code-review` for major features.
- Use `review-and-ship` before human handoff.
- Fix Critical and Important findings before proceeding.

### 9. CI Loop

Use `loop-on-ci` with `gh pr checks` as source of truth.

If checks fail: use `fix-ci`, `superpowers:systematic-debugging`, fix one failure at a time, re-check all PR checks. Emit `ci_green` when all required checks pass.

If the user asks to keep the PR ready while waiting, use `babysit`.

### 10. Human Review and Feedback

Tell the user the PR is ready for review. On feedback: use `get-pr-comments`, `superpowers:receiving-code-review`, update the same PR, re-verify, re-run CI. Emit `pr_updated` when the PR changes materially.

### 11. Merge Gate

Merge only when:

- The user explicitly approved merge.
- Fresh passing local verification evidence exists.
- Required PR checks are green or the user accepts a documented non-blocking status.
- Critical and Important review issues are resolved.
- No unresolved Critical/High audit findings for configured `verify.audits`, or explicit user override documented (see `verify.blocking_severity` in config).
- No unresolved Critical/High readiness gaps when `verify.readiness` is enabled, or explicit user override documented.

Use configured merge strategy. Prefer `gh pr merge --squash` when unspecified.

Emit `merged` with merge commit and PR URL.

### 12. Post-Merge Verification and Completion

After merge:

1. Verify merged result from base branch (see [reference.md](reference.md)).
2. Record verification evidence in `docs/verified-merge/delivery.json`.
3. Emit `post_merge_verified` only after verification succeeds.
4. Let the adapter sync external completion status if configured.
5. If verification fails, emit `blocked` and start the fix workflow.

## Output Contract

Report:

- Unit id, source type, and final delivery status from `delivery.json`.
- Branch name and PR URL.
- Tests and verification commands run, with outcomes.
- Audit report paths and unresolved Critical/High findings when configured audits ran.
- Readiness report path and unresolved Critical/High gaps when configured readiness ran.
- Scaffold skill invoked and paths created when Step 5 scaffold resolution ran.
- Agent review status and unresolved notes.
- CI status, merge status, post-merge verification evidence.
- Adapter sync results (Jira transition, WO mirror) when applicable.
- Remaining risks or explicit user decisions.

## Common Failure Modes

- **Ambiguous unit:** stop and clarify; do not build guesses.
- **Unit too large:** propose split before implementation.
- **Blocking dependency:** stop until prerequisite units are Done.
- **Tests fail:** root cause before fixing.
- **CI fails:** one actionable failure at a time.
- **Premature merge pressure:** explain missing gates and continue verification.
- **Missing external transition:** report available statuses; do not invent one.

## Additional Resources

- Adapter contract: [adapter-contract.md](adapter-contract.md)
- Dependencies and checklists: [reference.md](reference.md)
- Pressure scenarios: [test-scenarios.md](test-scenarios.md)
