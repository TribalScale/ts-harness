# Fan-Out — Parallel Multi-Agent Dispatch

## When to use

Use this skill when a **single user request** requires work from **multiple specialists** whose subtasks are **independent** (no strict ordering of outputs between them). If subtasks depend on each other’s outputs, use a **sequential chain** first, then fan-out only the independent remainder—or run **phased** fan-out (see step 3).

Specialists **never** coordinate with each other; the orchestrator dispatches, collects, and hands everything to **synthesize**.

---

## Steps

1. **Decompose** the user request into **independent subtasks**. Each subtask should have one clear owner from the routing table (see **route-task** skill).

2. **Assign specialists** per subtask using the same routing table:
   - Code/PRs/refactoring/dependencies → `dev-agent`
   - Infrastructure/deploys/monitoring/incidents → `ops-agent`
   - Testing/QA/coverage/bugs → `qa-agent`
   - Data/reports/metrics/costs → `analyst-agent`
   - Messages/updates/summaries/scheduling → `comms-agent`

3. **Verify independence**  
   Confirm subtasks do **not** require another specialist’s **unfinished** result as input.  
   - If **dependencies exist**: split into **phases**—Phase A: parallel independent work; Phase B: sequential steps using Phase A outputs; repeat as needed.  
   - Use the **dependency detection heuristic** below when unsure.

4. **Prepare delegation packages** for each specialist (same template as **route-task**): scoped memories, RULES.md applicability, expected structured output, no unrelated context.

5. **Dispatch** all **independent** subtasks **simultaneously** (parallel fan-out). Do not serialize purely independent work.

6. **Timeout**  
   Wait up to a configurable limit per subtask (**default: 5 minutes**). If the platform allows, track per-specialist deadlines; partial collection is acceptable per step 9.

7. **Validate each result** as it arrives: `status`, `confidence`, `warnings`, and whether `follow_up_needed` is set.

8. **Partial failure handling**  
   - On **failure**: **retry once** with refined instructions and any error context.  
   - If **still failing**: proceed with **partial results**, explicitly **note the gap** in synthesis, and apply escalation protocol if the gap blocks the user’s goal.

9. **Completion**  
   When all results are in **or** timeouts have elapsed, pass the **full set** of outcomes (including failures/timeouts) to the **synthesize** skill.

10. **Remember**  
    Store the **decomposition pattern** (which specialists, parallel vs phased) as episodic or procedural memory for similar future requests.

---

## Routing table (reference)

| Domain signal | Specialist |
| ------------- | ---------- |
| Code/PRs/refactoring/dependencies | `dev-agent` |
| Infrastructure/deploys/monitoring/incidents | `ops-agent` |
| Testing/QA/coverage/bugs | `qa-agent` |
| Data/reports/metrics/costs | `analyst-agent` |
| Messages/updates/summaries/scheduling | `comms-agent` |

---

## Examples

### Example A — Status update (4-way fan-out)

**User:** "Give me a status update on everything."

**Decomposition (independent):**

| Subtask | Specialist |
| ------- | ---------- |
| Stale PRs, recent merges | `dev-agent` |
| Pipeline health, environment status | `ops-agent` |
| Sprint metrics, cost trends | `analyst-agent` |
| Pending messages, upcoming meetings | `comms-agent` |

**Pattern:** Single phase, full parallel fan-out → **synthesize** into one unified status report (grouped by topic, not by agent).

---

### Example B — "Deploy and notify" (chain with fan-out)

**User:** "Deploy the latest to staging and notify the team."

**Dependencies:** Deploy must complete (or be definitively in progress with verifiable state) before notification content is accurate.

**Pattern:**

1. **Sequential:** `ops-agent` → deploy to staging; capture version/environment facts.  
2. **Then:** `comms-agent` → draft/post update using ops output.

**Note:** If "notify" only means "schedule a generic stand-up reminder" unrelated to deploy outcome, that slice could fan-out in parallel with ops—but "notify **the team**" about **the deploy** implies a **chain** after ops.

---

### Example C — "Review PR and check costs" (2-way fan-out)

**User:** "Review PR #123 and tell me if our AWS spend spiked this week."

**Decomposition:**

| Subtask | Specialist |
| ------- | ---------- |
| PR review | `dev-agent` |
| AWS spend / cost trend | `analyst-agent` |

**Pattern:** **No** dependency between code review and aggregate cost query → **2-way parallel fan-out** → **synthesize** (e.g., review summary + cost section with "Data as of" per analyst-agent rules).

---

## Dependency detection heuristic

Ask for each pair of subtasks **A** and **B**:

1. **Input question:** Does A’s **result** change what B should do, or the **validity** of B’s output? If yes → dependent (order or chain).  
2. **Resource question:** Do they mutate the **same** resource in conflicting ways (same PR, same env, same message channel) in a non-commutative order? If yes → likely **sequential**.  
3. **Truth question:** Would running B before A finish produce **wrong or misleading** user-facing claims? If yes → **chain** (or phase B after A).  
4. **Read-only + different artifacts:** Both read-only or touch disjoint resources with no shared decision → **independent** → safe for parallel fan-out.

When **mixed**: run the **independent** subset in parallel; **block** dependent tasks until prerequisites complete.

---

## Timeout and partial-failure handling

| Situation | Behavior |
| --------- | -------- |
| Subtask within default timeout | Normal validation and synthesis input. |
| Subtask exceeds timeout | Mark as `timed_out`; include in synthesis as a **gap**; retry once if the cause was transient and worth retrying. |
| Subtask fails after retry | Record failure reason; synthesize with **partial results**; escalate if the user cannot act without that slice. |
| Conflicting partial successes | Surface conflict in synthesis; apply **conflict resolution** from **synthesize** skill (present both views if needed). |

**Default timeout:** 5 minutes per subtask (configure per environment or task criticality).

---

## Related skills

- **route-task** — single-specialist routing and delegation packages  
- **synthesize** — merge parallel results into one response  
- **remember** — store decomposition patterns for reuse  
