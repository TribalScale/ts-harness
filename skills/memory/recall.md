# Recall — Search Memory

Search and retrieve relevant memories from the Postgres-backed TS Harness memory database and return structured context to the orchestrator or calling agent.

## Database context

**Connection defaults:**

| Setting  | Default        |
| -------- | -------------- |
| Host     | `ts-harness-db`  |
| Port     | `5432`         |
| User     | `ts_harness`     |
| Database | `ts_harness_memory` |

Execute queries with `psql` or an equivalent database tool.

**Relevant schema:** `memories` includes `embedding vector(1536)` with an **HNSW** index for **cosine** distance (`<=>`). Filter out expired rows with `expires_at`. The `type` column includes `directive` memories (treat as high-priority rules).

## When to use

- Before answering any question
- Before starting a task
- When the user references the past
- When working in a specific scope (team, project, user)

## Steps

1. **Determine search context:** topic or question text, optional `scope` (`global`, `team:X`, `project:X`, `user:X`), and optional time range (filter on `created_at` / `updated_at` in follow-up queries if needed).

2. **Semantic search** when you have a query embedding (same model and dimension as stored vectors — 1536):

   ```sql
   SELECT id, type, content, scope, confidence, created_at, last_accessed,
          1 - (embedding <=> $QUERY_EMBEDDING) AS similarity
   FROM memories
   WHERE ($SCOPE IS NULL OR scope = $SCOPE OR scope = 'global')
     AND (expires_at IS NULL OR expires_at > now())
   ORDER BY embedding <=> $QUERY_EMBEDDING
   LIMIT 10;
   ```

   Bind `$QUERY_EMBEDDING` as `vector(1536)`. Use `$SCOPE IS NULL` to search across the given scope plus `global`.

3. **Keyword fallback** when embeddings are unavailable or to complement semantic hits:

   ```sql
   SELECT id, type, content, scope, confidence, created_at
   FROM memories
   WHERE content ILIKE '%keyword%'
     AND ($SCOPE IS NULL OR scope = $SCOPE OR scope = 'global')
     AND (expires_at IS NULL OR expires_at > now())
   ORDER BY confidence DESC, created_at DESC
   LIMIT 10;
   ```

4. **Filter and prioritize results**
   - Discard rows with **`confidence < 0.5`** unless you have no other matches and must disclose low confidence to the user.
   - **Prioritize `directive`** memories: they represent rules and preferences—surface them prominently when relevant.

5. **Update `last_accessed`** for every memory id you actually use in reasoning:

   ```sql
   UPDATE memories
   SET last_accessed = now()
   WHERE id IN ($ID1, $ID2, ...);
   ```

6. **Log access** in `memory_audit` for each retrieved memory you rely on (one row per memory or one batched `details` JSON—prefer one row per `memory_id` for clearer audit trails):

   ```sql
   INSERT INTO memory_audit (id, memory_id, action, actor, reason, details, occurred_at)
   VALUES (
     gen_random_uuid(),
     $MEMORY_ID,
     'accessed',
     $ACTOR,
     'Recall before answering / task start.',
     '{"query_summary": "optional short description"}'::jsonb,
     now()
   );
   ```

7. **Present results** to the calling agent or orchestrator as structured context: list each memory with `id`, `type`, `content`, `scope`, `confidence`, `similarity` (if applicable), and `created_at`. Call out directives explicitly.

## Weighting and ranking

Combine vector similarity, confidence, and recency when ordering or merging duplicate lists from semantic + keyword searches.

**Composite score (for ranking merged candidates):**

`score = (similarity * 0.5) + (confidence * 0.3) + (recency_factor * 0.2)`

Equivalently: \(\text{score} = (\text{similarity} \times 0.5) + (\text{confidence} \times 0.3) + (\text{recency\_factor} \times 0.2)\).

- **`similarity`:** use `1 - (embedding <=> query_embedding)` from the semantic query; for keyword-only rows with no similarity, use `0.0` or a small default (e.g. `0.3`) if you still want them in the mix—document the choice in your trace.
- **`confidence`:** use the row’s `confidence` (0–1).
- **`recency_factor`:** `1.0` for memories from **today** (calendar day in the deployment timezone or UTC—be consistent), then subtract **`0.01` per day** elapsed since `created_at` (or `updated_at` if you prefer “last verified”—pick one convention), with a **floor of `0.1`**.

**Directive boost:** After computing `score`, you may bump `directive` rows by a fixed offset (e.g. `+0.1`) so rules compete fairly with high-similarity episodic noise—optional but recommended.

## Quick reference

| Signal        | Role in ranking                          |
| ------------- | ---------------------------------------- |
| Similarity    | 50% weight — topical match               |
| Confidence    | 30% weight — trust in the fact           |
| Recency       | 20% weight — fresher context preferred   |
| Type          | Directives are rules—prioritize in UI text |

Always respect expiry: never present rows where `expires_at <= now()` except for audit or maintenance flows.
