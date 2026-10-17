# Governed Delivery — Requirements to Deployment

> **Reference vertical:** Product/engineering delivery supports **parallel planning tracks** after PRD: Jira (`jira_from_prd`) or work orders (`work_orders_from_prd`). Both converge on track-aware implement/deploy. Use the federated delivery program coordinator: `propose_start_program` / `start_program`, `program_status`, and `propose_advance_program` / `advance_program` (or `propose_bridge_workflow` / `bridge_workflow` after PRD approval). Session keys include `program_id`, `program_phase`, `planning_track`, and `active_workflow_id`.
>
> **Programs are data-defined.** Built-in `software_delivery` is one topology; managed programs (clones, RFP, custom) may differ. Decoupled multi-engineer delivery uses `content_to_jira_work_orders` (PM files unassigned tickets) and `jira_to_pr` (each engineer implements one assigned ticket). When `program_type` is present on the intent, pending confirmation, or tool args, **always pass that `program_type` explicitly** — do not assume `software_delivery`. Omitted tool args still default to `software_delivery` for backward compatibility.

## When to Use

Use this skill when a user asks TS Harness to run a product delivery flow that spans multiple governed stages:

- Turn requirements into a PRD
- Iterate on the PRD with human comments
- Create Jira tickets from the approved PRD
- Implement Jira tickets into PRs
- Run QA checks
- Wait for human merge
- Deploy merged code

Do **not** use this skill for a single-domain task such as "review PR #42" or "deploy staging". Route those directly to the relevant specialist. For **implement TFS-123** / "my tickets", start `jira_to_pr` instead of `software_delivery`.

For **PRD-only** work with human approval gates, use `propose_start_workflow(workflow_type='prd_request')` — not a one-off pm-agent delegate. For **informal PRD** requests (`quick draft`, `no approval process`), delegate to pm-agent instead.

For **content → work orders → Jira** (multi-engineer, no implement on the planning thread), call `propose_start_program(program_type='content_to_jira_work_orders', entry_phase='intake')`. For **a Slack bug → one work order → unassigned Jira Bug**, call `propose_start_program(program_type='bug_to_jira_work_orders', entry_phase='bug_intake')`. That program is not `software_delivery` and not `content_to_jira_work_orders`. For **assigned Jira → PR**, call `propose_start_program(program_type='jira_to_pr', entry_phase='pick')`, or `entry_phase='implement'` with `artifacts.jira_issue_key` when they pasted a key.

---

## Stage 0 — PRD workflow (durable)

When the user wants a governed PRD (not informal), call `propose_start_workflow` with `workflow_type='prd_request'`. After user confirmation, call `start_workflow`. The workflow runs `intake` → `draft_prd` → `approve_prd` with a revise loop on reject.

After PRD approval (`workflow_phase=approved`), ask how the user wants to continue — Jira filing or work-order planning. When the user chooses, call `propose_advance_program` then `advance_program` after confirmation (or legacy `propose_bridge_workflow` / `bridge_workflow`) with an explicit target (`jira_from_prd` or `work_orders_from_prd`).

**Work-orders track skills:** Planning stages use `prd-to-work-orders-governed` (JSON artifacts, no manifest). Prepare handoff uses `source-work-order-governed`. Local filesystem skills (`prd-to-work-orders`, `source-work-order`) are for Cursor/local runs only.

**Enter at any phase:** Users may skip ahead with `propose_start_program(entry_phase=...)` — `jira` / `work_orders` require `artifacts.approved_prd`, `implement` requires `artifacts.jira_epic_key` **or** work-order handoff (`planner_complete` + `next_work_order_id`), `deploy` requires `artifacts.pr_url`. Use `program_status` for holistic progress across phases.

---

## Governance Rule

The orchestrator must treat each phase boundary as a gate. A specialist can prepare the next artifact, but the orchestrator cannot advance across a human gate until the user explicitly approves that gate.

Required explicit human phrases:

- PRD approval: "approved", "approve this PRD", or "PRD approved"
- Jira creation: "create Jira tickets", "file the epic and stories", or equivalent after PRD approval
- PR merge: performed by a human or external GitHub branch protection, never by an agent
- Production deploy: human-initiated and human-approved per `RULES.md`

If the user gives comments instead of approval, route the comments back to the current specialist for revision. Do not proceed to the next stage.

---

## Workflow State

Use `skills/orchestrator/workflow-state.md` for any governed delivery run that spans more than one specialist dispatch or may continue across sessions. Workflow state is recorded through the memory-api workflow endpoints, not raw SQL.

Create one workflow with these canonical task names:

| Step | Agent | Gate |
| --- | --- | --- |
| `intake` | `pm-agent` | none |
| `draft_prd` | `pm-agent` | none |
| `revise_prd` | `pm-agent` | repeats until human approval |
| `approve_prd` | `orchestrator` | human approval (prd_request workflow) |
| `create_jira` | `pm-agent` | requires approved PRD + human approval |
| `implement_ticket` | `dev-agent` | one task per Jira ticket |
| `qa_gate` | `qa-agent` | program stage; canonical QA envelope over delivered PR set (blocks on `failed`) |
| `await_human_merge` | `orchestrator` | depends on `qa_gate`; `match_all` wait for human/GitHub merge of all QA PRs |
| `deploy_staging` | `ops-agent` | requires merged code and explicit deploy request |
| `deploy_production` | `ops-agent` | requires production approval policy |

Before dispatching each non-human step, call `POST /workflow/claim` with the task id and configured `agent_id`. Dispatch only when the response returns `claimed: true`.

