# Memory System — Usage Instructions

This file tells {{AGENT_NAME}} how to use the Postgres-backed memory system. Memory is what makes you useful over time — you learn, adapt, and retain context so users don't have to repeat themselves.

---

## Core Principle

**Every interaction is an opportunity to learn. Every task is informed by what you've learned before.**

Before you respond: **recall**. After you learn: **remember**. When things change: **update**. When asked: **forget**.

---

## When to Remember

Store a new memory when any of the following occur:

| Trigger                                      | What to Store                                           | Memory Type   | Example                                              |
| -------------------------------------------- | ------------------------------------------------------- | ------------- | ---------------------------------------------------- |
| You learn a new fact about the org/team       | The fact, source, and context                           | `semantic`    | "The auth service uses JWT with RS256 signing"       |
| A decision is made                            | The decision AND the reasoning behind it                | `episodic`    | "Team chose Postgres over DynamoDB because of JOIN needs — decided in sprint planning 2024-03-15" |
| A task is completed                           | Outcome, approach taken, any lessons learned            | `episodic`    | "Deployed v2.3.1 to staging — had to roll back once due to missing env var" |
| A user corrects you                           | The correction as a high-confidence directive           | `directive`   | "User: 'We never deploy on Fridays' -> store as deployment rule" |
| You discover a process or convention          | The process steps and when to apply them                | `procedural`  | "To deploy to staging: confirm merge, request staging deploy, run preflight, verify in #ops" |
| You learn about a relationship                | The relationship between entities                       | `relational`  | "Sarah is the tech lead for the payments team"       |
| A user states a preference                    | The preference and who stated it                        | `directive`   | "Mike prefers concise Slack updates, no email"       |

### What NOT to Store

- Verbatim conversation transcripts
- Credentials, tokens, API keys, or passwords
- PII without explicit consent and an expiry date
- Trivial or ephemeral information ("user said hello")
- Duplicate information already in memory (update instead)

---

## When to Recall

Check memory before acting in these situations:

| Situation                                         | What to Search For                                     |
| ------------------------------------------------- | ------------------------------------------------------ |
| **Before answering any question**                 | Relevant semantic and directive memories about the topic |
| **Before starting a task**                        | Past experiences with similar tasks (episodic), relevant procedures (procedural), applicable rules (directive) |
| **When a user references the past**               | Episodic memories matching the timeframe or topic      |
| **When working in a specific scope**              | All memories scoped to that team, project, or user     |
| **When interacting with a specific person**       | User preferences (directive), past interactions (episodic), role/relationships (relational) |
| **When encountering an error or unexpected state** | Past incidents or failures with similar symptoms        |
| **Before making a recommendation**                | Past decisions on similar topics, stated preferences    |

### Recall Strategy

1. **Start broad, then narrow.** Search by topic first, then filter by scope and recency.
2. **Weight recent memories higher** but don't ignore older foundational knowledge.
3. **Cross-reference memory types.** A semantic fact + an episodic experience + a directive rule together give you the full picture.
4. **If memory is empty for a topic**, acknowledge it: "I don't have any prior context on this. Let me work from first principles."

---

## When to Forget

Remove or expire memories in these situations:

| Trigger                                          | Action                                                  |
| ------------------------------------------------ | ------------------------------------------------------- |
| User says information is no longer correct        | Expire the old memory (set `expires_at` to now). Create a new memory with the corrected information. |
| Memory has passed its `expires_at` date          | Mark as expired during the monthly maintenance heartbeat. |
| User explicitly requests deletion                 | Delete the memory. Log the deletion in `memory_audit`.  |
| User says "forget that" or "that's wrong"         | Expire the relevant memory. If "that's wrong," also store the correction. |
| During monthly maintenance                        | Run the full maintenance cycle (see Heartbeat).         |
| Duplicate detected                                | Merge into the higher-confidence or more recent version. Expire the duplicate. |

### Forget Protocol

1. **Never hard-delete without logging.** Always record what was removed and why in `memory_audit`.
2. **Prefer expiry over deletion.** Expired memories can be reviewed; deleted ones cannot.
3. **When correcting**, always create the new memory before expiring the old one — never leave a gap.

---

## Memory Types Guide

### Episodic Memory

**What happened — events, experiences, task outcomes.**

Use for: completed tasks, incidents, decisions made, meetings, deployments, failures.

```
Type: episodic
Content: "Deployed auth-service v2.4.0 to staging. Encountered a database migration timeout.
          Fixed by increasing statement_timeout to 60s. Deployed successfully on second attempt."
Scope: project:auth-service
Confidence: 1.0
```

Episodic memories are your experience. They help you avoid repeating mistakes and build on past successes.

### Semantic Memory

**What you know — facts, definitions, technical knowledge.**

Use for: architecture details, technology choices, system configurations, team structures, business rules.

```
Type: semantic
Content: "The payments service uses Stripe API v2023-10-16. Webhook endpoint is /api/webhooks/stripe.
          Events processed: payment_intent.succeeded, payment_intent.failed, charge.refunded."
Scope: project:payments-service
Confidence: 0.9
```

Semantic memories are your knowledge base. They prevent you from asking the same questions twice.

### Procedural Memory

**How to do things — processes, workflows, step-by-step procedures.**

Use for: deployment procedures, incident response steps, onboarding checklists, release processes.

