# Adapter Contract

This document defines the contract between `verified-merge` and pluggable source adapters. Every adapter in `skills/integrate/ts-source-*/` must implement these operations.

## NormalizedDeliveryContext

The single data shape every adapter produces via `fetch`:

```yaml
source:
  type: work-order | jira | linear | github-issues | azure-devops
  id: WO-003 | PROJ-123 | ...
  ref: docs/planner/.../WO-003.md | PROJ-123 | ...
title: Short title of the unit of work
description: Longer description or summary
requirements:
  - Verbatim or structured requirement strings
acceptanceCriteria:
  - id: AC-001 | COV_PREFIX_NAME
    given: Precondition
    when: Action
    then: Expected result
dependencies:
  - Unit ids that must be Done before this unit starts
scopeBoundaries:
  inScope:
    - Explicit deliverables
  outOfScope:
    - Explicit exclusions
verification:
  localCommands:
    - Commands to run before opening a PR
  postMergeCommands:
    - Commands to run after merge on base branch
branchHint: wo-003-user-export | proj-123-feature-name
externalRefs:
  jira: PROJ-123
  linear: null
capabilities:
  canTransition: true | false
  canComment: true | false
  canLink: true | false
  canUpdateStatus: true | false
```

Rules:

- Requirements should be verbatim from the source when the source defines them.
- Acceptance criteria must be testable. Convert E2E test specs into scenario form when needed.
- Dependencies must reflect blocking prerequisites, not soft ordering preferences.
- `branchHint` is a suggestion; the orchestrator may normalize it per project config.

## Adapter Operations

Each adapter documents and implements four operations.

### detect(input) -> bool

Returns true when this adapter owns the user's input shape.

Examples:

| Adapter | Detects |
|---|---|
| Governed task input | `work_order_body` present on workflow task input (ts_harness `implement_work_orders`) — **no filesystem adapter** |
| `source-work-order` | `WO-###`, `CWO-###`, paths under `docs/planner/**` (local / non-governed) |
| `ts-source-jira` | Configured key pattern such as `PROJ-###` |

When multiple adapters match, prefer the most specific match (governed `work_order_body` > path > id pattern > config default).

For the governed path, build NormalizedDeliveryContext from `work_order_id` + `work_order_body` (+ `repo_url` / `base_branch` when provided). Implement via `run_coding_backend`, not sandbox `exec`.

### fetch(id|ref) -> NormalizedDeliveryContext

Loads the unit of work and maps it to NormalizedDeliveryContext.

- Must not invent requirements or acceptance criteria.
- Record unknowns in the description or leave acceptance criteria empty and let the orchestrator stop for clarification.
- For Work Orders, read context graph dependencies and block if prerequisites are not Done.

### on_event(event, payload) -> void

Reacts to lifecycle events emitted by the orchestrator. The core writes delivery state to `docs/verified-merge/delivery.json`; the adapter handles source-specific sync.

Events:

| Event | Typical adapter action |
|---|---|
| `context_accepted` | Acknowledge start; optional status sync |
| `in_development` | Transition external tracker or mirror WO status |
| `pr_opened` | Link PR URL; comment on external tracker |
| `pr_updated` | Update PR link or comment |
| `ci_green` | Optional status comment |
| `merged` | Record merge timestamp in delivery state (core); optional tracker comment |
| `post_merge_verified` | Mark complete; transition external tracker to done |
| `blocked` | Record blocker; optional tracker comment |

Payload fields the orchestrator may include:

```yaml
unitId: WO-003 | PROJ-123
branch: branch-name
prUrl: https://github.com/.../pull/42
mergeCommit: abc123
verificationEvidence: Command output or summary
status: In Progress | In Review | Done
```

Adapters must never invent external transitions or claim sync actions that did not occur.

### capabilities() -> object

Returns the adapter's sync capabilities (same shape as `capabilities` in NormalizedDeliveryContext).

## Routing Rules

The orchestrator resolves an adapter in this order:

1. **Auto-detect from input** — run `detect()` on each registered adapter.
2. **Configured default** — use `delivery.source` from project config when input is ambiguous.
3. **Ask** — if no adapter resolves, ask the user which source to use.

Registered adapters (initial set):

- `source-work-order` (default when config is silent)
- `ts-source-jira`

## Delivery State Store

The orchestrator owns `docs/verified-merge/delivery.json`. Adapters do not write to upstream planning manifests such as `docs/prd-to-work-orders/manifest.json`.

Schema:

```json
{
  "units": {
    "WO-003": {
      "source": "work-order",
      "status": "In Review",
      "branch": "wo-003-user-export",
      "prUrl": "https://github.com/.../pull/42",
      "mergedAt": null,
      "verifiedAt": null,
      "externalRefs": {
        "jira": "PROJ-123"
      }
    }
  }
}
```

The orchestrator updates this file on every lifecycle event. Adapters may read it for resumability but must not replace the orchestrator as the writer.

## Work Order Status Mirror (optional)

When `mirror_status_to_work_order: true` is configured, `source-work-order` may update the `Status` field in `docs/planner/**/WO-*.md`. Default is false to preserve Planner ownership boundaries.

Status mapping:

| Lifecycle | Delivery status | WO mirror (optional) |
|---|---|---|
| `context_accepted` | In Progress | In Progress |
| `pr_opened` | In Review | In Review |
| `post_merge_verified` | Done | Done |

## Adding a New Adapter

1. Create `skills/integrate/ts-source-<name>/SKILL.md` implementing all four operations.
2. Add `detect` patterns and `on_event` mappings.
3. Register the adapter in `verified-merge` `steps:` frontmatter.
4. Add test scenarios for fetch, routing, and event sync.
5. Run `python scripts/validate_skills.py` and regenerate the catalog.

No orchestrator changes are required beyond registration unless the contract itself evolves.