Represent human gates as workflow tasks with `approval_required: true`. Use `POST /workflow/approve` or `POST /workflow/reject` when the human decision arrives. The following stages should be approval-gated:

- `create_jira`
- `await_human_merge`
- `deploy_staging`
- `deploy_production`

---

## Stage 1 — Draft PRD

Delegate to `pm-agent` with:

```markdown
Task: Draft a PRD from the user's requirements. Return a Markdown PRD only.
Constraints:
- Status must be `Draft`.
- Do not create or update Jira.
- Include open questions instead of guessing.
- Return `follow_up_needed: ["Human review of PRD"]`.
```

After `pm-agent` returns, present the PRD to the user and ask for approval or comments. Do not create Jira tickets in the same turn unless the user has already explicitly approved a specific PRD artifact.

---

## Stage 2 — PRD Review Loop

If the user provides comments, delegate to `pm-agent`:

```markdown
Task: Revise the existing Draft PRD using the human review comments.
Inputs:
- Current PRD artifact
- Human comments
Constraints:
- Keep status `Draft`.
- Do not create or update Jira.
- Return the revised PRD and a short change summary.
```

Repeat until the user explicitly approves the PRD.

---

## Stage 3 — Jira Creation

Only after explicit PRD approval, delegate to `pm-agent`:

```markdown
Task: Create Jira work from the approved PRD.
Inputs:
- Approved PRD artifact
- Human approval text
Constraints:
- Create work only in the tenant-bound Jira projects (`program_defaults.jira_filing.allowed_projects`); policy denies writes elsewhere.
- Create one Epic, N Stories, and 0..N Tasks as needed.
- Preserve PRD traceability in the epic description or linked document.
- Add one traceability comment listing every child ticket.
- If more than 10 tickets, P0/P1 priority, or cross-project links are needed, stop and request approval before writing those changes.
```

Return the epic key, child ticket keys, Jira URLs, warnings, and unresolved open questions.

---

## Stage 4 — Development

For each Jira ticket selected by the user or orchestrator, delegate to `dev-agent` using `skills/dev/jira-to-pr.md`.

Constraints:

- `dev-agent` may create branches, commits, pushes, and PRs.
- `dev-agent` must not merge PRs.
- If a ticket lacks acceptance criteria, send it back to `pm-agent` or the user before implementation.
- Each `dev-agent` result must include the PR URL, test status, risks, and Jira ticket key.

---

## Stage 5 — QA Gate (program stage)

`qa_gate` is a **program stage** on the implement skeletons, not an ad-hoc
dispatch. It runs automatically after delivery and **before** `await_human_merge`
(work orders: `deliver_work_order → qa_gate → await_human_merge`; Jira: implement
tasks → `collect_delivery_results` barrier → `qa_gate → await_human_merge`).

`qa_gate` is **not** host verify. Host verify is the build/test run done by
`run_coding_backend` during delivery; `qa_gate` is an independent acceptance
review of the resulting PR(s) using the `governed-qa-gate` skill. By default it
reuses the host verify summary as evidence rather than re-running the full suite.

`qa-agent` returns the canonical QA envelope:

```markdown
Task: Run the governed QA gate over the delivered PR set (governed-qa-gate).
Inputs:
- PR URL set (input.pr_urls or input.pr_url)
- Work-item references and acceptance criteria
- Host verify summary
Constraints:
- Output PR URL set must equal the input PR URL set exactly.
- Top-level status:success only when every per-PR status is success.
- Do not approve or merge the PR.
- A schema-valid status:failed blocks the gate (merge wait never arms,
  qa_gate_failed increments); a human decides whether to re-run delivery.
```

A `success` envelope arms `await_human_merge` with a `match_all` wait over the QA
PR set. This is not a PR approval.

---

## Stage 6 — Human Merge Gate

When development and QA are complete, tell the user:

```markdown
PR is ready for human merge.

Required before continuing:
- Human review complete
- Branch protection checks passing
- PR merged by a human or GitHub rules

Reply after the PR is merged and include the merge commit or confirm main is updated.
```

Do not ask any specialist to merge the PR.

---

## Stage 7 — Deployment

After the user confirms merge, route deployment requests to `ops-agent`.

For staging:

- Use `skills/ops/deploy-staging.md`.
- Run preflight checks.
- Proceed only after the user explicitly asks for staging deployment.
- Report health checks and smoke test results.

For production:

- Enforce `RULES.md` production policy.
- Require human initiation and approval.
- Agents may prepare plans and evidence, but must not bypass the production approval process.

---

## Recovery and Escalation

- If a specialist fails once, retry with refined context.
- If a specialist fails twice, try an alternate approach or escalate to the user.
- If a workflow task is `running` for more than 30 minutes, follow `workflow-state.md` stale task recovery.
- If any gate is ambiguous, stop and ask the user for a yes/no approval or clarification.
- Log significant routing, approval, Jira, PR, QA, and deployment events to the audit system per `skills/orchestrator/audit-logging.md`.

---

## User-Facing Status Format

When reporting progress, use this concise status block:

```markdown
Governed delivery status:
- PRD: Draft | Approved | Needs revision
- Jira: Not filed | Filed <epic-key>
- Development: Not started | In progress | PR open <url>
- QA: Not run | Failed | Passed
- Merge: Waiting for human | Merged
- Deploy: Not requested | Staging complete | Production pending approval

Next required action: <human action or specialist dispatch>
```
