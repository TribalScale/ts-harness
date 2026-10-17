# PRD to Work Orders Pipeline — A Practical Guide

Turn any Product Requirements Document into agent-ready Work Orders that coding agents can implement autonomously. This guide walks you through the full pipeline using a real example.

## The Pipeline

```
PRD → [Refinery] → [Foundry] → [Planner] → Coding Agents → [Validator] → feedback loop
       what/why      how        who/when      build           verify         ↺
```

One skill, `prd-to-work-orders`, runs the pipeline through four internal stages. Each stage transforms the output of the previous into a more precise, more actionable artifact:

1. **Refinery** — raw PRD → structured requirements (FRDs, feature tree)
2. **Foundry** — requirements → technical Blueprints (containers, components, features)
3. **Planner** — Blueprints → agent-ready Work Orders with context graph
4. **Validator** — code → drift reports and corrective Work Orders

**The problem this solves:** Without this pipeline, agents get vague specs, miss context, hallucinate scope, and produce code that drifts from intent. Each module eliminates a specific failure mode — ambiguous requirements, missing architecture, fragmented tasks, and undetected drift.

**Who this guide is for:** Product managers defining what to build, technical leads designing how to build it, and developers using AI agents to implement it. No prior experience with the pipeline is required.

**Running example:** We'll follow **TaskFlow** — a task management application — through the entire pipeline.

---

## Prerequisites & Setup

### Recommended Entry Point

Use `prd-to-work-orders` as the single user-facing skill for production runs. It detects the current pipeline state, routes to the correct internal stage, enforces approval gates, updates the manifest, and prevents unsafe stage skips.

The four original stages still exist as internal modules:

- **Refinery** — PRD → FRDs
- **Foundry** — FRDs → Blueprints
- **Planner** — Blueprints → Work Orders
- **Validator** — code → drift reports and corrective Work Orders

Reference examples:

- `examples/team-tasks/` — tiny golden path fixture
- `examples/magic-mailbox-vertical-slice/` — product-realistic vertical slice

### Skill Location

The canonical skill lives at `~/.agents/skills/prd-to-work-orders/`:

```
~/.agents/skills/
└── prd-to-work-orders/
    ├── SKILL.md
    ├── stages/
    ├── templates/
    ├── quality-gates/
    └── schemas/
```

### How to Invoke

Tell your AI agent to use the skill by name. Example prompts:

- "Use prd-to-work-orders to turn this PRD into implementation-ready Work Orders"
- "Use prd-to-work-orders to continue from the current pipeline state"
- "Use prd-to-work-orders to validate this implementation against the specs"

### Input/Output Summary


| Step | Internal stage | Input | Output |
| ---- | -------------- | ----- | ------ |
| 1 | Refinery | Raw PRD | `docs/refinery/` — FRDs, feature tree, product overviews |
| 2 | Foundry | `docs/refinery/` | `docs/foundry/` — Container, Component, Feature Blueprints |
| 3 | Planner | `docs/foundry/` + `docs/refinery/` | `docs/planner/` — Work Orders, context graph, execution phases |
| 4 | Validator | All `docs/` + implemented code | `docs/validator/` — drift reports, corrective Work Orders |


The umbrella skill checks for required inputs, records state in `docs/prd-to-work-orders/manifest.json`, and stops if upstream artifacts are missing.

### Production Hardening

The canonical skill includes a semantic validation layer:

- Stage reviewer prompts challenge product thinking, architecture quality, Work Order executability, and Validator evidence.
- Work Orders include an Agent Executability Score so unclear work can be split or clarified before coding.
- Clarifications are tracked in `docs/prd-to-work-orders/clarifications.md` and feed delivery metrics.
- `scripts/validate_prd_to_work_orders.py` checks structure, typed Markdown extraction, examples, review artifacts, and executability scores.

---

## Step 1: Refinery — PRD to Structured Requirements

> **Invoke:** "Use prd-to-work-orders to run the Refinery stage for this PRD"

