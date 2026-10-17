# TS Harness Vision

## One-line summary

**TS Harness is the governed control plane for multi-agent organizational work—starting with product and engineering, but built so any domain can plug in through configuration, not code forks.**

## What we are building

TS Harness is a **governed, multi-agent operating system** for work inside an organization.

People express intent in natural language (Slack, Discord, Telegram, or API). The platform classifies that intent, applies policy, and routes it to the right outcome:

- **Ephemeral delegation** — a specialist agent handles a one-off task in chat.
- **Durable workflows** — a multi-step process with checkpoints, human gates, revise loops, and audit trails.

The orchestrator is a **router**, not a domain expert. Specialists hold domain knowledge. Workflows encode process. Policy enforces boundaries. Memory persists state across sessions, agents, and time.

This applies to **any kind of organizational work**: product, engineering, sales, marketing, legal, finance, support, operations, and whatever comes next. Product and engineering are where we are **starting**, not where the platform ends.

## What we are not building

- A chatbot that improvises delivery end-to-end without governance.
- A product-management tool whose identity is tied to PRDs or Jira.
- A hardcoded pipeline where every new work type requires runtime changes.

PRD-first governed delivery is our **first end-to-end proof** that durable workflows, intent routing, human gates, and opt-in handoffs work in production channels. It is a **reference implementation**, not the definition of the product.

## Design principles

| Principle | Meaning |
|-----------|---------|
| **Work types are data** | New domains add workflow skeletons, routing rules, agents, and skills—not new orchestrator branches. |
| **Governance is generic** | Confirm before start, approve/reject gates, revise loops, supersede/complete lifecycle—reusable across domains. |
| **Routing is declarative** | Intent classification and policy live in config (`routing.intents`, feature flags, policy pipeline) with code as fallback only. |
| **Specialists are pluggable** | Agents, toolsets, and skills are entries in runtime config; the gateway resolves and dispatches them uniformly. |
| **Durable beats ephemeral when it matters** | Chat delegation is fine for informal work; governed processes get workflow records, task state, and audit. |
| **Human gates stay human** | Agents propose and execute within policy; people approve, reject, and opt into downstream pipelines. |
| **Audit by default** | Tool calls, workflow transitions, and policy decisions are traceable for compliance and debugging. |

## Platform layers

```text
┌─────────────────────────────────────────────────────────────┐
│  Channels (Slack, Discord, Telegram, HTTP)                  │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│  Gateway — auth, sessions, intent routing, policy, confirm  │
└───────────────────────────┬─────────────────────────────────┘
                            │
          ┌─────────────────┼─────────────────┐
          ▼                 ▼                 ▼
   ┌────────────┐   ┌──────────────┐   ┌─────────────┐
   │ Delegate   │   │ Start /      │   │ Cron /      │
   │ to         │   │ continue     │   │ heartbeat   │
   │ specialist │   │ workflow     │   │ automation  │
   └──────┬─────┘   └──────┬───────┘   └──────┬──────┘
          │                │                  │
          └────────────────┼──────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Agent runtime — tool loop, skills, MCP, sandbox            │
└───────────────────────────┬─────────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  Memory API — workflows, tasks, artifacts, tenant scope     │
└───────────────────────────┬─────────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  Workflow runner — claim tasks, dispatch, hooks, complete   │
└─────────────────────────────────────────────────────────────┘
```

Configuration (`config/ts-harness-runtime.yaml`, workflow JSON under `config/workflows/`, skills under `skills/`) sits **above** this stack and defines *what* runs. The runtime defines *how* it runs safely and consistently.

## First reference domain: product and engineering

We exercise the platform on product and engineering work because it stress-tests the hard parts:

1. **Ambiguous intent** — "Create a PRD from these notes" vs "quick draft, no approval."
2. **Human gates** — draft → review → approve or revise.
3. **Opt-in handoffs** — PRD approved does not automatically start delivery; the user chooses to continue.
4. **Closed pipelines** — after opt-in, delivery stages run as a governed sequence (e.g. Jira → implement → deploy).
5. **Policy enforcement** — governed PRD intent must not silently fall back to chat delegation.

The canonical flow is documented in [PRD-first delivery flow](./prd-first-delivery-flow.md). Treat it as a **pattern library**, not a ceiling:

- **Intake → produce artifact → gate → optional downstream pipeline** generalizes to RFPs, contracts, campaign briefs, incident runbooks, hiring packets, and more.
- **Revise on reject** is a generic loop, not PRD-specific logic in the engine.
- **Continue / bridge hooks** connect one workflow to another when the user opts in.

## Adding a new work type

Adding a domain should follow a checklist, not a rewrite:

1. **Workflow skeleton** — JSON under `config/workflows/` with steps, gates, hooks, and `on_complete` / `on_reject` behavior.
2. **Routing signals** — entries in `routing.intents` (or classifier extensions) so channel ingress maps phrases to the right workflow or delegation path.
3. **Policy rules** — deny unsafe shortcuts (e.g. delegate when a governed workflow is required).
4. **Specialists** — agent entries with toolsets and skills appropriate to the domain.
5. **Session metadata** — namespaced keys for phase tracking; avoid baking one domain's fields into core gateway logic.
6. **Hooks** — small, composable units registered in the workflow engine; domain logic lives in hooks and config, not scattered conditionals.
7. **Smoke tests and release gates** — prove the skeleton and routing work before promotion.

If a change requires editing the orchestrator's core loop for one domain, that is a design smell—prefer config, hooks, or policy.

## Success criteria

We know the vision is working when:

- A new work type can ship as **config + hooks + tests** without forking the runtime.
- Operators can trace any governed outcome from user message → intent → workflow/task → tool audit.
- Informal and governed paths coexist: users choose speed vs durability through intent and confirmation, not hidden defaults.
- Product/engineering remains a **supported vertical**, not the only story we tell about the platform.

## Generic session and bridge config

Platform session metadata uses generic keys: `workflow_phase`, `approved_artifact`, `source_workflow_id`, `pending_bridge`, modes `workflow_linked` and `bridge_linked`. Workflow skeletons declare `session.phase_tracking`, `bridge.target_workflow_type`, and routing rules declare `policy` — see [PRD-first delivery flow](./prd-first-delivery-flow.md) as the reference vertical.

## Related documentation

- [Harness blueprint](./harness-blueprint.md) — modular composition model and client-project practices
- [Agent runtime](./agent-runtime.md) — gateway, loop, tools, channels
- [Workflow engine](./workflow-engine.md) — task lifecycle and dispatch
- [Workflow hooks](./workflow-hooks.md) — composable step and transition behavior
- [PRD-first delivery flow](./prd-first-delivery-flow.md) — first reference workflow (product/engineering)
- [Governed delivery runbook](../runbooks/governed-delivery-flow.md) — operational guide for the reference domain
