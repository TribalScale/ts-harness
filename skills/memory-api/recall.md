# Recall (API) — Search Memory via HTTP

> **Use this skill if you do not have the `exec` tool.** It calls the memory-api sidecar at `http://127.0.0.1:18792` via the `fetch` tool. Agents with `exec` should use `skills/memory/recall.md` instead.

Search and retrieve relevant memories from the Postgres-backed TS Harness memory database.

## When to use

- Before answering any question
- Before starting a task
- When the user references the past
- When working in a specific scope (team, project, user)

## Steps

1. **Determine search context:** topic/question text, optional `scope`, optional `limit` (default: 10).

2. **POST to the memory API** using the `fetch` tool:

   ```json
   POST http://127.0.0.1:18792/memory/recall
   Content-Type: application/json

   {
     "query": "How does the auth service handle tokens?",
     "scope": "project:payments-api",
     "limit": 10
   }
   ```

   Omit `scope` to search across all scopes.

3. **Check the response**:
   - `200 OK` → `{ "results": [ ... ] }` (array of memory rows)
   - `400` → fix the request body (missing `query`)
   - `500` → memory-api is down; proceed without memory context and note this

4. **Filter and prioritize results**:
   - Discard rows with `confidence < 0.5` unless no other matches exist.
   - **Prioritize `directive`** memories — they represent rules and preferences.
   - Sort by `similarity` DESC (already ordered by the API).

5. **Present results** to the calling context as structured information:
   - List each memory with `id`, `type`, `content`, `scope`, `confidence`, `similarity`.
   - Call out `directive` memories explicitly as rules.

## Request fields

| Field   | Type   | Description                                   |
|---------|--------|-----------------------------------------------|
| `query` | string | Natural language search query (**required**)  |
| `scope` | string | Optional scope filter                         |
| `limit` | int    | Max results to return (default: `10`)         |

## Response format

```json
{
  "results": [
    {
      "id":         "<uuid>",
      "type":       "semantic",
      "content":    "The auth service uses JWT with RS256.",
      "scope":      "project:payments-api",
      "confidence": "1.0",
      "created_at": "2025-01-01T00:00:00Z",
      "similarity": "0.95"
    }
  ]
}
```

## Example fetch call (pseudocode)

```
result = fetch("http://127.0.0.1:18792/memory/recall", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    query: "deployment approval requirements",
    scope: "global",
    limit: 5
  })
})
memories = result.json().results
```
