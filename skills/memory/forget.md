# Forget — Expire or Remove a Memory

Expire or permanently remove memories in the Postgres-backed TS Harness memory database, with full audit logging and safe correction workflows.

## Database context

**Connection defaults:**

| Setting  | Default        |
| -------- | -------------- |
| Host     | `ts-harness-db`  |
| Port     | `5432`         |
| User     | `ts_harness`     |
| Database | `ts_harness_memory` |

Use `psql` or an equivalent database client for all operations.

**Maintenance:** The database provides **`expire_memories()`** to archive or process expired rows—use it during bulk maintenance after reviewing operational runbooks.

## When to use

- User says information is wrong or outdated
- User says "forget that" or requests removal
- During **monthly maintenance** (expiry sweep, low-confidence cleanup)
- When duplicates are found (expire the redundant row after merging or replacing)

## Forget protocol (non-negotiable)

1. **Never hard-delete without logging.** Every deletion must have a corresponding `memory_audit` row (and capture enough context in `details` to reconstruct what was removed).
2. **Prefer expiry over deletion.** Set `expires_at` so data remains recoverable for review; use `DELETE` only when the user explicitly requests permanent removal or policy requires it.
3. **When correcting wrong information:** create the **new** memory (see **`skills/memory/remember.md`**) **before** expiring the old one—**never leave a gap** in factual coverage.

## Steps

1. **Identify the target memory** by `id` if known, or search:

   ```sql
   SELECT id, type, content, scope, confidence, created_at, expires_at
   FROM memories
   WHERE content ILIKE '%keyword%'
     AND ($SCOPE IS NULL OR scope = $SCOPE OR scope = 'global')
   ORDER BY confidence DESC, created_at DESC
   LIMIT 10;
   ```

2. **Choose the action**
   - **Expire (preferred):** user wrong / "forget" / duplicate / maintenance—set `expires_at = now()` (or a past instant).
   - **Hard delete:** only on **explicit** user request to remove permanently, or strict policy—still log first.

3. **If expiring**

   ```sql
   UPDATE memories
   SET expires_at = now(), updated_at = now()
   WHERE id = $ID;
   ```

4. **If deleting** (only after step 5 logging for audit trail—see ordering below)

   ```sql
   DELETE FROM memories
   WHERE id = $ID;
   ```

5. **ALWAYS log to `memory_audit`** before or in the same transaction as the state change. Include the memory's **`content`** (or a faithful excerpt if very long) in `details` for an audit trail.

   **Before expire (recommended pattern: log then update)**

   ```sql
   INSERT INTO memory_audit (
     id,
     memory_id,
     action,
     actor,
     reason,
     details,
     occurred_at
   ) VALUES (
     gen_random_uuid(),
     $ID,
     'expired',
     $ACTOR,
     'User stated information is incorrect; expiring prior to correction.',
     '{"previous_content": "...", "scope": "..."}'::jsonb,
     now()
   );

   UPDATE memories
   SET expires_at = now(), updated_at = now()
   WHERE id = $ID;
   ```

   **Before delete**

   ```sql
   INSERT INTO memory_audit (
     id,
     memory_id,
     action,
     actor,
     reason,
     details,
     occurred_at
   ) VALUES (
     gen_random_uuid(),
     $ID,
     'deleted',
     $ACTOR,
     'User explicitly requested permanent deletion.',
     '{"deleted_content": "...", "scope": "..."}'::jsonb,
     now()
   );

   DELETE FROM memories WHERE id = $ID;
   ```

   Use `action = 'expired'` for soft expiry; `action = 'deleted'` for hard delete. Replace the JSON string values with the actual content and scope snapshots from step 1.

6. **If the user provided corrected information:** immediately follow **`skills/memory/remember.md`** to store the correction—**before** you finish the turn without a replacement.

   Practical order when replacing wrong facts:

   1. Insert/update the **new** memory (remember skill).
   2. Log `memory_audit` for the old row with `action = 'expired'` and `details` referencing the new memory id if useful.
   3. `UPDATE memories SET expires_at = now() WHERE id = $OLD_ID`.

7. **Bulk maintenance (e.g. monthly)**
   - Call **`expire_memories()`** per your DB procedure (often `SELECT expire_memories();` or `CALL expire_memories();`—confirm in schema/migrations).
   - Review memories with **`confidence < 0.5`**: merge into stronger rows, expire junk, or leave with explicit human review—do not bulk-delete without audit.

   Example pattern after function run:

   ```sql
   SELECT expire_memories();
   ```

   ```sql
   SELECT id, type, content, scope, confidence, created_at
   FROM memories
   WHERE confidence < 0.5
     AND (expires_at IS NULL OR expires_at > now())
   ORDER BY created_at DESC
   LIMIT 50;
   ```

## Example SQL summary

| Goal              | Core SQL |
| ----------------- | -------- |
| Find candidate    | `SELECT ... FROM memories WHERE content ILIKE ...` |
| Expire            | `INSERT memory_audit (... 'expired' ...);` then `UPDATE memories SET expires_at = now() WHERE id = ...` |
| Delete            | `INSERT memory_audit (... 'deleted' ...);` then `DELETE FROM memories WHERE id = ...` |
| Sweep expired     | `expire_memories();` |
| Review weak rows  | `SELECT ... WHERE confidence < 0.5 AND ...` |

## Alignment with `workspace/MEMORY.md`

The forget protocol above matches the project's **MEMORY.md** guidance: audit everything, prefer expiry, and when correcting, store the new memory so context is never missing.
