# Bug to Jira work order

Slack bug report → confirm repo → read the cited files → diagnose → one approved work order → one unassigned Jira Bug. Engineers implement via [jira-to-pr.md](jira-to-pr.md). This program does not start `jira_to_pr`.

## Start

Slack signals: `file this bug`, `investigate this error`, `bug report`, `production incident`, `fix bug`, `regression`, `broken`, `error`. `what error` and `error handling` stay off this program. Vague code (`open pr`, `refactor`, `change the`) asks once, then starts this program or `content_to_jira_work_orders`. `implement TFS-` and `my tickets` stay on `jira_to_pr`.

```text
propose_start_program(program_type=bug_to_jira_work_orders, entry_phase=bug_intake)
```

Mid-pipeline file: `entry_phase=file_jira` with `work_order_ids` + `work_orders_markdown`.

Live routing sees these signals only after the host runtime YAML (the mounted file, not the template) has the same intent.

## What it does

1. `bug_intake` (`bug_to_plan`) confirms an allowlisted repo, gathers the default-branch tree and up to eight cited files, and has review-agent diagnose from those files only. Unknown cause is a valid diagnosis. The reporter says ready before the bug report is drafted, then approves it.
2. `work_order` (`bug_work_order`) drafts one `# WO-001` section (Summary, Requirements, Files, E2E Acceptance Tests, Out of Scope) and waits for approval. No refinery, foundry, or planner.
3. `file_jira` reuses `file_work_orders_as_jira`. Issue type is Bug unless `issue_type_by_project` says otherwise. The ticket stays unassigned. The description is that work-order section plus the pointer block.

## Operator config

`config.repo_picker.repos: []` inherits the tenant allowlist, same as the feature program. `config.jira_filing` is `mode: custom` with `default_issue_type: Bug`, so it does not inherit Story.

## After filing

A human assigns the Bug. A later `jira_to_pr` run reads the work-order section from the ticket body. This program has no implement node.
