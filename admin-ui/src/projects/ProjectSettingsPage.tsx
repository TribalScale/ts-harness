import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import type { Project } from "../api/types";
import { Button } from "../components/Button";
import { PageState } from "../components/PageState";
import { RegistriesPanel } from "./RegistriesPanel";

// Fargate CPU units and the memory range (MiB) each allows.
const SIZES: Record<number, [number, number]> = {
  1024: [2048, 8192],
  2048: [4096, 16384],
  4096: [8192, 30720],
  8192: [16384, 61440],
  16384: [32768, 122880],
};

function sizeError(cpu: string, memory: string): string | null {
  if (!cpu && !memory) return null;
  if (!cpu || !memory)
    return "Set both CPU and memory, or neither to use the default.";
  const range = SIZES[Number(cpu)];
  const mb = Number(memory);
  if (!range || !Number.isInteger(mb))
    return "Choose a CPU size and a whole number of MiB.";
  if (mb < range[0] || mb > range[1])
    return `Memory for ${Number(cpu) / 1024} vCPU must be ${range[0]}–${range[1]} MiB.`;
  return null;
}

function ProjectForm({
  project,
  isAdmin,
  onSaved,
}: {
  project: Project;
  isAdmin: boolean;
  onSaved: (p: Project) => void;
}) {
  const [name, setName] = useState(project.name);
  const [team, setTeam] = useState(project.team_slug || "");
  const [jira, setJira] = useState(project.jira_project_key || "");
  const [branch, setBranch] = useState(project.default_base_branch || "");
  const [cpu, setCpu] = useState(
    project.worker_cpu ? String(project.worker_cpu) : "",
  );
  const [memory, setMemory] = useState(
    project.worker_memory_mb ? String(project.worker_memory_mb) : "",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const invalid = sizeError(cpu, memory);
  const readOnly = !project.can_manage;

  async function save() {
    setBusy(true);
    const body: Record<string, unknown> = {
      name,
      jira_project_key: jira,
      default_base_branch: branch,
      worker_cpu: cpu ? Number(cpu) : null,
      worker_memory_mb: memory ? Number(memory) : null,
    };
    if (isAdmin) body.team_slug = team;
    try {
      const saved = await apiFetch<Project>(
        `/admin/projects/${encodeURIComponent(project.id)}`,
        {
          method: "PUT",
          body: JSON.stringify(body),
        },
      );
      setError(null);
      onSaved(saved);
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      aria-label="Project settings"
      className="project-form"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <fieldset disabled={readOnly}>
        <label className="form-field">
          <span>Name</span>
          <input
            aria-label="Project name"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <label className="form-field">
          <span>Owning team</span>
          <input
            aria-label="Owning team"
            value={team}
            disabled={!isAdmin}
            onChange={(e) => setTeam(e.target.value)}
          />
        </label>
        <label className="form-field">
          <span>Jira project key</span>
          <input
            aria-label="Jira project key"
            value={jira}
            onChange={(e) => setJira(e.target.value)}
          />
        </label>
        <label className="form-field">
          <span>Default base branch</span>
          <input
            aria-label="Default base branch"
            value={branch}
            placeholder="main"
            onChange={(e) => setBranch(e.target.value)}
          />
        </label>
        <label className="form-field">
          <span>Worker CPU</span>
          <select
            aria-label="Worker CPU"
            value={cpu}
            onChange={(e) => setCpu(e.target.value)}
          >
            <option value="">Default</option>
            {Object.keys(SIZES).map((units) => (
              <option key={units} value={units}>
                {Number(units) / 1024} vCPU
              </option>
            ))}
          </select>
        </label>
        <label className="form-field">
          <span>Worker memory (MiB)</span>
          <input
            aria-label="Worker memory"
            inputMode="numeric"
            value={memory}
            placeholder={
              cpu
                ? `${SIZES[Number(cpu)][0]}–${SIZES[Number(cpu)][1]}`
                : "Default"
            }
            onChange={(e) => setMemory(e.target.value.trim())}
          />
        </label>
      </fieldset>
      {(invalid || error) && (
        <p className="field-error" role="alert">
          {invalid || error}
        </p>
      )}
      {!readOnly && (
        <div className="dialog-actions">
          <Button
            type="submit"
            variant="primary"
            busy={busy}
            disabled={Boolean(invalid)}
          >
            Save project
          </Button>
        </div>
      )}
    </form>
  );
}

function NewProjectForm({ onCreated }: { onCreated: (p: Project) => void }) {
  const [key, setKey] = useState("");
  const [name, setName] = useState("");
  const [team, setTeam] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function create() {
    setBusy(true);
    try {
      const created = await apiFetch<Project>("/admin/projects", {
        method: "POST",
        body: JSON.stringify({
          key,
          name: name || key,
          team_slug: team || null,
        }),
      });
      setKey("");
      setName("");
      setTeam("");
      setError(null);
      onCreated(created);
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      aria-label="New project"
      className="project-new"
      onSubmit={(event) => {
        event.preventDefault();
        void create();
      }}
    >
      <label className="form-field">
        <span>Key</span>
        <input
          aria-label="New project key"
          value={key}
          onChange={(e) => setKey(e.target.value)}
        />
      </label>
      <label className="form-field">
        <span>Name</span>
        <input
          aria-label="New project name"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <label className="form-field">
        <span>Team</span>
        <input
          aria-label="New project team"
          value={team}
          onChange={(e) => setTeam(e.target.value)}
        />
      </label>
      {error && <p className="field-error">{error}</p>}
      <Button
        type="submit"
        variant="secondary"
        busy={busy}
        disabled={!key.trim()}
      >
        Create project
      </Button>
    </form>
  );
}

export function ProjectSettingsPage({ selectedId }: { selectedId?: string }) {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [isAdmin, setIsAdmin] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const out = await apiFetch<{ projects: Project[]; is_admin: boolean }>(
        "/admin/projects",
      );
      setProjects(out.projects);
      setIsAdmin(out.is_admin);
      setError(null);
    } catch (e: any) {
      setError(String(e.message || e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (error && !projects)
    return (
      <PageState
        kind="error"
        title="Unable to load projects"
        detail={error}
        onRetry={() => void load()}
      />
    );
  if (!projects) return <PageState kind="loading" title="Loading projects" />;

  const selected =
    projects.find((p) => p.id === selectedId || p.key === selectedId) || null;
  const select = (p: Project) => {
    window.location.hash = `#/system/projects/${encodeURIComponent(p.id)}`;
  };

  return (
    <div className="projects-page">
      <aside className="projects-list" aria-label="Projects">
        {projects.length === 0 ? (
          <PageState
            kind="empty"
            title="No projects"
            detail="Projects appear here once created."
          />
        ) : (
          <ul>
            {projects.map((p) => (
              <li key={p.id}>
                <button
                  type="button"
                  className={p.id === selected?.id ? "active" : ""}
                  onClick={() => select(p)}
                >
                  <strong>{p.name}</strong>
                  <span className="muted">
                    {" "}
                    {p.key}
                    {p.team_slug ? ` · ${p.team_slug}` : ""}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
        {isAdmin && (
          <NewProjectForm
            onCreated={(p) => {
              void load();
              select(p);
            }}
          />
        )}
      </aside>
      <section className="projects-detail">
        {selected ? (
          <>
            <h3>{selected.name}</h3>
            <ProjectForm
              key={selected.id}
              project={selected}
              isAdmin={isAdmin}
              onSaved={(saved) =>
                setProjects((all) =>
                  (all || []).map((p) => (p.id === saved.id ? saved : p)),
                )
              }
            />
            {selected.can_manage ? (
              <RegistriesPanel projectId={selected.id} />
            ) : (
              <p className="muted">
                Only admins and managers of the owning team can see this
                project's registries.
              </p>
            )}
          </>
        ) : (
          <PageState
            kind="empty"
            title="Select a project"
            detail="Choose a project to change its workers and registries."
          />
        )}
      </section>
    </div>
  );
}
