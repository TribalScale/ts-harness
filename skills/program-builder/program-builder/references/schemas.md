# Definition schemas

Unknown keys are rejected. Validation issues name the exact path to fix.

## Workflow

Top level:

- `workflow_type`: new, lowercase snake_case.
- `definition_version`: `1.0.0` for a new workflow.
- `display_name`: optional.
- `stages`: the list of stages (required).
- `contracts`: optional named output schemas that stages reference with `contract_ref`.

Each stage:

| Field | Meaning |
|---|---|
| `name` | Unique stage name. Other stages refer to it in `depends_on`. |
| `kind` | `task` for normal work. `expand` and `compensate` are advanced; avoid them. |
| `agent_id` | Required. An agent from `builder_catalog.agent_ids`. Gates use `orchestrator`. |
| `depends_on` | Names of the stages that must finish first. The first stage has none. Every other stage must be reachable from it. |
| `approval_required` | `true` makes the stage a human gate. Downstream stages wait for approval. |
| `present_prompt` | `approval` on gates. |
| `present_output` | `true` posts the stage output to the person's thread. |
| `skill_hints` | Approved skill ids (or ids you proposed) that guide the agent. |
| `tool_allow` | The stage's whole tool set. Every tool must be one the agent has (`builder_catalog.agent_tools`). Omit it to give the stage all of the agent's tools. |
| `input_map` | Optional. Maps input keys to `$input.<key>`, `$workflow.<key>`, or `$prior.<stage>.output.<key>`. |

A minimal gate:

```json
{"name": "approve_draft", "kind": "task", "agent_id": "orchestrator", "depends_on": ["draft"], "approval_required": true, "present_prompt": "approval"}
```

## Program

Top level:

- `program_type`: new, lowercase snake_case.
- `definition_version`: `1.0.0`.
- `display_name`: optional.
- `startable_nodes`: the node ids a person can start from.
- `nodes`: the program's nodes.
- `edges`: transitions between nodes. Use `[]` for one node.

Each node:

| Field | Meaning |
|---|---|
| `id` | Node id. Routing `entry_phase` uses it. |
| `default_workflow_type` | The workflow this node runs (your new `workflow_type`). |
| `workflow_variants` | `[]` unless you have a reason. |
| `required_artifacts` | Artifacts that must exist before the node starts. Use `[]` for the first node. |
| `produces_artifacts` | Names the node's completion adapter stores. |
| `input_bindings` | Usually `{"kind": "program_summary", "target": "summary"}` and `{"kind": "transcript_context", "target": "context", "limit": 20}`. Kinds: `program_summary`, `artifact` (needs `name`), `static` (needs `value`), `transcript_context`. |
| `completion_adapter` | `extract_declared_outputs` for new programs. |
| `terminal` | `true` on the last node. |

| `reentry` | Optional. Makes the node loop over a list. See below. |

Each edge has `source`, `target`, `mode` (`manual` or `automatic`), `confirm_required`, and `signals`.

### Looping a node (`reentry`)

A node can run once per item of a list artifact. Edges must not form cycles; the loop is declared on the node itself.

| Field | Meaning |
|---|---|
| `target_node` | Must be the node's own id. |
| `remaining_list_artifact` | The list to iterate. An upstream node or the start artifacts must provide it. |
| `next_item_artifact` | The artifact that holds the current item. Add it to the node's `required_artifacts`. Default `next_item`. |
| `completed_list_artifact` | Where finished items are recorded. Default `completed_<remaining list>`. |
| `mode` | `list`. Inferred when `remaining_list_artifact` is not `work_order_ids`. |
| `max_iterations` | 1 to 500, default 50. The program is cancelled when the loop hits it. |

The loop node must not list `remaining_list_artifact` or `completed_list_artifact` in its `produces_artifacts`.

Example: a `collect` node produces `account_ids`; `summarize` runs once per account.

```json
{
  "id": "summarize",
  "default_workflow_type": "account_summary",
  "workflow_variants": [],
  "required_artifacts": ["account_id"],
  "produces_artifacts": ["account_summary"],
  "input_bindings": [{"kind": "program_summary", "target": "summary"}],
  "completion_adapter": "extract_declared_outputs",
  "terminal": true,
  "reentry": {
    "target_node": "summarize",
    "remaining_list_artifact": "account_ids",
    "next_item_artifact": "account_id",
    "max_iterations": 100
  }
}
```

When the list is done, the node follows its single automatic edge, or the program succeeds if there is none.

## Routing intent

| Field | Meaning |
|---|---|
| `id` | Unique intent id; use the program type. |
| `signals` | Phrases that start the program. Two or more, specific to this program. |
| `program_type` | Your new program type. |
| `entry_phase` | A node id from `startable_nodes`. |
| `exclude` | Optional phrases that stop a match. |

The builder adds your intent to the active routing intents. It does not change the others.
