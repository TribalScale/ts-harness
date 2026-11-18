import { useCallback, useEffect, useId, useRef, useState } from "react";
import { apiFetch } from "../api/client";
import type { Project } from "../api/types";
import { Button } from "../components/Button";
import { PageState } from "../components/PageState";
import { RegistriesPanel } from "./RegistriesPanel";
import { RepositoriesPanel } from "./RepositoriesPanel";
import { ProjectNotesPanel } from "./ProjectNotesPanel";

// Fargate CPU units and the memory range (MiB) each allows.
const SIZES: Record<number, [number, number]> = {
  1024: [2048, 8192],
  2048: [4096, 16384],
  4096: [8192, 30720],
  8192: [16384, 61440],
  16384: [32768, 122880],
};
const PROJECT_KEY = /^[A-Za-z][A-Za-z0-9_-]{1,31}$/;

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

type Fields = {
  name: string;
  team: string;
  jira: string;
  branch: string;
  bdd: string;
  cpu: string;
  memory: string;
};

function fieldsOf(project: Project): Fields {
  return {
    name: project.name,
    team: project.team_slug || "",
    jira: project.jira_project_key || "",
    branch: project.default_base_branch || "",
    bdd: project.bdd_mode || "auto",
    cpu: project.worker_cpu ? String(project.worker_cpu) : "",
    memory: project.worker_memory_mb ? String(project.worker_memory_mb) : "",
  };
}

