import { useCallback, useEffect, useMemo, useState } from "react";
import { apiFetch } from "../api/client";
import type { GitHubOverview, ProjectRepository } from "../api/types";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/ConfirmDialog";

interface Draft {
  fullName: string;
  role: string;
  defaultBranch: string;
}

const EMPTY: Draft = { fullName: "", role: "", defaultBranch: "" };
const FULL_NAME = /^[\w.-]+\/[\w.-]+$/;

function BranchCell({
  branch,
  projectDefault,
}: {
  branch: string | null;
  projectDefault: string;
}) {
  if (branch) return <code className="proj-branch">{branch}</code>;
  return (
    <span className="muted">
      Project default · <code className="proj-branch">{projectDefault}</code>
    </span>
  );
}

export function RepositoriesPanel({
  projectId,
  canManage,
  projectDefaultBranch,
}: {
  projectId: string;
  canManage: boolean;
  /** The branch a repository without its own uses (the project's default base branch, else dev). */
  projectDefaultBranch?: string | null;
}) {
  const base = `/admin/projects/${encodeURIComponent(projectId)}/repositories`;
  const fallback = projectDefaultBranch || "dev";
  const [repositories, setRepositories] = useState<ProjectRepository[] | null>(
    null,
  );
  const [installed, setInstalled] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState<Draft | null>(null);
  const [editing, setEditing] = useState<{
    id: string;
    role: string;
    defaultBranch: string;
  } | null>(null);
  const [removing, setRemoving] = useState<ProjectRepository | null>(null);
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
    if (!adding || installed.length) return;
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
  }, [adding, installed.length]);

  const choices = useMemo(() => {
    const taken = new Set(
      (repositories || []).map((r) => r.full_name.toLowerCase()),
    );
    return installed.filter((name) => !taken.has(name.toLowerCase()));
  }, [installed, repositories]);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    try {
      await action();
      setError(null);
      await load();
      return true;
    } catch (e: any) {
      setError(String(e.message || e));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function add() {
    if (!adding) return;
    const done = await run(() =>
      apiFetch(base, {
        method: "POST",
        body: JSON.stringify({
          full_name: adding.fullName.trim(),
          role: adding.role.trim() || null,
          default_branch: adding.defaultBranch.trim() || null,
        }),
      }),
    );
    if (done) setAdding(null);
  }

  async function saveEdit() {
    if (!editing) return;
    const done = await run(() =>
      apiFetch(`${base}/${encodeURIComponent(editing.id)}`, {
        method: "PUT",
        body: JSON.stringify({
          role: editing.role.trim() || null,
          default_branch: editing.defaultBranch.trim() || null,
        }),
      }),
    );
    if (done) setEditing(null);
  }

  async function confirmRemove() {
    if (!removing) return;
    const target = removing;
    const done = await run(() =>
      apiFetch(`${base}/${encodeURIComponent(target.id)}`, {
        method: "DELETE",
      }),
    );
    if (done) setRemoving(null);
  }

  return (
    <section aria-label="Repositories" className="proj-card">
      <header className="proj-card-head">
        <div>
          <h3>Repositories</h3>
          <p className="muted">
            Where this project's work orders and Jira tickets are delivered. A
            ticket without a <code>Repo:</code> line uses the repository here;
            with several, it names the one it changes.
          </p>
        </div>
        {canManage && !adding && (
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setAdding({ ...EMPTY })}
          >
            Add repository
          </Button>
        )}
      </header>
      {error && (
        <p className="field-error" role="alert">
          {error}
        </p>
      )}
      {!repositories && !error && (
        <p className="muted">Loading repositories…</p>
      )}
      {repositories && repositories.length === 0 && !adding && (
        <p className="proj-empty">
          No repositories yet. Coding runs use the install's default repository
          until you add one.
        </p>
      )}
      {repositories && repositories.length > 0 && (
        <div className="proj-table-wrap">
          <table className="proj-table">
            <thead>
              <tr>
                <th>Repository</th>
                <th>Role</th>
                <th>Base branch</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {repositories.map((repository) =>
                editing?.id === repository.id ? (
                  <tr key={repository.id} className="proj-row-editing">
                    <td>
                      <strong>{repository.full_name}</strong>
                    </td>
                    <td>
                      <input
                        aria-label={`Role for ${repository.full_name}`}
                        value={editing.role}
                        placeholder="web, api…"
                        onChange={(e) =>
                          setEditing({ ...editing, role: e.target.value })
                        }
                      />
                    </td>
                    <td>
                      <input
                        aria-label={`Base branch for ${repository.full_name}`}
                        value={editing.defaultBranch}
                        placeholder={`Project default (${fallback})`}
                        onChange={(e) =>
                          setEditing({
                            ...editing,
                            defaultBranch: e.target.value,
                          })
                        }
                      />
                    </td>
                    <td className="proj-actions">
                      <Button
                        variant="primary"
                        size="sm"
                        busy={busy}
                        onClick={() => void saveEdit()}
                      >
                        Save
                      </Button>
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() => setEditing(null)}
                      >
                        Cancel
                      </Button>
                    </td>
                  </tr>
                ) : (
                  <tr key={repository.id}>
                    <td>
                      {repository.repo_url ? (
                        <a
                          href={repository.repo_url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {repository.full_name}
                        </a>
                      ) : (
                        repository.full_name
                      )}
                      {repository.github_host !== "github.com" && (
                        <span className="muted">
                          {" "}
                          · {repository.github_host}
                        </span>
                      )}
                    </td>
                    <td>
                      {repository.role ? (
                        <span className="proj-tag">{repository.role}</span>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                    <td>
                      <BranchCell
                        branch={repository.default_branch}
                        projectDefault={fallback}
                      />
                    </td>
                    <td className="proj-actions">
                      {canManage && (
                        <>
                          <Button
                            variant="secondary"
                            size="sm"
                            disabled={busy}
                            aria-label={`Edit ${repository.full_name}`}
                            onClick={() =>
                              setEditing({
                                id: repository.id,
                                role: repository.role || "",
                                defaultBranch: repository.default_branch || "",
                              })
                            }
                          >
                            Edit
                          </Button>
                          <Button
                            variant="secondary"
                            size="sm"
                            disabled={busy}
                            aria-label={`Remove ${repository.full_name}`}
                            onClick={() => setRemoving(repository)}
                          >
                            Remove
                          </Button>
                        </>
                      )}
                    </td>
                  </tr>
                ),
              )}
            </tbody>
          </table>
        </div>
      )}
      {adding && (
        <form
          className="proj-subform"
          aria-label="Add repository"
          onSubmit={(event) => {
            event.preventDefault();
            if (!busy && FULL_NAME.test(adding.fullName.trim())) void add();
          }}
        >
          <div className="proj-grid">
            <label className="form-field proj-span-2">
              <span>Repository (owner/name)</span>
              <input
                list={`repo-choices-${projectId}`}
                value={adding.fullName}
                placeholder="acme/payments-api"
                autoFocus
                onChange={(e) =>
                  setAdding({ ...adding, fullName: e.target.value })
                }
              />
              <datalist id={`repo-choices-${projectId}`}>
                {choices.map((name) => (
                  <option key={name} value={name} />
                ))}
              </datalist>
            </label>
            <label className="form-field">
              <span>Role (optional, e.g. web or api)</span>
              <input
                value={adding.role}
                onChange={(e) => setAdding({ ...adding, role: e.target.value })}
              />
            </label>
            <label className="form-field">
              <span>Base branch (optional)</span>
              <input
                value={adding.defaultBranch}
                placeholder={`Project default (${fallback})`}
                onChange={(e) =>
                  setAdding({ ...adding, defaultBranch: e.target.value })
                }
              />
            </label>
          </div>
          <div className="proj-form-actions">
            <Button
              variant="secondary"
              size="sm"
              type="button"
              onClick={() => setAdding(null)}
            >
              Cancel
            </Button>
            <Button
              variant="primary"
              size="sm"
              type="submit"
              busy={busy}
              disabled={busy || !FULL_NAME.test(adding.fullName.trim())}
            >
              Add
            </Button>
          </div>
        </form>
      )}
      <ConfirmDialog
        open={Boolean(removing)}
        title="Remove repository?"
        description={
          removing
            ? `${removing.full_name} will no longer receive this project's work orders or Jira tickets. The repository itself is not changed.`
            : ""
        }
        confirmLabel="Remove"
        danger
        busy={busy}
        onConfirm={() => void confirmRemove()}
        onCancel={() => setRemoving(null)}
      />
    </section>
  );
}
