import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import type { ProjectRegistry, RegistryType } from "../api/types";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PageState } from "../components/PageState";
import { StatusPill } from "../components/StatusPill";

const TYPES: {
  id: RegistryType;
  label: string;
  scopeLabel: string;
  placeholder: string;
}[] = [
  {
    id: "npm",
    label: "npm",
    scopeLabel: "Scope (optional, e.g. @acme)",
    placeholder: "https://npm.acme.dev/repo",
  },
  {
    id: "pypi",
    label: "PyPI",
    scopeLabel: "Scope (unused)",
    placeholder: "https://pypi.acme.dev/simple",
  },
  {
    id: "maven",
    label: "Maven",
    scopeLabel: "Repository id (optional)",
    placeholder: "https://maven.acme.dev/releases",
  },
  {
    id: "docker",
    label: "Docker",
    scopeLabel: "Scope (unused)",
    placeholder: "registry.acme.dev",
  },
];

interface Draft {
  id?: string;
  type: RegistryType;
  url: string;
  scope: string;
  username: string;
  token: string;
  hasCredential: boolean;
}

const EMPTY: Draft = {
  type: "npm",
  url: "",
  scope: "",
  username: "",
  token: "",
  hasCredential: false,
};

function testStatus(registry: ProjectRegistry) {
  if (registry.last_test_ok === true)
    return <StatusPill status="success" label="Reachable" />;
  if (registry.last_test_ok === false)
    return (
      <StatusPill
        status="danger"
        label={registry.last_test_error || "Failed"}
      />
    );
  return <StatusPill status="muted" label="Not tested" />;
}

export function RegistriesPanel({ projectId }: { projectId: string }) {
  const base = `/admin/projects/${encodeURIComponent(projectId)}/registries`;
  const [registries, setRegistries] = useState<ProjectRegistry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<ProjectRegistry | null>(null);

  const load = useCallback(async () => {
    try {
      const out = await apiFetch<{ registries: ProjectRegistry[] }>(base);
      setRegistries(out.registries);
      setError(null);
    } catch (e: any) {
      setError(String(e.message || e));
    }
  }, [base]);

  useEffect(() => {
    void load();
  }, [load]);

  async function save() {
    if (!draft) return;
    setBusy("save");
    const body: Record<string, unknown> = {
      url: draft.url,
      scope: draft.scope,
      username: draft.username,
    };
    if (draft.token) body.token = draft.token;
    try {
      if (draft.id) {
        await apiFetch(`${base}/${draft.id}`, {
          method: "PUT",
          body: JSON.stringify(body),
        });
      } else {
        await apiFetch(base, {
          method: "POST",
          body: JSON.stringify({ ...body, type: draft.type }),
        });
      }
      setDraft(null);
      await load();
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(null);
    }
  }

  async function test(registry: ProjectRegistry) {
    setBusy(`test-${registry.id}`);
    try {
      await apiFetch(`${base}/${registry.id}/test`, {
        method: "POST",
        body: "{}",
      });
      await load();
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(null);
    }
  }

  async function remove() {
    if (!deleting) return;
    setBusy("delete");
    try {
      await apiFetch(`${base}/${deleting.id}`, { method: "DELETE" });
      setDeleting(null);
      await load();
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(null);
    }
  }

  if (!registries && !error)
    return <PageState kind="loading" title="Loading registries" />;
  const typeInfo = TYPES.find((t) => t.id === draft?.type) || TYPES[0];

  return (
    <section aria-label="Package registries" className="registries-panel">
      <div className="system-header">
        <div>
          <h3>Package registries</h3>
          <p className="muted">
            Private registries the coding workers use for this project.
            Credentials are stored in the secret manager and never shown again.
          </p>
        </div>
        {!draft && (
          <Button variant="secondary" onClick={() => setDraft({ ...EMPTY })}>
            Add registry
          </Button>
        )}
      </div>
      {error && (
        <p className="field-error" role="alert">
          {error}
        </p>
      )}
      {registries && registries.length === 0 && !draft && (
        <PageState
          kind="empty"
          title="No registries"
          detail="Workers use the public registries only."
        />
      )}
      {registries && registries.length > 0 && (
        <div className="cron-table-wrap">
          <table className="cron-table">
            <thead>
              <tr>
                <th>Type</th>
                <th>URL</th>
                <th>Scope</th>
                <th>Credential</th>
                <th>Status</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {registries.map((registry) => (
                <tr key={registry.id}>
                  <td>{registry.type}</td>
                  <td>{registry.url}</td>
                  <td>{registry.scope || "—"}</td>
                  <td>
                    {registry.has_credential
                      ? `Set${registry.username ? ` (${registry.username})` : ""}`
                      : "None"}
                  </td>
                  <td>{testStatus(registry)}</td>
                  <td className="row-actions">
                    <Button
                      size="sm"
                      variant="ghost"
                      busy={busy === `test-${registry.id}`}
                      onClick={() => void test(registry)}
                    >
                      Test
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() =>
                        setDraft({
                          id: registry.id,
                          type: registry.type,
                          url: registry.url,
                          scope: registry.scope || "",
                          username: registry.username || "",
                          token: "",
                          hasCredential: registry.has_credential,
                        })
                      }
                    >
                      Edit
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => setDeleting(registry)}
                    >
                      Delete
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {draft && (
        <form
          className="registry-form"
          aria-label={draft.id ? "Edit registry" : "Add registry"}
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <label className="form-field">
            <span>Type</span>
            <select
              aria-label="Registry type"
              value={draft.type}
              disabled={Boolean(draft.id)}
              onChange={(e) =>
                setDraft({ ...draft, type: e.target.value as RegistryType })
              }
            >
              {TYPES.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.label}
                </option>
              ))}
            </select>
          </label>
          <label className="form-field">
            <span>URL</span>
            <input
              aria-label="Registry URL"
              value={draft.url}
              placeholder={typeInfo.placeholder}
              onChange={(e) => setDraft({ ...draft, url: e.target.value })}
            />
          </label>
          <label className="form-field">
            <span>{typeInfo.scopeLabel}</span>
            <input
              aria-label="Registry scope"
              value={draft.scope}
              onChange={(e) => setDraft({ ...draft, scope: e.target.value })}
            />
          </label>
          <label className="form-field">
            <span>Username (optional)</span>
            <input
              aria-label="Registry username"
              autoComplete="off"
              value={draft.username}
              onChange={(e) => setDraft({ ...draft, username: e.target.value })}
            />
          </label>
          <label className="form-field">
            <span>
              {draft.hasCredential
                ? "New token (leave blank to keep the current one)"
                : "Token"}
            </span>
            <input
              aria-label="Registry token"
              type="password"
              autoComplete="new-password"
              value={draft.token}
              onChange={(e) => setDraft({ ...draft, token: e.target.value })}
            />
          </label>
          <div className="dialog-actions">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setDraft(null)}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              variant="primary"
              busy={busy === "save"}
              disabled={!draft.url.trim()}
            >
              Save registry
            </Button>
          </div>
        </form>
      )}
      <ConfirmDialog
        open={Boolean(deleting)}
        title="Delete registry"
        description={`Workers will stop using ${deleting?.url || "this registry"} and its stored credential is scheduled for deletion.`}
        confirmLabel="Delete"
        danger
        busy={busy === "delete"}
        onConfirm={() => void remove()}
        onCancel={() => setDeleting(null)}
      />
    </section>
  );
}
