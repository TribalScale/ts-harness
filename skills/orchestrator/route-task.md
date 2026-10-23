# Route Task — Intent Classification and Delegation

## When to use

Use this skill on **every user message that requires action** (work to be done, decisions to be informed by specialists, or changes to systems or communications).

Apply **Step 0** first (below). If Step 0 passes, respond directly without this skill.

The orchestrator **never executes tasks directly**. It classifies intent, prepares context, delegates to specialists, and synthesizes outcomes.

## Step 0 — Direct-respond guard

Run this check **before** the routing table. Respond directly (no `sessions_spawn`) when **ALL** of the following hold:

1. **No state-mutating verb is required:** no `write`, `exec`, `deploy`, `post`, `create`, `update`, `delete`, `merge`, `push`, `apply`, `rotate`, `notify`.
2. **The answer is derivable from at least one of:**
   - Memory (via the `recall` skill)
   - Prior conversation in this session
   - Workspace bootstrap docs (`RULES.md`, `MEMORY.md`, `HEARTBEAT.md`)
   - Orchestrator-native tools: `sessions_list`, `sessions_history`, `session_status`, `subagents`
3. **No specialist-unique tool surface is required:** no GitHub API, no Jira, no Slack API, no AWS, no database write, no `mcp-atlassian`, no browser.
4. **The user is not explicitly asking for execution** — phrases like "deploy X", "run Y", "post Z", "test W", "open a PR", "file a ticket" are never direct-respond.

If **any** condition fails → proceed to Step 1 (routing table).

### Step 0 worked examples

| Request | Step 0 verdict | Reason |
| --- | --- | --- |
| "what agents do we have?" | direct | workspace roster |
| "what did dev-agent do yesterday?" | direct | `sessions_history` tool |
| "did my last request finish?" | direct | `session_status` is orchestrator-native |
| "summarize my last standup" | direct | memory recall of last cron output |
| "you said X earlier — clarify?" | direct | conversational clarification |
| "list open PRs" | delegate (dev-agent) | GitHub API is specialist-unique |
| "run the tests" | delegate (qa-agent) | `exec` is specialist-only |
| "deploy to staging" | delegate (ops-agent) | state-mutating verb + production-adjacent |

---

> **Note:** `routing.intents` in `ts-harness-runtime.yaml` is authoritative for deterministic channel routing. This table provides narrative examples for the product/engineering reference vertical. Additional governed intents (for example `governed_rfp` → `rfp_request`) are configured in YAML without code changes.

## Routing table

Classify using the **primary** action and domain. When multiple signals appear, prefer the specialist whose scope matches the **main** verb (what the user is trying to accomplish first).

| Signal in the request | Route to |
| --------------------- | -------- |
| Defect, regression, or broken behavior | `propose_start_program(program_type='bug_to_jira_work_orders', entry_phase='bug_intake')` |
| Feature or pasted content to plan | `propose_start_program(program_type='content_to_jira_work_orders', entry_phase='intake')` |
| Vague code change, open a PR, or refactor | Ask once: bug or feature, then start the matching program. Do not delegate to `dev-agent`. |
| Assigned ticket (`implement PROJ-123`, my tickets) | `propose_start_program(program_type='jira_to_pr')`. `dev-agent` implements only inside that program. |
| Infrastructure, deploys, monitoring, incidents | `ops-agent` |
| Testing, QA, coverage, bug triage | `qa-agent` |
| Data queries, reports, metrics, cost analysis | `analyst-agent` |
| PRDs, epics, user stories, scoping, requirements (single-shot / informal) | `pm-agent` |
| Governed PRD from requirements (durable workflow + approval) | `propose_start_workflow(workflow_type='prd_request')` |
| Informal PRD (`quick draft`, `rough outline`, `no approval process`) | `propose_delegate_task(pm-agent)` |
| Messages, updates, summaries, scheduling | `comms-agent` |
| Content creation, campaigns, brand, analytics | `marketing-agent` |
| Lead research, outreach drafts, pipeline, deal ops | `sales-agent` |
| Governed delivery flow (requirements → PRD → Jira → PR → QA → merge → deploy) | Use `governed-delivery.md` |
| Multi-domain (e.g., "deploy and notify the team") | Decompose → use **fan-out** (parallel) or **sequential chain** per dependencies |

---

## Steps

1. **Parse the request**  
   Identify the **primary action verb** (e.g., fix, deploy, test, report, notify) and the **domain** (code, infra, quality, data, communications).

   > Note: If Step 0 already passed, you are responding directly — this section is not reached.

2. **Classify using the routing table**  
   Map the request to exactly one specialist when the work is single-domain. If multiple domains are clearly required, do not pick one winner—proceed to step 3.

   If the request describes the governed product delivery flow (for example, "turn these requirements into a PRD, get approval, create Jira tickets, build them, QA them, and deploy"), switch to `skills/orchestrator/governed-delivery.md`. That skill defines the required gates and handoffs. Do not collapse the flow into a single `pm-agent` or `dev-agent` task.

