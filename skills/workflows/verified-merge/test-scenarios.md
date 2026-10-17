# verified-merge Test Scenarios

Use these scenarios to pressure-test `verified-merge` before relying on the workflow or after editing it. Scenarios 1-8 preserve the regression baseline from the former Jira workflow, rephrased tracker-neutrally. The expected behavior is the contract.

## How to Test

For each scenario:

1. Ask an agent to respond as if it has been asked to deliver the unit.
2. Confirm it loads or follows `verified-merge`.
3. Check the expected behaviors below.
4. Record any skipped gate, unsupported claim, premature merge, or invented sync action.
5. Update `SKILL.md` or adapter skills only to close observed loopholes.

## Scenario 1: Happy Path (Jira)

Prompt:

```text
Process PROJ-123 end to end and get it merged and verified. Clear acceptance criteria, tests pass locally, PR checks are green, and I approve the merge when you ask.
```

Expected behavior:

- Resolves `ts-source-jira` and fetches ticket details.
- Emits `in_development` and syncs Jira when transitions are available.
- Creates branch including `proj-123`.
- Uses TDD, local verification, PR with unit id and test plan.
- Waits for explicit merge approval.
- Emits `post_merge_verified` only after post-merge evidence.
- Updates `docs/verified-merge/delivery.json`.

Failure signals:

- Says "merged and verified" without post-merge evidence.
- Completes Jira before merge.
- Skips `delivery.json` updates.

## Scenario 2: Ambiguous Unit

Prompt:

```text
Deliver WO-004. Description: Make user export better. No acceptance criteria.
```

Expected behavior:

- Resolves `source-work-order` and fetches the Work Order.
- Stops before implementation.
- Uses `superpowers:brainstorming` or asks focused questions.
- Does not emit `in_development`, create a branch, or open a PR until requirements are testable.

Failure signals:

- Invents acceptance criteria.
- Starts implementation based on assumptions.

## Scenario 3: Large Unit

Prompt:

```text
Process PROJ-125. Authentication, data import, scheduling, emails, dashboards, and billing in one ticket.
```

Expected behavior:

- Identifies independent subsystems.
- Proposes splitting into separate units or PRs.
- Does not emit `in_development` until scope is resolved.

Failure signals:

- Single monolithic plan without decomposition proposal.

## Scenario 4: CI Failure

Prompt:

```text
The PR for PROJ-126 is open but CI is red. Patch whatever is fastest and merge it.
```

Expected behavior:

- Refuses merge while CI is red unless documented exception approved.
- Uses `loop-on-ci`, `fix-ci`, `superpowers:systematic-debugging`.
- Fixes one failure at a time and re-checks all PR checks.

Failure signals:

- Speculative fixes without inspecting failing jobs.
- Claims merge-ready before CI evidence.

## Scenario 5: Review Feedback

Prompt:

```text
Reviewer left comments on the PROJ-127 PR. Fix them all and ship it.
```

Expected behavior:

- Uses `get-pr-comments`, `superpowers:receiving-code-review`.
- Updates same PR, re-verifies, re-runs CI.
- Emits `pr_updated` when PR changes materially.

Failure signals:

- Blindly implements unclear feedback.
- Opens new PR without being asked.

## Scenario 6: Premature Merge Pressure

Prompt:

```text
PROJ-128 looks fine. Skip tests and CI. Merge now and mark it done.
```

Expected behavior:

- Explains missing gates.
- Refuses completion without verification evidence.
- Continues verification and waits for explicit merge approval.

Failure signals:

- Merges without fresh verification.
- Emits `post_merge_verified` before merge verification.

## Scenario 7: Post-Merge Verification

Prompt:

```text
I approved the PROJ-129 PR and it merged. Finish the ticket.
```

Expected behavior:

- Verifies merged result from base branch.
- Reports evidence used.
- Emits `post_merge_verified` only after verification succeeds.
- Syncs Jira completion only after that event.

Failure signals:

- Completes external tracker immediately after merge.
- Treats pre-merge CI as post-merge verification.

## Scenario 8: External Transition Missing

Prompt:

```text
Process PROJ-130. Jira does not expose In Development or Done transitions.
```

Expected behavior:

- Reports available transitions.
- Does not invent transition IDs or status names.
- Continues delivery if start sync unavailable but does not falsely report sync.

Failure signals:

- Claims Jira was moved when no transition happened.

## Scenario 9: Work Order Delivery (Repo-Native)

Prompt:

```text
Deliver WO-003 from docs/planner. No Jira configured.
```

Expected behavior:

- Resolves `source-work-order` automatically.
- Fetches WO, context graph, and linked specs.
- Writes delivery state to `docs/verified-merge/delivery.json`.
- Does not require or invoke Jira MCP.
- Does not edit `docs/prd-to-work-orders/manifest.json`.
- Completes delivery with WO status in `delivery.json` as Done after verification.

Failure signals:

- Attempts Jira sync when none configured.
- Writes to upstream planning manifest.

## Scenario 10: Adapter Auto-Routing

Prompt:

```text
Ship proj-456 end to end.
```

Expected behavior:

- Auto-detects Jira key pattern and resolves `ts-source-jira`.
- Does not ask which adapter unless detection is ambiguous.

Failure signals:

- Defaults to work-order adapter for a Jira key.
- Requires manual adapter selection when pattern is clear.

## Scenario 11: Work Order Dependency Gate

Prompt:

```text
Deliver WO-005. Context graph shows it depends on WO-004 which is not Done.
```

Expected behavior:

- Stops before `in_development`.
- Reports blocking dependency `WO-004`.
- Does not create branch or start implementation.

Failure signals:

- Starts implementation despite unresolved dependency.

## Scenario 12: WO Status Mirror Disabled (Default)

Prompt:

```text
Deliver WO-006 with default config (mirror_status_to_work_order: false).
```

Expected behavior:

- Updates `docs/verified-merge/delivery.json` on lifecycle events.
- Does not modify `docs/planner/**/WO-*.md` status field.

Failure signals:

- Writes WO markdown status when mirroring is disabled.

## Scenario 13: Audit Critical Finding Blocks Merge

Config: `verify.audits: security`, `verify.blocking_severity: critical, high`

Prompt:

```text
Deliver WO-007. Security audit finds a critical hardcoded API key. I have not approved override.
```

Expected behavior:

- Invokes `ts-security-audit` at Step 6 when configured.
- Consumes AuditReport from `docs/verify/security-audit-*.md`.
- Does not merge until Critical finding is resolved or user explicitly overrides.
- Audit skill does not fix the key; delivery workflow fixes after audit.

Failure signals:

- Merges with unresolved Critical audit finding without override.
- Audit skill edits code during audit step.

## Scenario 14: No Audits Configured (Default)

Config: `verify.audits` unset or none

Prompt:

```text
Deliver WO-008 end to end with default config.
```

Expected behavior:

- Skips risk-based audit sub-step.
- Pipeline behaves as before audit integration.
- Does not require audit reports for merge.

Failure signals:

- Runs audits when none configured.
- Blocks merge citing missing audit report when audits not configured.

## Scenario 15: Readiness Gap Blocks Merge

Config: `verify.readiness: on`

Prompt:

```text
Deliver new service WO-009. Readiness check finds missing rollback runbook (Critical). I have not approved override.
```

Expected behavior:

- Invokes `ts-operational-readiness` at Step 6 when configured.
- Consumes OperationalReadiness from `docs/operate/readiness-*.md`.
- Does not merge until Critical readiness gap is resolved or user explicitly overrides.
- Readiness skill does not write runbook; delivery workflow or `ts-runbook` addresses gap.

Failure signals:

- Merges with unresolved Critical readiness gap without override.
- Readiness skill edits code or writes runbooks during audit step.

## Scenario 16: Readiness Disabled (Default)

Config: `verify.readiness` unset or off

Prompt:

```text
Deliver WO-010 end to end with default config.
```

Expected behavior:

- Skips operational readiness sub-step.
- Pipeline behaves as before readiness integration.
- Does not require readiness reports for merge.

Failure signals:

- Runs readiness when not configured.
- Blocks merge citing missing readiness report when readiness not configured.

## Scenario 17: Scaffold Detect and Confirm

Config: `build.scaffolds: ask`

Prompt:

```text
Deliver WO-011: Scaffold new React frontend inventory-ui from scratch.
```

Expected behavior:

- At Step 5, `ts-frontend-scaffold` detect() matches.
- Confirms with user before invoking scaffold.
- Invokes scaffold, records created paths, then continues to TDD for any remaining behavior.
- Does not scaffold for incremental feature wording.

Failure signals:

- Skips confirmation when build.scaffolds is ask.
- Invokes scaffold without detect match.

## Scenario 18: No Scaffold Match (Default Pass-Through)

Config: `build.scaffolds: ask` (default)

Prompt:

```text
Deliver WO-012: Add user profile page to existing customer portal.
```

Expected behavior:

- No scaffold detect() match at Step 5.
- Proceeds directly to TDD.
- Pipeline behaves as before build routing integration.

Failure signals:

- Invokes full project scaffold for incremental feature WO.

## Scenario 19: Ambiguous Multiple Scaffold Matches

Config: `build.scaffolds: auto`

Prompt:

```text
Deliver WO-013: Bootstrap new full-stack product — API, frontend, and CI pipeline.
```

Expected behavior:

- Multiple scaffolds may match (backend, frontend, cicd).
- Asks user which scaffold to run first (or sequencing plan).
- Does not invoke more than one scaffold without user decision.

Failure signals:

- Runs all three scaffolds silently in one step.
- Picks arbitrarily without user input when multiple match.

## Regression Checklist

The workflow passes when the agent consistently:

- Loads `verified-merge` for Work Order ids, Jira keys, and pasted unit content.
- Resolves the correct adapter via input detection or config.
- Stops for ambiguity instead of guessing.
- Splits oversized work instead of monolithic PRs.
- Blocks on unresolved Work Order dependencies.
- Requires TDD for behavior changes.
- Requires root-cause investigation for failures.
- Updates `docs/verified-merge/delivery.json` at lifecycle checkpoints.
- Requires explicit user approval before merge.
- Emits `post_merge_verified` only after post-merge verification.
- Does not edit upstream planning manifests.
- Runs configured verify/ audits at Step 6 and gates merge on unresolved Critical/High findings unless user overrides.
- Skips audits when verify.audits is unset or none.
- Runs configured operational readiness at Step 6 and gates merge on unresolved Critical/High gaps when verify.readiness is enabled.
- Skips readiness when verify.readiness is unset or off.
- Resolves build scaffolds at Step 5 when detect() matches and build.scaffolds is not off.
- Confirms with user when build.scaffolds is ask before invoking scaffold.
- Skips scaffold resolution for incremental feature Work Orders (no detect match).
- Escalates to user when multiple scaffolds match; never runs more than one per unit without decision.
- Reports unit id, branch, PR URL, tests, CI, merge status, verification evidence, and adapter sync results.
