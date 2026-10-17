# Remember (API) — Store a Memory via HTTP

> **Use this skill if you do not have the `exec` tool.** It calls the memory-api sidecar at `http://127.0.0.1:18792` via the `fetch` tool. Agents with `exec` should use `skills/memory/remember.md` instead.

Store a new memory (or update an existing one) in the Postgres-backed TS Harness memory database.

## When to use

- After learning a new fact
- After a decision is made
- After a user correction
- After completing a task (outcomes, lessons)
- When discovering a process or convention worth retaining

Do **not** store secrets, raw transcripts, or duplicates.

## Steps

1. **Determine memory type** (`episodic`, `semantic`, `procedural`, `relational`, or `directive`) — see the type guide in `workspace/MEMORY.md`.

2. **Determine scope** (narrowest accurate scope you are allowed to write):
   - `tenant:<tenant>:agent:<agent>` — your own notes
   - `project:<id-or-slug>`
   - `user:<id-or-handle>`
   - Do not send `global`, `department:<name>`, or `team:<slug>`. The API rejects `global` and `department:`. Department memory is created only when a person promotes an approved program brief.

3. **Assess confidence**:
   - `1.0` — explicit user statements, confirmed facts
   - `0.8` — reasonably inferred
   - `0.6` — uncertain

4. **POST to the memory API** using the `fetch` tool:

   ```json
   POST http://127.0.0.1:18792/memory/remember
   Content-Type: application/json

   {
     "type":       "semantic",
     "content":    "The auth service uses JWT with RS256.",
     "scope":      "project:payments-api",
     "source":     "orchestrator",
     "confidence": 1.0,
     "tags":       ["auth", "jwt"],
     "metadata":   {"ticket": "PROJ-123"}
   }
   ```

5. **Check the response**:
   - `200 OK` → `{ "id": "<uuid>", "action": "created" | "updated" }`
   - `400` → fix the request body (missing `content`)
   - `500` → memory-api is down; log the error and continue without storing

6. **Record the memory id** returned in the response for future reference or linking.

## Required fields

| Field      | Type   | Description                                      |
|------------|--------|--------------------------------------------------|
| `content`  | string | The fact or observation to store (**required**)  |
| `type`     | string | Memory type (default: `"semantic"`)              |
| `scope`    | string | Agent, project, or user scope. `global` and `department:` are rejected. |
| `source`   | string | Caller identifier (default: `"memory-api"`)      |
| `confidence` | float | 0.0–1.0 (default: `1.0`)                       |
| `tags`     | array  | Optional string tags                             |
| `metadata` | object | Optional JSON metadata                           |

## Example fetch call (pseudocode)

```
result = fetch("http://127.0.0.1:18792/memory/remember", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    type: "directive",
    content: "Always cc the team lead on RDS change requests.",
    scope: "global",
    source: "orchestrator",
    confidence: 1.0
  })
})
```
