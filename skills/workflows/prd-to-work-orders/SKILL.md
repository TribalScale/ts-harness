---
name: prd-to-work-orders
description: Converts Product Requirements Documents (PRDs) into validated Feature Requirements Documents (FRDs), technical Blueprints, and agent-ready Work Orders through a 4-stage pipeline (Refinery → Foundry → Planner → Validator). Use when transforming raw product requirements into structured implementation plans with quality gates, state machine tracking, and drift detection.
type: workflow
disable-model-invocation: true
---

# PRD to Work Orders

`prd-to-work-orders` is the single user-facing PRD-to-Work-Orders workflow. It orchestrates four internal stages:

1. **Refinery** — raw PRD to structured requirements and FRDs.
2. **Foundry** — FRDs to technical Blueprints.
3. **Planner** — Blueprints to agent-ready Work Orders.
4. **Validator** — implemented code to drift reports and corrective Work Orders.

The stage modules live in `stages/`. This file is orchestration only: detect the current pipeline state, enforce ownership and approval gates, route to the next valid stage, and keep the manifest current.

## Start Here

At invocation, announce:

> "I'm using the prd-to-work-orders workflow to move this product from PRD to implementation-ready Work Orders."

Then run these checks in order:

1. Read `docs/prd-to-work-orders/manifest.json` if it exists.
2. Detect the current state using `state-machine.md`.
3. Check artifact ownership rules in `ownership.md`.
4. Check whether the requested action is a resume, patch, regenerate, or migrate using `rerun-policy.md`.
5. Load the mandatory modules for the selected stage.
6. Route to exactly one stage module.

Never infer a stage from the user's wording alone. The manifest and filesystem decide what is safe to do next.

## Mandatory Reads

Before any stage execution, read all common modules:

- `state-machine.md`
- `ownership.md`
- `rerun-policy.md`
- `approval-gates.md`
- `failure-recovery.md`
- The selected `stages/<stage>.md`
- The selected `quality-gates/<stage>.md`

Stage-specific required reads:

| Stage | Additional files |
|---|---|
| Refinery | `templates/refinery/frd-template.md`, `templates/refinery/feature-tree-template.md`, `templates/refinery/product-overview-template.md`, `reviewers/refinery-semantic-review.md` |
| Foundry | `templates/foundry/adr-template.md`, `templates/foundry/adversarial-review-prompt.md`, `reviewers/foundry-semantic-review.md` |
| Planner | `templates/planner/work-order-template.md`, `templates/planner/context-graph-template.md`, `templates/planner/executability-score-template.md`, `reviewers/planner-executability-review.md` |
| Validator | `templates/validator/drift-report-template.md`, `templates/validator/feedback-ingestion-template.md`, `reviewers/validator-review.md` |

If any required module cannot be read, stop and report the missing module. Do not improvise stage behavior from memory.

## Pipeline State Summary

| State | Meaning | Next valid action |
|---|---|---|
| `NO_INPUT` | No PRD path or pasted PRD is available. | Ask for the PRD. |
| `PRD_READY` | Raw PRD exists; Refinery is not complete. | Run `stages/refinery.md`. |
| `REFINERY_IN_PROGRESS` | Partial Refinery output exists. | Resume or regenerate Refinery. |
| `REFINERY_COMPLETE` | FRDs and overview docs pass gates. | Run `stages/foundry.md`. |
| `FOUNDRY_IN_PROGRESS` | Partial Blueprint output exists. | Resume or regenerate Foundry. |
| `FOUNDRY_COMPLETE` | Blueprints pass gates and challenges are resolved. | Run `stages/planner.md`. |
| `PLANNER_IN_PROGRESS` | Partial Planner output exists. | Resume or regenerate Planner. |
| `PLANNER_COMPLETE` | Work Orders and execution phases pass gates. | Hand off to `verified-merge` with `source-work-order`. |
| `IMPLEMENTATION_PRESENT` | Code exists for completed Work Orders. | Run `stages/validator.md`. |
| `VALIDATOR_COMPLETE` | Drift report is complete and items are resolved or escalated. | Continue with CWOs or finish. |

## Required Module References

- State machine: `state-machine.md`
- Ownership: `ownership.md`
- Reruns and regeneration: `rerun-policy.md`
- Approval gates: `approval-gates.md`
- Failure recovery: `failure-recovery.md`
- Evaluation metrics: `evaluation.md`
- Versioning: `versioning.md`
- Maintainer rules: `maintainer-rules.md`

## Stage Routing

### Refinery

Run `stages/refinery.md` when there is a PRD and no complete Refinery output. It owns `docs/refinery/**` and writes the `refinery` manifest section.

### Foundry

Run `stages/foundry.md` only after Refinery is complete. It owns `docs/foundry/**`, requires capability-map approval, writes Blueprints, runs adversarial architecture review, and writes the `foundry` manifest section.

### Planner

Run `stages/planner.md` only after Foundry is complete. It owns `docs/planner/**`, extracts Work Orders, assembles the context graph, proposes execution phases, and writes the `planner` manifest section.

### Validator

Run `stages/validator.md` only when Planner is complete and implemented code exists. It owns `docs/validator/**`, detects drift, generates CWOs, escalates major drift, and writes the `validator` manifest section.

## Global Rules

- Keep `SKILL.md` under 250 lines. Move stage behavior, templates, gates, and examples into linked files.
- Do not edit upstream artifacts from a downstream stage. Reopen the owning stage or create a stage issue.
- Do not delete or regenerate owned artifacts without explicit user approval.
- Do not mark a stage complete unless its quality gates pass and the manifest section is current.
- Record human approvals in `docs/prd-to-work-orders/approvals.md`.
- Record metrics in `docs/prd-to-work-orders/metrics.md`.
- Record reruns and migrations in `docs/prd-to-work-orders/run-log.md`.
- Record clarifications in `docs/prd-to-work-orders/clarifications.md`.

## Output Structure

```text
docs/prd-to-work-orders/
├── manifest.json
├── approvals.md
├── metrics.md
├── run-log.md
└── archive/
```

Stage-specific outputs remain in:

```text
docs/refinery/
docs/foundry/
docs/planner/
docs/validator/
```

## When to Use

Use this workflow when the user:

- Provides a raw PRD or product requirements and asks to turn them into Work Orders, FRDs, or Blueprints.
- Wants to continue an in-progress PRD-to-Work-Orders pipeline from the current state.
- Asks to validate implemented code against specs or detect drift.
- Mentions "software factory", "PRD to work orders", "refinery", "foundry", "planner", or "validator" in a planning context.

Do not use this for single-unit implementation or PR workflows. Use `verified-merge` with `source-work-order` or `ts-source-jira`.

## Additional Resources

- For the full pipeline walkthrough, stage examples, fixtures, and FAQ, see [reference.md](reference.md)