3. **Check for multi-domain scope**  
   If the task spans **multiple** domains, **decompose** into subtasks. Route independent subtasks via the **fan-out** skill; dependent subtasks as a **sequential chain** (each step’s output feeds the next). Do not collapse unrelated work into a single specialist.

4. **Check memory (recall skill)**  
   Recall how similar past requests were routed, what context helped, and any team-specific conventions. Use only **relevant** memories—scope by project, team, or ticket as appropriate.

5. **Prepare the delegation package** for the selected specialist (see template below):
   - **Task description** — clear, specific, actionable; one primary outcome.
   - **Relevant memories** — filtered to this specialist’s scope (context brokering: no full thread dumps).
   - **Applicable rules** — excerpts or pointers from [RULES.md](../../workspace/RULES.md) that constrain how this work must be done.
   - **Prior step outputs** — if this is part of a chain, include only what this specialist needs.
   - **Expected output format** — remind the specialist of structured return: `{ status, result, confidence, warnings, follow_up_needed }`.

6. **Dispatch** to the specialist. Specialists **never** talk to each other or the user—all traffic goes through the orchestrator.

7. **Receive the result.** Validate `status`, read `result`, and note `confidence` and `warnings`.

8. **Escalation check**  
   If `confidence < 0.7` or `warnings` is non-empty, apply the **escalation protocol** (retry with refined instructions, alternate approach, or human escalation as appropriate).

9. **Synthesize** the specialist output into a user-facing response using the **synthesize** skill.

10. **Remember** durable learnings (routing choices, clarifications, outcomes) using the **remember** skill for future recall.

---

## Ambiguous requests — resolution examples

| Ambiguous phrasing | Resolution |
| ------------------ | ---------- |
| "Fix production" | Could be code (dev), deploy rollback (ops), or incident comms (comms). **Recall** similar incidents; if unclear, ask: "Is this a code defect, an infra/deployment issue, or primarily notifying stakeholders?" Default **primary** action: if they mention errors/logs first → often dev then ops chain. |
| "Run the tests and ship it" | **Chain**: qa-agent (tests) → then dev-agent or ops-agent depending on whether "ship" means merge/PR vs deploy. Clarify environment if production is implied. |
| "Create a PRD from these requirements" | **pm-agent** drafts the PRD. If the user also asks for Jira tickets, require explicit PRD approval before ticket creation. |
| "Approved, create Jira tickets" | If `workflow_phase=approved` on a linked `prd_request` session, call `propose_bridge_workflow` then `bridge_workflow` after confirmation — not a one-off pm-agent delegate. |
| "Run the whole requirements-to-deploy flow" | Use **governed-delivery.md** and create a gated sequential workflow. |
| "How much did we spend and is the app up?" | **Fan-out**: analyst-agent (costs) + ops-agent (health)—independent; synthesize after both return. |
| "Update everyone on the release" | **comms-agent** as primary; if content requires facts not yet gathered, **chain**: dev-agent/ops-agent for facts → comms-agent for messaging. |
| "Review this PR" | **dev-agent** (code review). If they also want QA sign-off, decompose: dev review + qa-agent test/coverage follow-up (sequential or parallel per independence). |
| "Something is slow" | **Recall** recent changes. Primary might be ops (monitoring), analyst (metrics), or dev (profiling). Ask one short question: "Frontend, API, or infrastructure layer?" if still unclear after recall. |

When routing remains ambiguous after recall: ask a **brief** clarifying question; otherwise default to the specialist for the **primary** action.

---

## Delegation package template

Use this structure when handing work to a specialist (adjust fields for chains vs single-shot).

```markdown
## Delegation package

**Specialist:** <dev-agent | ops-agent | qa-agent | analyst-agent | pm-agent | comms-agent | marketing-agent | sales-agent>

**Task**
<Single paragraph: what "done" looks like. Constraints, environment, links/IDs.>

**Context (scoped)**
- Relevant memories: <bullets or "none">
- From prior chain step: <artifact summary or N/A>
- Rules: <pointers to RULES.md sections or quoted must-follow lines>

**Expected return**
Structured object:
`{ status, result, confidence, warnings, follow_up_needed }`

**Trust / approvals**
Note if this touches production, IAM, external comms, etc., and whether human approval is required.
```

---

## Related skills

- **fan-out** — parallel multi-specialist dispatch  
- **synthesize** — unified user-facing response  
- **recall** / **remember** — memory read/write for routing and learnings  

---

## Specialist output contract (reminder)

Each specialist returns:

```json
{
  "status": "success | partial | failed",
  "result": "<structured or narrative payload>",
  "confidence": 0.0,
  "warnings": [],
  "follow_up_needed": false
}
```

The orchestrator interprets this; the user does not receive raw specialist JSON unless appropriate for the channel.
