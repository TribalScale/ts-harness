# Remember — Store a Memory

> **Embedding contract:** This skill uses `scripts/embed.sh` to generate embeddings. The active provider, model, and dimension are set at bootstrap time via environment variables (`EMBEDDINGS_PROVIDER`, `EMBEDDINGS_MODEL`, `EMBEDDINGS_DIMENSIONS`). The generated vector matches `memories.embedding vector($EMBEDDINGS_DIMENSIONS)` in the Postgres schema. Do not call an embedding API directly — always use `scripts/embed.sh`.

Store a new memory (or update an existing one) in the Postgres-backed TS Harness memory database.

## Database context

**Connection defaults** (override with environment variables or your deployment’s DSN):

| Setting  | Default        |
| -------- | -------------- |
| Host     | `ts-harness-db`  |
| Port     | `5432`         |
| User     | `ts_harness`     |
| Database | `ts_harness_memory` |

Run SQL via `psql` or any equivalent database client available in your environment, for example:

```bash
psql "host=ts-harness-db port=5432 user=ts_harness dbname=ts_harness_memory" -c "YOUR_SQL"
```

**`memories` table** (relevant columns): `id` (UUID), `type` (TEXT: `episodic`, `semantic`, `procedural`, `relational`, `directive`), `content` (TEXT), `embedding` (vector($EMBEDDINGS_DIMENSIONS)), `scope` (TEXT), `source` (TEXT), `confidence` (FLOAT), `created_at`, `updated_at`, `last_accessed`, `expires_at`, `tags` (TEXT[]), `metadata` (JSONB).

**`memory_audit` table**: `id`, `memory_id`, `action` (`created`, `accessed`, `updated`, `archived`, `deleted`, `expired`), `actor`, `reason`, `details` (JSONB), `occurred_at`.

Embeddings use an HNSW index for cosine similarity (`<=>` operator). Expired rows are handled by `expire_memories()`.

## When to use

- After learning a new fact
- After a decision is made
- After a user correction
- After completing a task (outcomes, lessons)
- When discovering a process or convention worth retaining

Do **not** store secrets, raw transcripts, or duplicates—update instead when similar content already exists.

## Steps

1. **Determine memory type** (`episodic`, `semantic`, `procedural`, `relational`, or `directive`) using the type guide in **`workspace/MEMORY.md`** (see the “When to Remember” table and examples there).

2. **Determine scope** using the narrowest accurate scope you are allowed to write:
   - `tenant:<tenant>:agent:<agent>` — your own notes. This is the default for `remember_memory`.
   - `project:<id-or-slug>` — project-specific
   - `user:<id-or-handle>` — user-specific
   - Do not write `global`, `department:<name>`, or `team:<slug>`. `remember` rejects `global` and `department:`. Department memory is created only when a person promotes an approved program brief.

3. **Assess confidence**
   - `1.0` — explicit user statements, confirmed facts
   - `0.8` — reasonably inferred from context
   - `0.6` — uncertain or weakly supported

4. **Check for duplicates** before inserting. Extract a few distinctive keywords from the planned `content`, then run:

   ```sql
   SELECT id, content, confidence
   FROM memories
   WHERE type = $TYPE
     AND scope = $SCOPE
     AND content ILIKE '%keyword%'
   LIMIT 5;
   ```

   If a row is substantively the same, **update** it (step 6) instead of inserting a new row.

   **Before steps 5 and 6:** complete **step 8** (generate embedding) so you have `$EMBEDDING` for the `INSERT`/`UPDATE` statements. Step 8 is listed last in this checklist but runs **immediately after** duplicate resolution.

5. **If new:** `INSERT` into `memories` with all required fields (including `embedding` from step 8). Set `tags` to a useful `TEXT[]`. Set `expires_at` when the information is time-sensitive (otherwise `NULL`). Set `source` (e.g. agent name, ticket id, or `user`).

6. **If updating:** `UPDATE` the existing row: refresh `content`, `embedding`, `tags`, `metadata`, and `updated_at`; **raise `confidence`** if the new information confirms or strengthens the memory.

7. **Log to `memory_audit`** with `action = 'created'` or `'updated'`, `actor` = your agent name (or agreed identifier), and `reason` = short explanation of why this was stored or changed.

8. **Generate the embedding** (executed **before** steps 5–6 in practice):

   ```bash
   EMBEDDING=$(scripts/embed.sh "$CONTENT")
   # Returns: '[0.01,-0.02,...]'::vector($EMBEDDINGS_DIMENSIONS)
   # The provider, model, and dimension come from bootstrap env vars.
   # Do NOT call any embedding API directly.
   ```

   Pass `$EMBEDDING` directly into the `INSERT` or `UPDATE` SQL — it is already formatted as a pgvector literal (e.g. `'[...]'::vector(1536)`).

## Example SQL

**INSERT (new memory)**

```sql
INSERT INTO memories (
  id,
  type,
  content,
  embedding,
  scope,
  source,
  confidence,
  created_at,
  updated_at,
  last_accessed,
  expires_at,
  tags,
  metadata
) VALUES (
  gen_random_uuid(),
  'semantic',
  'The auth service uses JWT with RS256.',
  $EMBEDDING::vector($EMBEDDINGS_DIMENSIONS),  -- replace $EMBEDDING with the vector from scripts/embed.sh
  'project:payments-api',
  'ts_harness-agent',
  1.0,
  now(),
  now(),
  NULL,
  NULL,
  ARRAY['auth', 'jwt'],
  '{"ticket": "PROJ-123"}'::jsonb
)
RETURNING id;
```

**UPDATE (merge / refresh)**

```sql
UPDATE memories
SET
  content = $NEW_CONTENT,
  embedding = $EMBEDDING::vector($EMBEDDINGS_DIMENSIONS),
  confidence = GREATEST(confidence, $NEW_CONFIDENCE),
  tags = $NEW_TAGS,
  metadata = COALESCE(metadata, '{}'::jsonb) || $METADATA_PATCH::jsonb,
  updated_at = now()
WHERE id = $EXISTING_ID;
```

**Audit log (after INSERT or UPDATE)**

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
  $MEMORY_ID,
  'created',  -- or 'updated'
  'ts_harness-agent',
  'User stated deployment rule; storing as directive for future tasks.',
  '{"summary": "JWT RS256 on payments-api"}'::jsonb,
  now()
);
```

Use `'updated'` for `action` when you merged into an existing row; use `'created'` for a new `memories.id`.