```
Type: procedural
Content: "To release to production:
          1. Ensure all tests pass on staging
          2. Create a release PR from staging to main
          3. Get two approvals (one must be tech lead)
          4. Merge the PR
          5. Request the approved production deployment
          6. Verify in production within 15 minutes
          7. Post release notes in #releases"
Scope: tenant:default:agent:orchestrator
Confidence: 1.0
```

Procedural memories are your playbooks. They ensure consistency and reduce the need for human hand-holding. A company-wide playbook becomes `global` or `department:{name}` only when a person promotes an approved brief.

### Relational Memory

**How things and people connect — relationships, ownership, dependencies.**

Use for: team membership, system dependencies, who owns what, who to contact for what.

```
Type: relational
Content: "Sarah Chen (sarah.chen) is the tech lead for the Platform team.
          She owns auth-service and user-service. Reports to James (VP Eng).
          Prefers Slack for quick questions, email for formal requests."
Scope: tenant:default:agent:orchestrator
Confidence: 1.0
```

Relational memories are your org chart and dependency map. They help you route questions and tasks correctly.

### Directive Memory

**What you must do or must not do — rules, preferences, corrections.**

Use for: user preferences, team conventions, explicit rules, corrections from users.

```
Type: directive
Content: "NEVER deploy on Fridays. This is a hard rule from the engineering lead.
          If a Friday deploy is requested, push back and suggest Monday."
Scope: tenant:default:agent:orchestrator
Confidence: 1.0
```

Directive memories are your guardrails beyond what's in RULES.md. They capture department and user rules that evolve over time. Do not store them as `global` or `team:`.

---

## Scope Rules

Memories are scoped to control visibility and relevance. Scoping prevents irrelevant context from polluting agent responses.

### Scope Hierarchy

```
global              -> company facts already stored; agents do not write this scope
  department:{name} -> promoted approved briefs for one department
  project:{id}      -> shared across programs in the same tenant + project
    tenant:{t}:agent:{a} -> agent-private notes (what you write)
```

Collaboration is **tenant + project**, and separately **department**. Two programs in Project A and Project B stay isolated unless someone creates a one-way explicit link. A program with no `project_id` does not auto-share. A program with no department does not read or publish department memory. Do not dump another program's session; use the curated `related_program_context` / project brief (approved facts + live status only). Department memory is an approved brief a person in that department promotes. Do not write `global`, `department:`, or `team:` yourself.

### How to Determine Scope

| Memory is about...                | Scope to use              | Example                                |
| --------------------------------- | ------------------------- | -------------------------------------- |
| What you learned on this run      | your agent scope          | `tenant:{tenant}:agent:{agent}`        |
| A specific project or repo        | `project:{project_name}`  | "auth-service uses RS256 for JWTs"     |
| A specific person's preference    | `user:{username}`         | "Sarah prefers bullet-point updates"   |
| One department's standing practice | `department:{name}`      | Promoted from an approved brief only   |

### Scope Selection Rules

1. **Write your own agent scope.** `remember` rejects `global` and `department:`.
2. **Department memory is a promotion.** A person in that department copies an approved program brief. Agents do not write it, and there is no team scope.
3. **When unsure, stay on the agent scope.**

---

## Confidence Scoring

Every memory gets a confidence score from 0.0 to 1.0.

| Score Range | Meaning                        | When to Use                                                    |
| ----------- | ------------------------------ | -------------------------------------------------------------- |
| **1.0**     | Certain — explicit statement   | User directly told you this fact. Verified from authoritative source. |
| **0.9**     | Very high — strong evidence    | Multiple consistent data points. Well-documented.              |
| **0.8**     | High — inferred with evidence  | Inferred from reliable context (code, docs, consistent behavior). |
| **0.7**     | Moderate — reasonable inference| Inferred from limited context. Likely correct but not verified. |
| **0.6**     | Low-moderate — uncertain       | Observed once. Could be coincidence or outdated.               |
| **0.5**     | Uncertain — needs verification | Educated guess. Flag for verification at next opportunity.     |
| **< 0.5**   | Unreliable                    | Do not store. If already stored, flag for review or expiry.    |

### Confidence Adjustment Rules

- **User confirms a memory** -> raise confidence to 1.0
- **User contradicts a memory** -> expire old memory, create new one at 1.0
- **Memory is used successfully** (led to correct action) -> raise confidence by 0.1 (max 1.0)
- **Memory leads to incorrect action** -> lower confidence by 0.2, flag for review
- **Memory ages without access or verification** -> lower confidence by 0.05 per month after 6 months

---

## Memory Hygiene

### Monthly Maintenance Checklist

Run during the monthly heartbeat:

1. **Expire stale memories** — any memory past its `expires_at` date
2. **Review low-confidence memories** (< 0.5) — expire or flag for user verification
3. **Deduplicate** — find and merge duplicate memories, keeping the higher-confidence version
4. **Archive inactive memories** — memories not accessed in > 90 days move to archive
5. **Audit directive memories** — confirm that rules and preferences are still current
6. **Generate memory health report**:
   - Total active memories (by type, by scope)
   - Memories created this month
   - Memories expired this month
   - Average confidence score
   - Most-accessed memories

### Storage Guidelines

- Keep memory content **concise but complete**. Include enough context to be useful without being verbose.
- Always include: the fact/event, the source (who said it / where you learned it), the date.
- For decisions: include the alternatives considered and why this option was chosen.
- For procedures: include when to use them and any prerequisites.
- Set `expires_at` for time-sensitive information (e.g., "current sprint ends 2024-04-12" should expire on that date).
