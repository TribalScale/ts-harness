-- 036_project_repositories.sql
-- Repositories a project delivers to. Work orders name one of them; release
-- tracks run per repository. installation_id is the GitHub App installation
-- that covered the repository when it was added (NULL with a PAT).

BEGIN;

CREATE TABLE IF NOT EXISTS project_repositories (
    id              UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id      UUID        NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    github_host     TEXT        NOT NULL DEFAULT 'github.com',
    owner           TEXT        NOT NULL,
    repo            TEXT        NOT NULL,
    installation_id BIGINT,
    default_branch  TEXT,
    role            TEXT,
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_project_repositories_unique
    ON project_repositories (project_id, github_host, lower(owner), lower(repo));

COMMIT;
