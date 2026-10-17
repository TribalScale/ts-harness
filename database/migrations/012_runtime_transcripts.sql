BEGIN;

CREATE TABLE IF NOT EXISTS public.runtime_transcript_messages (
    session_id   TEXT        NOT NULL REFERENCES public.runtime_sessions (session_id),
    seq          BIGINT      NOT NULL,
    role         TEXT        NOT NULL,
    content      TEXT,
    tool_calls   JSONB,
    tool_call_id TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_transcript_session
    ON public.runtime_transcript_messages (session_id, seq);

COMMIT;