The Refinery transforms a raw PRD into structured, hierarchical requirements. It defines *what* and *why* — never *how*.

### Sample PRD Input

Paste your PRD into the conversation. It can be messy — the Refinery is designed to handle gaps, ambiguity, and mixed business/technical content. Here's our TaskFlow PRD:

```markdown
# TaskFlow — Project Requirements

We need a task management app. Users should be able to create tasks, assign
them to team members, and track progress. We want real-time updates.

The app should have authentication with roles — admins can manage teams,
regular users can only see their own tasks. Use JWT for auth tokens.

We need notifications when tasks are assigned or completed. Email and
in-app notifications.

Tech stack: React frontend, Node.js/Express backend, PostgreSQL.
Should be deployed on AWS with Docker containers.
```

### What Happens

**Phase 1 — PRD Analysis & Gap Detection**

The agent reads the PRD and produces a gap report. It separates business requirements from technical decisions (JWT, React, AWS) and flags ambiguity:

```markdown
## Gap Report

### Gaps Found
- "Roles" mentioned but no role definitions provided. What roles exist beyond admin/regular?
- "Real-time updates" — which actions trigger real-time updates? All changes or only assignments?
- No mention of task states or workflow (e.g., To Do → In Progress → Done)
- No success metrics or business problem statement

### Technical Content Extracted (for Foundry handoff)
- JWT authentication (intentional tech decision)
- React, Node.js/Express, PostgreSQL (stack choice)
- AWS + Docker (deployment choice)
```

The agent presents this report and waits for you to resolve the gaps before proceeding. **This is the highest-leverage human intervention point** — resolving ambiguity here prevents cascading issues downstream.

**Phase 2 — Product Overview Authoring**

After gaps are resolved, the agent drafts six overview documents. Each is presented individually for your approval. Here's one example:

```markdown
# Personas

## Project Manager (Primary)
- **Goals:** Assign tasks to team members, monitor team progress, manage deadlines
- **Frustrations:** Losing track of task status across tools, no visibility into workload
- **Success looks like:** Can see all team tasks in one view, gets notified when tasks complete

## Team Member
- **Goals:** See assigned tasks, update progress, collaborate with teammates
- **Frustrations:** Not knowing when new tasks are assigned, unclear priorities
- **Success looks like:** Clear task list with priorities, instant notification of new assignments
```

**Phase 3 — Feature Decomposition**

The agent proposes a feature tree with hierarchy, complexity estimates, and dependencies:

```markdown
## Feature Hierarchy

- **User Authentication** [M]
  - Login / Logout [S]
  - Registration [S]
  - Role-Based Access Control [M]
- **Task Management** [L]
  - Task CRUD [M]
  - Task Assignment [S]
  - Task Status Workflow [M]
- **Team Collaboration** [M]
  - Team Management [M]
  - Member Directory [S]
- **Notifications** [M]
  - In-App Notifications [S]
  - Email Notifications [M]

## Dependencies
| Feature | Depends On |
|---------|-----------|
| Task Assignment | User Authentication, Team Collaboration |
| Notifications | Task Management (triggers) |
| Role-Based Access Control | User Authentication (base) |
```

You approve or revise this tree before the agent writes any FRDs.

**Phase 4 — FRD Authoring**

The agent writes one Feature Requirement Document per feature, in dependency order. Each FRD follows a strict format with traceable IDs. Here's an excerpt from FRD-001:

```markdown
# FRD-001: User Authentication

**Parent Feature:** User Authentication
**Personas:** Project Manager, Team Member
**Business Problem Link:** Teams need secure, role-appropriate access

## User Story
As a team member, I want to log in securely so that I can access my tasks
and my team's workspace without unauthorized access.

## Requirements

### REQ-AUTH-001: Login
When a registered user submits valid credentials, the system shall
authenticate them and redirect to their dashboard.

**Acceptance Criteria:**
- AC-AUTH-001.1: When a user submits a valid email and password, the system
  shall authenticate and redirect to the dashboard within 2 seconds.
- AC-AUTH-001.2: When a user submits an invalid password, the system shall
  display "Invalid email or password" without revealing which field is wrong.
- AC-AUTH-001.3: When a user submits an unregistered email, the system shall
  display the same "Invalid email or password" message.

### REQ-AUTH-002: Session Management
When an authenticated user's session expires, the system shall redirect
to the login page with a "Session expired" message.

**Acceptance Criteria:**
- AC-AUTH-002.1: When a session token expires, the system shall redirect
  to login and display "Your session has expired. Please log in again."
```

**Phase 5 — Cross-FRD Consistency**

The agent verifies all FRDs use consistent terminology, have no conflicting requirements, and every feature traces upward to a persona and business problem.

### Human Touchpoints


| Phase                 | What You Decide                                          |
| --------------------- | -------------------------------------------------------- |
| 1 — Gap Detection     | Resolve ambiguities and fill gaps                        |
| 2 — Product Overviews | Approve each document (personas, business problem, etc.) |
| 3 — Feature Tree      | Approve or revise the hierarchy and dependencies         |
| 4 — FRDs              | Review each FRD as the agent writes them                 |


### Common Mistakes to Avoid

- Writing acceptance criteria with vague language ("handle errors appropriately") — rewrite with specific behavior
- Letting technical decisions ("use JWT") leak into FRDs — those belong in the Technical Requirements overview
- Accepting compound ACs with "and" joining distinct behaviors — split them

---

## Step 2: Foundry — Requirements to Blueprints

> **Invoke:** "Use prd-to-work-orders to run the Foundry stage from the Refinery output"

The Foundry reads the Refinery output and produces technical Blueprints — the *how* for the Refinery's *what*. Blueprints define runtime components, data models, API contracts, and architectural decisions.

### What Happens

**Phase 1 — Container Identification**

The agent reads the Technical Requirements and identifies deployable units:

```markdown
## Containers

1. **client-app** — React SPA served via CDN
2. **api-server** — Node.js/Express REST API
3. **database** — PostgreSQL with application schema
4. **notification-worker** — Background worker for email delivery
```

**Phase 2 — Capability Mining & Proposal**

The agent reads all FRDs and identifies shared capabilities that multiple features need. This is the key architectural decision — the agent proposes, you approve:

```markdown
## Proposed Component Blueprints

| Component | Used By | Summary |
|-----------|---------|---------|
| Identity Management | FRD-001, FRD-003 | User model, authentication, sessions, RBAC |
| Task Engine | FRD-002, FRD-004 | Task model, CRUD operations, status workflow |
| Team Registry | FRD-003, FRD-002 | Team model, membership, member directory |
| Notification Delivery | FRD-004, FRD-002 | Event triggers, in-app + email routing |

Approve this capability map? These become the reusable building blocks.
```

**Phase 3 — Blueprint Authoring**

