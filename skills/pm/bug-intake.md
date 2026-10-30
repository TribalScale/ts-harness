---
name: bug-intake
description: Confirm an allowlisted repo, clarify a Slack bug one question at a time, draft the bug report, and write one work order from the diagnosis.
allowed_agents: [orchestrator, pm-agent]
---

# Bug intake

Use this skill on `bug_to_plan` and `bug_work_order`. Do not draft a PRD and do not call the write-up `approved_prd` or `prd_markdown`.

## pick_repo

List configured `repo_picker.repos`. If the list is empty, ask for `org/repo` and **fail closed** unless an operator added it. If there is one repo, still confirm it. Record `repo` and `default_branch` (omit → `dev`). Do not invent a repo.

## clarify

Ask **one question at a time**. Use the diagnosis and the reporter's words in `input.report`; do not ask for anything the report already says. The reporter may answer several in one message. Do **not** draft the bug report until they say `ready` or `draft the bug`. You may say you have enough, but they decide.

## draft_bug

Write `bug_report` as Markdown in the bug template (`read_skill_reference skill_id=jira-ticket-format path=references/bug.md`): Steps to reproduce, Expected, Actual with a failure class, Evidence citing only files the diagnosis read, Blast radius, Regression test and Notes. Start with a one-line title for the Jira summary. An unknown cause is allowed; say so. Return `repo` and `default_branch` alongside it. Build it from `input.report`, the clarified answers and the diagnosis. Stay inside the clarified scope. Do not open a PR or edit code.

## draft_work_order

Write exactly one work order from the approved bug report and its diagnosis. Return:

- `work_orders_markdown` — one heading `# WO-001 …` whose body has `## Summary`, `## Requirements`, `## Files`, `## E2E Acceptance Tests`, and `## Out of Scope`. The E2E tests include the bug report's regression test and at least one negative case.
- `work_order_ids`: `["WO-001"]`
- `next_work_order_id`: `"WO-001"`

Do not run refinery, foundry, or planner. Do not file Jira from this stage.
