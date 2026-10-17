# Forget (API) — Expire or Delete a Memory via HTTP

> **Use this skill if you do not have the `exec` tool.** It calls the memory-api sidecar at `http://127.0.0.1:18792` via the `fetch` tool. Agents with `exec` should use `skills/memory/forget.md` instead.

Expire or permanently delete a memory from the Postgres-backed TS Harness memory database.

## When to use

- A fact has been explicitly corrected by the user (expire the old version first, then remember the new)
- A memory is confirmed to be outdated or no longer relevant
- Privacy/compliance requires removal of a specific record

**Prefer `expire` over `delete`** — expiry preserves the audit trail while hiding the memory from future recall. Only use `delete` when compliance requires permanent removal.

## Steps

1. **Identify the memory id** to forget. Use `recall.md` first to confirm the exact `id`.

2. **POST to the memory API** using the `fetch` tool:

   ```json
   POST http://127.0.0.1:18792/memory/forget
   Content-Type: application/json

   {
     "id":     "<uuid>",
     "action": "expire",
     "reason": "User confirmed the auth service switched to RS512.",
     "actor":  "orchestrator"
   }
   ```

3. **Check the response**:
   - `200 OK` → `{ "ok": true, "action": "expire", "id": "<uuid>" }`
   - `400` → fix the request body (missing `id`)
   - `500` → memory-api is down; log the error and handle manually

4. **After expiring, remember the correction** (if applicable) using `remember.md`.

## Request fields

| Field    | Type   | Description                                              |
|----------|--------|----------------------------------------------------------|
| `id`     | string | UUID of the memory to forget (**required**)              |
| `action` | string | `"expire"` (default) or `"delete"` (permanent)           |
| `reason` | string | Reason for forgetting (logged in audit trail)            |
| `actor`  | string | Agent or user performing the action (default: `"memory-api"`) |

## Response format

```json
{
  "ok":     true,
  "action": "expire",
  "id":     "<uuid>"
}
```

## Example fetch call (pseudocode)

```
result = fetch("http://127.0.0.1:18792/memory/forget", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    id:     "a1b2c3d4-...",
    action: "expire",
    reason: "User corrected: auth service uses RS512, not RS256",
    actor:  "orchestrator"
  })
})
```