/** One card of project settings with its own Save; every card saves the whole project. */
function SettingsCard({
  title,
  description,
  label,
  project,
  isAdmin,
  dirtyKeys,
  validate,
  onSaved,
  children,
}: {
  title: string;
  description: string;
  label: string;
  project: Project;
  isAdmin: boolean;
  dirtyKeys: (keyof Fields)[];
  validate?: (fields: Fields) => string | null;
  onSaved: (project: Project) => void;
  children: (
    fields: Fields,
    set: (patch: Partial<Fields>) => void,
    readOnly: boolean,
  ) => React.ReactNode;
}) {
  const [fields, setFields] = useState<Fields>(() => fieldsOf(project));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const readOnly = !project.can_manage;
  const original = fieldsOf(project);
  const dirty = dirtyKeys.some((key) => fields[key] !== original[key]);
  const invalid = validate?.(fields) || null;

  async function save() {
    setBusy(true);
    // This card's fields; the rest as last saved (another card may have changed them).
    const merged: Fields = { ...fieldsOf(project) };
    for (const key of dirtyKeys) merged[key] = fields[key];
    const body: Record<string, unknown> = {
      name: merged.name.trim() || project.name,
      jira_project_key: merged.jira.trim().toUpperCase() || null,
      default_base_branch: merged.branch.trim() || null,
      bdd_mode: merged.bdd || "auto",
      worker_cpu: merged.cpu ? Number(merged.cpu) : null,
      worker_memory_mb: merged.memory ? Number(merged.memory) : null,
    };
    if (isAdmin) body.team_slug = merged.team.trim() || null;
    try {
      const out = await apiFetch<Project>(
        `/admin/projects/${encodeURIComponent(project.id)}`,
        {
          method: "PUT",
          body: JSON.stringify(body),
        },
      );
      setError(null);
      setSaved(true);
      setFields(fieldsOf(out));
      onSaved(out);
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      aria-label={label}
      className="proj-card"
      onSubmit={(event) => {
        event.preventDefault();
        if (invalid || busy || !dirty) return;
        void save();
      }}
    >
      <header className="proj-card-head">
        <div>
          <h3>{title}</h3>
          <p className="muted">{description}</p>
        </div>
      </header>
      <fieldset className="proj-grid" disabled={readOnly}>
        {children(
          fields,
          (patch) => {
            setSaved(false);
            setFields((prev) => ({ ...prev, ...patch }));
          },
          readOnly,
        )}
      </fieldset>
      {(invalid || error) && (
        <p className="field-error" role="alert">
          {invalid || error}
        </p>
      )}
      {!readOnly && (
        <div className="proj-form-actions">
          {saved && !dirty && <span className="proj-saved">Saved</span>}
          {dirty && (
            <Button
              variant="secondary"
              size="sm"
              type="button"
              onClick={() => setFields(fieldsOf(project))}
            >
              Discard
            </Button>
          )}
          <Button
            type="submit"
            variant="primary"
            size="sm"
            busy={busy}
            disabled={!dirty || Boolean(invalid)}
          >
            Save changes
          </Button>
        </div>
      )}
    </form>
  );
}

function Hint({ children }: { children: React.ReactNode }) {
  return <small className="proj-hint">{children}</small>;
}

function NewProjectDialog({
  open,
  isAdmin,
  onClose,
  onCreated,
}: {
  open: boolean;
  isAdmin: boolean;
  onClose: () => void;
  onCreated: (p: Project) => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const [key, setKey] = useState("");
  const [name, setName] = useState("");
  const [team, setTeam] = useState("");
  const [jira, setJira] = useState("");
  const [branch, setBranch] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const keyOk = PROJECT_KEY.test(key.trim());

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      setKey("");
      setName("");
      setTeam("");
      setJira("");
      setBranch("");
      setError(null);
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    } else if (!open && dialog.open) {
      if (typeof dialog.close === "function") dialog.close();
      else dialog.removeAttribute("open");
    }
  }, [open]);

  async function create() {
    setBusy(true);
    try {
      const created = await apiFetch<Project>("/admin/projects", {
        method: "POST",
        body: JSON.stringify({
          key: key.trim().toUpperCase(),
          name: name.trim() || key.trim().toUpperCase(),
          team_slug: team.trim() || null,
          jira_project_key: (jira.trim() || "").toUpperCase() || null,
          default_base_branch: branch.trim() || null,
        }),
      });
      onCreated(created);
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      className="dialog astryx-dialog proj-dialog"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <form
        aria-label="New project"
        onSubmit={(event) => {
          event.preventDefault();
          if (keyOk && !busy) void create();
        }}
      >
        <h2 id={titleId}>New project</h2>
        <p className="muted">
          A project groups the repositories, workers and registries one team
          delivers to. Link its Jira project so tickets there know where to go.
        </p>
        <div className="proj-grid">
          <label className="form-field">
            <span>Key</span>
            <input
              aria-label="New project key"
              value={key}
              placeholder="PAY"
              autoFocus
              onChange={(e) => setKey(e.target.value.toUpperCase())}
            />
            <Hint>Letters, digits, - and _. Used in links and the API.</Hint>
          </label>
          <label className="form-field">
            <span>Name</span>
            <input
              aria-label="New project name"
              value={name}
              placeholder={key || "Payments"}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label className="form-field">
            <span>Jira project key</span>
            <input
              aria-label="New project Jira key"
              value={jira}
              placeholder={key || "PAY"}
              onChange={(e) => setJira(e.target.value.toUpperCase())}
            />
            <Hint>
              Optional. Tickets in this Jira project use this project's
              repositories.
            </Hint>
          </label>
          <label className="form-field">
            <span>Default base branch</span>
            <input
              aria-label="New project base branch"
              value={branch}
              placeholder="dev"
              onChange={(e) => setBranch(e.target.value)}
            />
            <Hint>
              Optional. PRs target it unless a repository sets its own.
            </Hint>
          </label>
          {isAdmin && (
            <label className="form-field proj-span-2">
              <span>Owning team</span>
              <input
                aria-label="New project team"
                value={team}
                placeholder="payments"
                onChange={(e) => setTeam(e.target.value)}
              />
              <Hint>
                Optional. The team's managers can change this project.
              </Hint>
            </label>
          )}
        </div>
        {error && (
          <p className="field-error" role="alert">
            {error}
          </p>
        )}
        <div className="dialog-actions">
          <Button variant="secondary" type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" busy={busy} disabled={!keyOk}>
            Create project
          </Button>
        </div>
      </form>
    </dialog>
  );
}

