-- 054_knowledge_note_search.sql
-- Hybrid recall for knowledge notes: a weighted full-text vector (title, body, why) and an
-- embedding from the same provider and width as memories.embedding (vector(1536), see
-- 001_init_memory.sql). embedding_hash is the md5 of the text that was embedded, so a note
-- whose text changed since is embedded again. A note with no embedding is still found by
-- full-text search. source 'drafted' marks notes drafted from a failed-then-fixed run.

BEGIN;

ALTER TABLE knowledge_notes
    ADD COLUMN IF NOT EXISTS search_tsv tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A')
        || setweight(to_tsvector('english', coalesce(body, '')), 'B')
        || setweight(to_tsvector('english', coalesce(why, '')), 'C')
    ) STORED;

ALTER TABLE knowledge_notes ADD COLUMN IF NOT EXISTS embedding vector(1536);
ALTER TABLE knowledge_notes ADD COLUMN IF NOT EXISTS embedding_hash TEXT;
ALTER TABLE knowledge_notes ADD COLUMN IF NOT EXISTS embedded_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_knowledge_notes_search_tsv
    ON knowledge_notes USING gin (search_tsv);

CREATE INDEX IF NOT EXISTS idx_knowledge_notes_embedding
    ON knowledge_notes USING hnsw (embedding vector_cosine_ops);

ALTER TABLE knowledge_notes DROP CONSTRAINT IF EXISTS knowledge_notes_source_check;
ALTER TABLE knowledge_notes
    ADD CONSTRAINT knowledge_notes_source_check
    CHECK (source IN ('manual', 'review', 'agent', 'drafted'));

COMMIT;