After approval, the agent writes all Blueprints autonomously. Component Blueprints first (they're feature-agnostic), then Feature Blueprints (they compose Components).

Here's an excerpt from the **Identity Management Component Blueprint**:

```markdown
# Component Blueprint: Identity Management

**Capability:** User identity, authentication, authorization, session management
**Containers:** api-server, database

## Data Models

### User
| Field | Type | Constraints |
|-------|------|------------|
| id | UUID | Primary key, auto-generated |
| email | string | Unique, indexed, max 255 chars |
| password_hash | string | bcrypt, min cost 12 |
| role | enum | "admin" or "member", default "member" |
| team_id | UUID | Foreign key → Team.id, nullable |
| created_at | timestamp | Auto-set on creation |
| updated_at | timestamp | Auto-set on mutation |

### Session
| Field | Type | Constraints |
|-------|------|------------|
| token | JWT | Signed with RS256, 24h expiry |
| user_id | UUID | Foreign key → User.id |
| issued_at | timestamp | Auto-set |
| expires_at | timestamp | issued_at + 24 hours |

## System Contracts

### POST /api/auth/login
**Request:** `{ email: string, password: string }`
**Response 200:** `{ token: string, user: { id, email, role } }`
**Response 401:** `{ error: "Invalid email or password" }`

### ADR-001: JWT over Server-Side Sessions
**Context:** PRD specifies JWT. Server-side sessions offer revocation but add infrastructure.
**Decision:** Use JWT with RS256 signing per PRD intent.
**Consequences:** No immediate token revocation — mitigate with short expiry + refresh tokens.
```

And an excerpt from the **Feature Blueprint for User Authentication**:

```markdown
# Feature Blueprint: FB-001 User Authentication

**FRD:** FRD-001
**Components Used:** Identity Management, Notification Delivery

## Component Composition

The login flow uses Identity Management's `POST /api/auth/login` contract.
On successful registration, Notification Delivery sends a welcome email
via the `user.registered` event.

## Feature-Specific Components

### LoginForm (client-app)
- Email + password inputs with client-side validation
- Error display area for server responses
- Submits to Identity Management login contract

### RegistrationForm (client-app)
- Email, password, confirm password inputs
- Password strength indicator
- Submits to Identity Management registration contract
```

**Phase 4 — Cross-Blueprint Consistency**

The agent checks that field names, data types, and contract shapes are identical everywhere they appear. No silent contradictions.

### Human Touchpoints


| Phase                 | What You Decide                                                               |
| --------------------- | ----------------------------------------------------------------------------- |
| 2 — Capability Mining | Approve or revise the component list (highest-leverage architecture decision) |


Phases 1, 3, and 4 run with minimal human input — the agent authors autonomously.

### Common Mistakes to Avoid

- Data models without field definitions ("User model with standard fields") — every field must be explicit
- Feature Blueprints that redefine a capability instead of composing a Component Blueprint
- Missing ADRs — even "obvious" decisions like choosing bcrypt need a record

---

## Step 3: Planner — Blueprints to Work Orders

> **Invoke:** "Use prd-to-work-orders to run the Planner stage from the Blueprints"

The Planner reads all Blueprints and FRDs, then produces Work Orders — self-contained task packages that a coding agent can pick up and implement without asking questions.

### What Happens

**Phase 1 — Component Epic Extraction**

Each Component Blueprint becomes an Epic. The agent slices it into Stories:

```markdown
## CE-001: Identity Management

- WO-001: User Data Model & Repository
- WO-002: Authentication Service (login/logout)
- WO-003: Session Management (JWT issuance/validation)
- WO-004: Role-Based Access Control Middleware
```

**Phase 2 — Feature Epic Extraction**

Each Feature Blueprint becomes an Epic. Stories reference Component Work Orders they depend on:

```markdown
## FE-001: User Authentication

- WO-010: Login Flow (depends on WO-002, WO-003)
- WO-011: Registration Flow (depends on WO-001)
- WO-012: Password Reset Flow (depends on WO-002)
```

**Phase 3 — Context Graph Assembly**

Every Work Order gets an inline context graph. Here's what WO-010's looks like:

```markdown
## Context Graph

- **Depends on:** WO-001 (User model), WO-002 (Auth service), WO-003 (Sessions)
- **Depended on by:** WO-020 (Task assignment — needs authenticated user)
- **Shares contract with:** WO-011 (both use Identity Management contracts)
- **Related Blueprint:** FB-001 User Authentication, Identity Management
- **Related FRD:** FRD-001, REQ-AUTH-001, REQ-AUTH-002
```

This graph is what enables agents to autonomously navigate to related context when they need it.

**Phase 4 — Phasing & Sequencing**

Work Orders are sorted into execution phases. Component Epics go first (they provide the foundation), Feature Epics follow:

```markdown
## Execution Phases

| Phase | Epics | Work Orders | Rationale |
|-------|-------|-------------|-----------|
| 1 | CE-001, CE-002, CE-003 | WO-001 through WO-009 | Foundation — no dependencies |
| 2 | FE-001, FE-002 | WO-010 through WO-018 | Depend on Phase 1 components |
| 3 | FE-003, FE-004 | WO-019 through WO-025 | Depend on Phase 1 + 2 |
```

### Complete Work Order Example

Here's WO-010 in full — this is what a coding agent receives:

```markdown
# WO-010: Login Flow

**Epic:** FE-001 User Authentication
**Status:** Ready
**Complexity:** M
**Phase:** 2

## Context Graph
- **Depends on:** WO-001 (User model), WO-002 (Auth service), WO-003 (Sessions)
- **Depended on by:** WO-020 (Task assignment)
- **Shares contract with:** WO-011 (Registration flow)
- **Related Blueprint:** FB-001 §Component Composition, Identity Management §Contracts
- **Related FRD:** FRD-001, REQ-AUTH-001

## Summary
Implement the user login flow: LoginForm component on the client,
login API endpoint on the server, JWT token handling.

## In Scope
- LoginForm component with email/password inputs and error display
- POST /api/auth/login endpoint integration
- JWT token storage in client (httpOnly cookie)
- Redirect to dashboard on success

## Out of Scope
- Registration flow (WO-011)
- Password reset (WO-012)
- Social login / OAuth providers
- Rate limiting (WO-004 handles this at middleware level)

## Requirements
- AC-AUTH-001.1: When a user submits a valid email and password, the system
  shall authenticate and redirect to the dashboard within 2 seconds.
- AC-AUTH-001.2: When a user submits an invalid password, the system shall
  display "Invalid email or password" without revealing which field is wrong.
- AC-AUTH-001.3: When a user submits an unregistered email, the system shall
  display the same "Invalid email or password" message.

## Blueprints
- Identity Management §System Contracts: POST /api/auth/login
- FB-001 §Feature-Specific Components: LoginForm
- Identity Management §Data Models: User, Session

## E2E Acceptance Tests

### COV_AUTH_LOGIN_SUCCESS
**Covers:** AC-AUTH-001.1
**Preconditions:** User exists with email "test@taskflow.com", password "ValidPass123!"
**Steps:**
1. Navigate to /login
2. Fill email field with "test@taskflow.com"
3. Fill password field with "ValidPass123!"
4. Click "Log In" button
**Assert:** URL changes to /dashboard within 2 seconds. Dashboard displays user name.

### COV_AUTH_LOGIN_INVALID_PASSWORD
**Covers:** AC-AUTH-001.2
**Preconditions:** User exists with email "test@taskflow.com"
**Steps:**
1. Navigate to /login
2. Fill email field with "test@taskflow.com"
3. Fill password field with "WrongPass!"
4. Click "Log In" button
**Assert:** Error message "Invalid email or password" is visible. URL remains /login.

### COV_AUTH_LOGIN_UNREGISTERED
**Covers:** AC-AUTH-001.3
**Steps:**
1. Navigate to /login
2. Fill email field with "nobody@taskflow.com"
3. Fill password field with "AnyPass123!"
4. Click "Log In" button
**Assert:** Error message "Invalid email or password" is visible. URL remains /login.

## Subtasks
- WO-010.1: Write failing E2E test for login success
- WO-010.2: Implement LoginForm component
- WO-010.3: Integrate with POST /api/auth/login endpoint
- WO-010.4: Implement JWT token storage
- WO-010.5: Implement dashboard redirect
- WO-010.6: Write failing E2E tests for error cases
- WO-010.7: Implement error handling
- WO-010.8: Verify all tests pass, commit
```

### Human Touchpoints

The Planner runs largely autonomously. Review the execution phases to confirm the sequencing makes sense.

### Common Mistakes to Avoid

- Paraphrasing acceptance criteria instead of copying verbatim from the FRD
- Leaving "Out of Scope" empty — agents need explicit boundaries
- Orphan Work Orders with no context graph connections

---

## Step 4: Validator — Drift Detection & Feedback

> **Invoke:** "Use prd-to-work-orders to run Validator against the implemented code"  
> or: "Use prd-to-work-orders to run a checkpoint audit for CE-001"

The Validator compares implemented code against Blueprints and FRDs. It classifies mismatches as minor (auto-corrected) or major (escalated), and transforms feedback into corrective Work Orders.

### When to Run

- **Checkpoint audits:** After each Epic or Phase completes — catches drift early
- **Final review:** After all Work Orders are complete — full codebase sweep
- **Feedback ingestion:** Anytime you have user feedback or test results

### Sample Drift Report

After a coding agent finishes CE-001 (Identity Management), you run a checkpoint:

```markdown
# Drift Report: Checkpoint CE-001

**Date:** 2026-05-08
**Scope:** CE-001 Identity Management
**Status:** Minor drift

### DRIFT-001: Naming Mismatch
**Severity:** Minor
**Category:** Naming

**Expected:** Blueprint says field name `user_id` in Session model
**Source:** Identity Management §Data Models

**Actual:** Code uses `userId` in `src/models/session.ts`, line 12
**Impact:** Inconsistency with other models that use snake_case

**Corrective action:** CWO-001 generated

### DRIFT-002: Authentication Strategy Mismatch
**Severity:** Major
**Category:** Architecture

**Expected:** Blueprint specifies JWT with RS256 signing (ADR-001)
**Source:** Identity Management §ADR-001

**Actual:** Code uses express-session with server-side session store
in `src/middleware/auth.ts`, lines 15-30
**Impact:** Entire session management architecture differs from Blueprint

**Corrective action:** Escalated to human

**DRIFT-002 requires human decision:**

**Option A — Fix the code:**
Refactor auth.ts to use jsonwebtoken with RS256. Remove express-session.
Estimated complexity: M (affects auth middleware, login endpoint, all
protected routes).

**Option B — Update the specs:**
Update Identity Management Blueprint ADR-001 to specify server-side
sessions. Update Session data model to reflect session store schema.
The agent's approach may be valid if token revocation is a priority.

**Recommendation:** Option A — the PRD explicitly requested JWT.

## Summary
| Severity | Count |
|----------|-------|
| Minor | 1 |
| Major | 1 |
| **Total** | **2** |

**Corrective Work Orders generated:** CWO-001
**Major items escalated:** DRIFT-002
```

### Sample Corrective Work Order

CWO-001 (auto-generated for the naming mismatch) follows the same template as Planner Work Orders:

```markdown
# CWO-001: Fix Session Model Field Naming

**Epic:** CE-001 Identity Management
**Status:** Ready
**Complexity:** S
**Phase:** Current

## Context Graph
- **Triggered by:** DRIFT-001 (naming mismatch)
- **Related Work Order:** WO-003 (Session Management)
- **Related Blueprint:** Identity Management §Data Models
- **Related FRD:** FRD-001, REQ-AUTH-002

## Summary
Rename `userId` to `user_id` in Session model to match Blueprint convention.

## In Scope
- Rename field in Session model
- Update all references in auth middleware and login endpoint

## Out of Scope
- Any logic changes to session handling

## Requirements
- AC-AUTH-002.1: When a session token expires, the system shall redirect
  to login and display "Your session has expired. Please log in again."

## Blueprints
- Identity Management §Data Models: Session

## E2E Acceptance Tests
- Existing COV_AUTH_LOGIN_SUCCESS must continue to pass after rename
```

### Feedback Ingestion Example

You can also feed user feedback or test results to the Validator:

```markdown
# User Feedback — 2026-05-10

## FB-001: No Error on Wrong Password
**Type:** Bug
**Observed in:** Login screen
**Description:** When I type the wrong password, nothing happens.
  The form just sits there.
**Expected:** Should show an error message.
```

The Validator maps this to FRD-001, AC-AUTH-001.2 ("shall display 'Invalid email or password'"), classifies it as an implementation gap, and generates CWO-003.

For test results, the Validator reads test output, groups failures by root cause, and generates one CWO per root cause — not one per individual failure.

### Human Touchpoints


| Situation     | What You Decide                                       |
| ------------- | ----------------------------------------------------- |
| Minor drift   | Nothing — auto-generates CWOs                         |
| Major drift   | Choose Option A (fix code) or Option B (update specs) |
| User feedback | Review the feedback processing report                 |


### Common Mistakes to Avoid

- Fixing drift directly in code without creating a Work Order — breaks traceability
- Classifying architectural mismatches as "minor" to avoid escalation
- Generating one CWO per test failure instead of per root cause

---

## Quick Reference

### The Umbrella Skill at a Glance


| Skill | Internal Stage | Input | Output | Human Decisions |
| ------ | -------------- | ----- | ------ | --------------- |
| `prd-to-work-orders` | Refinery | Raw PRD | `docs/refinery/` | Gap resolution, persona approval, feature tree, FRD review |
| `prd-to-work-orders` | Foundry | `docs/refinery/` | `docs/foundry/` | Capability map approval |
| `prd-to-work-orders` | Planner | `docs/foundry/` + `docs/refinery/` | `docs/planner/` | Execution phase review |
| `prd-to-work-orders` | Validator | All `docs/` + code | `docs/validator/` | Major drift decisions |


### ID Convention Reference


| ID Pattern        | Meaning                      | Example                |
| ----------------- | ---------------------------- | ---------------------- |
| `FRD-NNN`         | Feature Requirement Document | FRD-001                |
| `REQ-PREFIX-NNN`  | Requirement within an FRD    | REQ-AUTH-001           |
| `AC-PREFIX-NNN.N` | Acceptance Criterion         | AC-AUTH-001.2          |
| `CE-NNN`          | Component Epic               | CE-001                 |
| `FE-NNN`          | Feature Epic                 | FE-001                 |
| `WO-NNN`          | Work Order (Story)           | WO-010                 |
| `WO-NNN.N`        | Subtask                      | WO-010.3               |
| `CWO-NNN`         | Corrective Work Order        | CWO-001                |
| `DRIFT-NNN`       | Drift item                   | DRIFT-002              |
| `COV_NAME`        | E2E test coverage ID         | COV_AUTH_LOGIN_SUCCESS |
| `ADR-NNN`         | Architecture Decision Record | ADR-001                |


---

## FAQ

**Can I start from the Foundry if I already have FRDs?**
Yes — if your FRDs follow the Refinery template format (traceable IDs, testable ACs, proper user stories). Place them in `docs/refinery/` and invoke `prd-to-work-orders`; it will detect that Refinery is complete and route to Foundry.

**What if my PRD is just a conversation transcript or rough notes?**
The Refinery handles messy input. Its Phase 1 (Gap Detection) is specifically designed to find what's missing, ambiguous, or contradictory and ask you to resolve it.

**Can I run the Validator mid-implementation?**
Yes — checkpoint audits are designed for this. Run one after each Epic or Phase completes. Catching drift early is far cheaper than catching it at the end.

**What if the Validator says my code is wrong but the spec is outdated?**
Major drift escalation always presents two options: fix the code OR update the Blueprint/FRD. The spec isn't always right — sometimes the coding agent found a better approach.

**Can multiple agents work on different Work Orders in parallel?**
Yes. That's why the context graph exists. Work Orders in the same phase with no dependencies between them can be implemented concurrently. Each agent uses the context graph to find related specs without stepping on another agent's work.

**Do I need all four skills for a small project?**
For very small projects, you can use Refinery + Planner (skip Foundry for projects with no shared capabilities). But for anything with 3+ features that share behavior (auth, notifications, data access), the Foundry adds significant value by preventing duplicate implementations.