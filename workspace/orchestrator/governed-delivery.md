# Governed Delivery — Requirements to Deployment

## When to Use

Use this skill when a user asks TS Harness to run a product delivery flow that spans multiple governed stages:

- Turn requirements into a PRD
- Iterate on the PRD with human comments
- Create Jira tickets from the approved PRD
- Implement Jira tickets into PRs
- Run QA checks
- Wait for human merge
- Deploy merged code

Do **not** use this skill for a single-domain task such as "write a PRD only", "review PR #42", or "deploy staging". Route those directly to the relevant specialist.

For **content → work orders → unassigned Jira**, start `content_to_jira_work_orders`. For **a Slack bug → one work order → unassigned Jira Bug**, start `bug_to_jira_work_orders` (not `software_delivery` and not `content_to_jira_work_orders`). For **my assigned Jira → PR**, start `jira_to_pr` (or pass `jira_issue_key` and enter at `implement`).

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
| `draft_prd` | `pm-agent` | none |
| `revise_prd` | `pm-agent` | repeats until human approval |
| `create_jira` | `pm-agent` | requires approved PRD |
| `implement_ticket` | `dev-agent` | one task per Jira ticket |
| `qa_gate` | `qa-agent` | requires PR URL |
| `await_human_merge` | `orchestrator` | requires human or GitHub merge |
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
- Create work only in projects `{{JIRA_PROJECTS}}`.
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

## Stage 5 — QA Gate

After a PR is opened, delegate to `qa-agent`:

```markdown
Task: Run the QA gate for this PR.
Inputs:
- PR URL
- Jira ticket key and acceptance criteria
- Test instructions from the PR
Constraints:
- Run tests and coverage checks appropriate for the changed package.
- Modify only test files if new tests are required.
- Do not approve or merge the PR.
- If the gate fails, return exact failures and route back to `dev-agent`.
```

QA returns `status: success` only when the test and coverage gate is satisfied. This is not a PR approval.

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
