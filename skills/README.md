# TS Harness Skills

Workflow skills maintained in this repository:

| Skill | Path | Used by |
|-------|------|---------|
| prd-to-work-orders | `skills/workflows/prd-to-work-orders/` | pm-agent (local/Cursor filesystem runs) |
| prd-to-work-orders-governed | `skills/workflows/prd-to-work-orders-governed/` | pm-agent (EC2 governed workflow dispatch) |
| source-work-order | `skills/integrate/source-work-order/` | orchestrator (local filesystem handoff) |
| source-work-order-governed | `skills/integrate/source-work-order-governed/` | orchestrator (EC2 JSON handoff) |
| verified-merge | `skills/workflows/verified-merge/` | dev-agent (WO delivery) |

Decoupled Jira programs:

| Skill | Path | Used by |
|-------|------|---------|
| content-to-plan | `skills/pm/content-to-plan.md` | pm-agent / orchestrator (`content_to_plan`) |
| bug-intake | `skills/pm/bug-intake.md` | pm-agent / orchestrator (`bug_to_plan`, `bug_work_order`) |
| investigate-bug | `skills/review/investigate-bug/SKILL.md` | review-agent (`bug_to_plan` investigate) |
| file-work-orders-as-jira | `skills/pm/file-work-orders-as-jira.md` | pm-agent (`file_work_orders_as_jira`) |
| jira-to-pr | `skills/dev/jira-to-pr.md` | orchestrator / dev-agent (`jira_to_pr`) |

Sales RFP (palette-authored vertical):

| Skill | Path | Used by |
|-------|------|---------|
| clarify-rfp | `skills/sales/clarify-rfp/` | sales-agent |
| draft-rfp | `skills/sales/draft-rfp/` | sales-agent |
