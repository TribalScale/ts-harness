import { useCallback, useEffect, useMemo, useState } from "react";
import { apiFetch } from "../api/client";
import type { GitHubOverview, ProjectRepository } from "../api/types";
import { Button } from "../components/Button";
import { PageState } from "../components/PageState";

interface Draft {
  fullName: string;
  role: string;
  defaultBranch: string;
}

const EMPTY: Draft = { fullName: "", role: "", defaultBranch: "" };

export function RepositoriesPanel({
  projectId,
  canManage,
}: {
  projectId: string;
  canManage: boolean;
}) {
  const base = `/admin/projects/${encodeURIComponent(projectId)}/repositories`;
  const [repositories, setRepositories] = useState<ProjectRepository[] | null>(null);
  const [installed, setInstalled] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const out = await apiFetch<{ repositories: ProjectRepository[] }>(base);
      setRepositories(out.repositories || []);
      setError(null);
    } catch (e: any) {
      setError(String(e.message || e));
    }
  }, [base]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!draft || installed.length) return;
    apiFetch<GitHubOverview>("/admin/github")
      .then((out) =>
        setInstalled(
          out.installations
            .filter((i) => !i.suspended)
            .flatMap((i) => i.repositories)
            .sort(),
        ),
      )
      .catch(() => setInstalled([]));
  }, [draft, installed.length]);

  const choices = useMemo(() => {
    const taken = new Set((repositories || []).map((r) => r.full_name.toLowerCase()));
    return installed.filter((name) => !taken.has(name.toLowerCase()));
  }, [installed, repositories]);

  async function add() {
    if (!draft) return;
    setBusy(true);
    try {
      await apiFetch(base, {
        method: "POST",
        body: JSON.stringify({
          full_name: draft.fullName.trim(),
          role: draft.role.trim() || null,
          default_branch: draft.defaultBranch.trim() || null,
        }),
      });
      setDraft(null);
      await load();
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  async function remove(repository: ProjectRepository) {
    setBusy(true);
    try {
      await apiFetch(`${base}/${repository.id}`, { method: "DELETE" });
      await load();
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  if (!repositories && !error)
    return <PageState kind="loading" title="Loading repositories" />;

  return (
    <section aria-label="Repositories" className="registries-panel">
      <div className="system-header">
        <div>
          <h3>Repositories</h3>
          <p className="muted">
            Where this project's work orders are delivered. With more than one,
            each work order names its repository and releases run per
            repository in dependency order.
          </p>
        </div>
        {canManage && !draft && (
          <Button variant="secondary" onClick={() => setDraft({ ...EMPTY })}>
            Add repository
          </Button>
        )}
      </div>
      {error && (
        <p className="field-error" role="alert">
          {error}
        </p>
      )}
      {repositories && repositories.length === 0 && !draft && (
        <PageState
          kind="empty"
          title="No repositories"
          detail="Coding runs use the install's default repository."
        />
      )}
      {repositories && repositories.length > 0 && (
        <div className="cron-table-wrap">
          <table className="cron-table">
            <thead>
              <tr>
                <th>Repository</th>
                <th>Role</th>
                <th>Base branch</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {repositories.map((repository) => (
                <tr key={repository.id}>
                  <td>
                    {repository.full_name}
                    {repository.github_host !== "github.com" && (
                      <span className="muted"> ({repository.github_host})</span>
                    )}
                  </td>
                  <td>{repository.role || "—"}</td>
                  <td>{repository.default_branch || "Project default"}</td>
                  <td className="actions">
                    {canManage && (
                      <Button
                        variant="secondary"
                        onClick={() => void remove(repository)}
                        disabled={busy}
                      >
                        Remove
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {draft && (
        <div className="registry-form">
          <label>
            Repository (owner/name)
            <input
              list={`repo-choices-${projectId}`}
              value={draft.fullName}
              placeholder="acme/payments-api"
              onChange={(e) => setDraft({ ...draft, fullName: e.target.value })}
            />
            <datalist id={`repo-choices-${projectId}`}>
              {choices.map((name) => (
                <option key={name} value={name} />
              ))}
            </datalist>
          </label>
          <label>
            Role (optional, e.g. web or api)
            <input
              value={draft.role}
              onChange={(e) => setDraft({ ...draft, role: e.target.value })}
            />
          </label>
          <label>
            Base branch (optional)
            <input
              value={draft.defaultBranch}
              placeholder="main"
              onChange={(e) => setDraft({ ...draft, defaultBranch: e.target.value })}
            />
          </label>
          <div className="actions">
            <Button
              variant="primary"
              onClick={() => void add()}
              disabled={busy || !/^[\w.-]+\/[\w.-]+$/.test(draft.fullName.trim())}
            >
              Add
            </Button>
            <Button variant="secondary" onClick={() => setDraft(null)}>
              Cancel
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}
