---
name: verified-merge-governed
description: Governed-runtime work-order delivery via run_coding_backend (OpenCode) and host git/gh. Use for implement_work_orders deliver_work_order.
type: workflow
category: governed-workflows
allowed_agents: [dev-agent]
disable-model-invocation: false
always_inline: true
approval_status: approved
version: 1.0.0
---

# Verified Merge (Governed Runtime)

`verified-merge-governed` delivers one work order inside enterprise workflow
dispatch. Implementation and PR creation run through `run_coding_backend`
(host OpenCode + git/gh), not sandbox `exec`.

Use the filesystem skill (`verified-merge`) for local/Cursor runs.
Use this skill for governed EC2 delivery.

## Start Here

At invocation, announce:

> "I'm using the governed verified-merge path with run_coding_backend."

Then:

1. Read `input.work_order_id` and `input.work_order_body` (required).
2. Read optional `input.repo_url` / `input.base_branch` (runtime config fills defaults).
3. If `input.refresh` is `true`, the base branch moved: call `run_coding_backend`
   with `mode: "refresh"`, `pr_url: input.pr_url`, `work_order_id` and
   `work_order_body`. It merges the base into the existing PR branch (resolving
   conflicts if needed). Never open a new pull request. Return
   `{work_order_id, pr_url, summary, head_sha, refresh_method, security}` from the tool result.
4. If `input.review_feedback` or `input.qa_feedback` is present, this is a
   revision after code review or QA: pass whichever is present (`review_feedback`,
   `qa_feedback`) and `input.pr_url` through to `run_coding_backend` and fix the
   existing PR. Do not open a new pull request.
5. Otherwise call **`run_coding_backend`** with those fields.
6. Do **not** use sandbox `exec` for OpenCode, git push, or `gh` (`network=none`).
7. Do **not** invent work-order content if `work_order_body` is missing — fail the stage.
8. Return JSON output exactly:

```json
{
  "work_order_id": "WO-001",
  "pr_url": "https://github.com/org/repo/pull/123",
  "summary": "short status",
  "security": { "secret_scan": { "tool": "gitleaks", "findings": [] } }
}
```

`pr_url` must be the URL returned by `run_coding_backend` (or fail). Copy the
tool result's `security` block through unchanged; the runtime publishes it as
the `harness/security-scan` status on the PR.

Humans merge the PR. The orchestrator resumes `await_human_merge` via
`POST /webhooks/github` (preferred) or `resume_external_event` with `event_type=github.merge` and matching `pr_url`.

## Not In Scope

- Merging the PR
- Deploy
- Expanding beyond the current work order