function ProjectDetail({
  project,
  isAdmin,
  onSaved,
}: {
  project: Project;
  isAdmin: boolean;
  onSaved: (p: Project) => void;
}) {
  const manage = Boolean(project.can_manage);
  return (
    <div className="proj-detail">
      <header className="proj-detail-head">
        <div>
          <h2>{project.name}</h2>
          <p className="proj-meta">
            <code className="proj-key">{project.key}</code>
            {project.jira_project_key ? (
              <span>Jira {project.jira_project_key}</span>
            ) : (
              <span className="muted">No Jira project</span>
            )}
            <span>
              {project.team_slug
                ? `Team ${project.team_slug}`
                : "No owning team"}
            </span>
            {!manage && <span className="proj-readonly">Read only</span>}
          </p>
        </div>
      </header>
      <SettingsCard
        key={`delivery-${project.id}`}
        title="Delivery"
        label="Project settings"
        description="How work for this project reaches its repositories."
        project={project}
        isAdmin={isAdmin}
        dirtyKeys={["name", "team", "jira", "branch"]}
        onSaved={onSaved}
      >
        {(fields, set) => (
          <>
            <label className="form-field">
              <span>Name</span>
              <input
                aria-label="Project name"
                value={fields.name}
                onChange={(e) => set({ name: e.target.value })}
              />
            </label>
            <label className="form-field">
              <span>Owning team</span>
              <input
                aria-label="Owning team"
                value={fields.team}
                disabled={!isAdmin}
                placeholder="No team"
                onChange={(e) => set({ team: e.target.value })}
              />
              {!isAdmin && (
                <Hint>Only admins can move a project to another team.</Hint>
              )}
            </label>
            <label className="form-field">
              <span>Jira project key</span>
              <input
                aria-label="Jira project key"
                value={fields.jira}
                placeholder="None"
                onChange={(e) => set({ jira: e.target.value.toUpperCase() })}
              />
              <Hint>
                Tickets in this Jira project without a Repo: line use the
                repositories below.
              </Hint>
            </label>
            <label className="form-field">
              <span>Default base branch</span>
              <input
                aria-label="Default base branch"
                value={fields.branch}
                placeholder="dev"
                onChange={(e) => set({ branch: e.target.value })}
              />
              <Hint>
                PRs target it unless a repository sets its own branch.
              </Hint>
            </label>
          </>
        )}
      </SettingsCard>
      <RepositoriesPanel
        key={`repos-${project.id}`}
        projectId={project.id}
        canManage={manage}
        projectDefaultBranch={project.default_base_branch}
      />
      <SettingsCard
        key={`testing-${project.id}`}
        title="Testing"
        label="Testing"
        description="How this project's tickets prove their acceptance criteria. Each program fixes the choice for its repositories when it starts."
        project={project}
        isAdmin={isAdmin}
        dirtyKeys={["bdd"]}
        onSaved={onSaved}
      >
        {(fields, set) => (
          <label className="form-field proj-span-2">
            <span>Acceptance tests (BDD)</span>
            <select
              aria-label="Acceptance tests (BDD)"
              value={fields.bdd}
              onChange={(e) => set({ bdd: e.target.value })}
            >
              <option value="auto">
                Auto: Gherkin for repositories with no tests yet or already
                using it
              </option>
              <option value="always">
                Always: Gherkin scenarios in every repository
              </option>
              <option value="never">
                Never: the repository's own test runner
              </option>
            </select>
            <Hint>
              With Gherkin, every ticket adds a feature file named after its
              key. A repository without a Gherkin runner gets one with its first
              ticket. Unit tests are required either way.
            </Hint>
          </label>
        )}
      </SettingsCard>
      <SettingsCard
        key={`workers-${project.id}`}
        title="Coding workers"
        label="Coding workers"
        description="The size of each coding job's container. Leave both blank for the install's default."
        project={project}
        isAdmin={isAdmin}
        dirtyKeys={["cpu", "memory"]}
        validate={(fields) => sizeError(fields.cpu, fields.memory)}
        onSaved={onSaved}
      >
        {(fields, set) => (
          <>
            <label className="form-field">
              <span>Worker CPU</span>
              <select
                aria-label="Worker CPU"
                value={fields.cpu}
                onChange={(e) => set({ cpu: e.target.value })}
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
                value={fields.memory}
                placeholder={
                  fields.cpu
                    ? `${SIZES[Number(fields.cpu)][0]}–${SIZES[Number(fields.cpu)][1]}`
                    : "Default"
                }
                onChange={(e) => set({ memory: e.target.value.trim() })}
              />
            </label>
          </>
        )}
      </SettingsCard>
      {manage ? (
        <RegistriesPanel
          key={`registries-${project.id}`}
          projectId={project.id}
        />
      ) : (
        <section className="proj-card" aria-label="Package registries">
          <header className="proj-card-head">
            <div>
              <h3>Package registries</h3>
              <p className="muted">
                Only admins and managers of the owning team can see this
                project's registries.
              </p>
            </div>
          </header>
        </section>
      )}
      <ProjectNotesPanel
        key={`notes-${project.id}`}
        projectId={project.id}
        canManage={manage}
        isAdmin={isAdmin}
      />
    </div>
  );
}

