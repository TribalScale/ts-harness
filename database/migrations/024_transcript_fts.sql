BEGIN;

ALTER TABLE public.runtime_transcript_messages
  ADD COLUMN IF NOT EXISTS content_tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('english', coalesce(content, ''))) STORED;

CREATE INDEX IF NOT EXISTS idx_transcript_content_tsv
  ON public.runtime_transcript_messages USING gin (content_tsv);

COMMIT;