export function ProjectSettingsPage({ selectedId }: { selectedId?: string }) {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [isAdmin, setIsAdmin] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [query, setQuery] = useState("");

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
  const needle = query.trim().toLowerCase();
  const shown = needle
    ? projects.filter((p) =>
        [p.name, p.key, p.team_slug, p.jira_project_key].some((value) =>
          String(value || "")
            .toLowerCase()
            .includes(needle),
        ),
      )
    : projects;

  return (
    <div className="proj-page">
      <div className="proj-toolbar">
        <p className="muted">
          {projects.length} {projects.length === 1 ? "project" : "projects"}
        </p>
        {isAdmin && (
          <Button variant="primary" size="sm" onClick={() => setCreating(true)}>
            New project
          </Button>
        )}
      </div>
      <div className="proj-layout">
        <aside className="proj-list" aria-label="Projects">
          {projects.length > 5 && (
            <input
              className="proj-search"
              type="search"
              aria-label="Find a project"
              placeholder="Find a project"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          )}
          {projects.length === 0 ? (
            <p className="proj-empty">
              No projects yet.
              {isAdmin
                ? " Create one to link a Jira project to its repositories."
                : ""}
            </p>
          ) : (
            <ul>
              {shown.map((p) => (
                <li key={p.id}>
                  <button
                    type="button"
                    aria-current={p.id === selected?.id ? "true" : undefined}
                    className={p.id === selected?.id ? "active" : ""}
                    onClick={() => select(p)}
                  >
                    <span className="proj-list-name">
                      <strong>{p.name}</strong>
                      <code className="proj-key">{p.key}</code>
                    </span>
                    <span className="proj-list-meta muted">
                      {[
                        p.team_slug ? `Team ${p.team_slug}` : null,
                        p.jira_project_key
                          ? `Jira ${p.jira_project_key}`
                          : null,
                      ]
                        .filter(Boolean)
                        .join(" · ") || "No team or Jira project"}
                    </span>
                  </button>
                </li>
              ))}
              {shown.length === 0 && (
                <li className="proj-empty">No project matches “{query}”.</li>
              )}
            </ul>
          )}
        </aside>
        <section className="proj-main">
          {selected ? (
            <ProjectDetail
              key={selected.id}
              project={selected}
              isAdmin={isAdmin}
              onSaved={(saved) =>
                setProjects((all) =>
                  (all || []).map((p) => (p.id === saved.id ? saved : p)),
                )
              }
            />
          ) : (
            <PageState
              kind="empty"
              title="Select a project"
              detail="Choose a project to set its Jira link, repositories, workers and registries."
            />
          )}
        </section>
      </div>
      <NewProjectDialog
        open={creating}
        isAdmin={isAdmin}
        onClose={() => setCreating(false)}
        onCreated={(p) => {
          setCreating(false);
          void load();
          select(p);
        }}
      />
    </div>
  );
}
