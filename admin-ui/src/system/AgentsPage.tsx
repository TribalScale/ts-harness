import { useCallback, useEffect, useMemo, useState } from "react";
import { apiFetch } from "../api/client";
import { Button } from "../components/Button";
import { PlusIcon } from "../components/PlusIcon";
import { PageState } from "../components/PageState";
import { paginate, Pagination } from "../components/Pagination";

type RosterRow = {
  id: string;
  source: string;
  default?: boolean;
  model?: string;
  skills?: string[];
  toolsets?: string[];
  tools?: { allow?: string[]; deny?: string[] };
  connector_bindings?: Record<string, string>;
  soul?: string;
};

type AgentObject = {
  workspace?: string;
  model?: string;
  display_name?: string;
  soul?: string;
  skills?: string[];
  toolsets?: string[];
  tools?: { allow?: string[]; deny?: string[] };
  connector_bindings?: Record<string, string>;
  default?: boolean;
  delegatable?: boolean;
};

type AgentsDefinition = {
  definition_type: string;
  definition_version: string;
  agents: Record<string, AgentObject>;
};

type AgentsDraft = {
  id: string;
  revision: string;
  definition: AgentsDefinition;
};

type AgentsGet = {
  draft: AgentsDraft | null;
  definition: AgentsDefinition;
  roster: RosterRow[];
  toolsets: string[];
  tool_catalog?: Record<string, string[]>;
  discovered_tools?: Record<string, string[]>;
  google_accounts?: string[];
  yaml_agent_ids?: string[];
};

function listedTools(names: string[] | undefined): string[] {
  return (names || []).map((name) => name.trim()).filter(Boolean);
}

function toolsetMembers(
  toolset: string,
  catalog: Record<string, string[]>,
  discovered: Record<string, string[]>,
): string[] {
  if (toolset === "mcp" || toolset === "google-workspace") {
    const custom = listedTools(catalog[toolset]);
    return custom.length ? custom : listedTools(discovered[toolset]);
  }
  return listedTools(catalog[toolset]);
}

function effectiveToolNames(
  agent: AgentObject,
  catalog: Record<string, string[]>,
  discovered: Record<string, string[]>,
): Set<string> {
  const allow = listedTools(agent.tools?.allow);
  const deny = new Set(listedTools(agent.tools?.deny));
  const toolsets = agent.toolsets || [];
  const tools = new Set<string>();
  if (allow.length) {
    allow.forEach((name) => tools.add(name));
    for (const name of toolsets) {
      if (name === "mcp") {
        listedTools(discovered.mcp).forEach((tool) => tools.add(tool));
      } else if (name === "google-workspace") {
        listedTools(discovered["google-workspace"]).forEach((tool) => tools.add(tool));
      } else {
        const custom = listedTools(catalog[name]);
        if (custom.length) custom.forEach((tool) => tools.add(tool));
      }
    }
  } else {
    for (const name of toolsets) {
      toolsetMembers(name, catalog, discovered).forEach((tool) => tools.add(tool));
    }
  }
  deny.forEach((name) => tools.delete(name));
  return tools;
}

function emptyAgent(id: string): AgentObject {
  return {
    workspace: id ? `/workspace/${id}` : "",
    model: "",
    display_name: "",
    soul: "",
    skills: [],
    toolsets: ["core-read"],
    default: false,
    delegatable: false,
  };
}

export function AgentsPage({
  selectedId,
  onDirtyChange,
}: {
  selectedId?: string;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const [payload, setPayload] = useState<AgentsGet | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [activated, setActivated] = useState(false);
  const [agentId, setAgentId] = useState(selectedId && selectedId !== "new" ? selectedId : "");
  const [form, setForm] = useState<AgentObject>(emptyAgent(selectedId && selectedId !== "new" ? selectedId : ""));
  const [skillsText, setSkillsText] = useState("");
  const [page, setPage] = useState(1);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const next = await apiFetch<AgentsGet>("/admin/agents");
      setPayload(next);
      setError(null);
      const currentId = selectedId && selectedId !== "new" ? selectedId : "";
      const fromOverlay = currentId
        ? next.draft?.definition.agents[currentId] || next.definition.agents[currentId]
        : undefined;
      const fromRoster = currentId
        ? next.roster.find((row) => row.id === currentId)
        : undefined;
      if (fromOverlay) {
        setAgentId(currentId);
        setForm({ ...emptyAgent(currentId), ...fromOverlay });
        setSkillsText((fromOverlay.skills || []).join(", "));
      } else if (fromRoster) {
        setAgentId(currentId);
        setForm({
          ...emptyAgent(currentId),
          model: fromRoster.model || "",
          skills: fromRoster.skills || [],
          toolsets: fromRoster.toolsets || ["core-read"],
          tools: {
            allow: listedTools(fromRoster.tools?.allow),
            deny: listedTools(fromRoster.tools?.deny),
          },
          connector_bindings: { ...(fromRoster.connector_bindings || {}) },
          soul: fromRoster.soul || "",
          default: Boolean(fromRoster.default),
        });
        setSkillsText((fromRoster.skills || []).join(", "));
      }
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }, [selectedId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    onDirtyChange?.(false);
    return () => onDirtyChange?.(false);
  }, [onDirtyChange]);

  const roster = payload?.roster || [];
  const toolsets = payload?.toolsets || [];
  const catalog = payload?.tool_catalog || {};
  const discovered = payload?.discovered_tools || {};
  const googleAccounts = payload?.google_accounts || [];
  const effective = effectiveToolNames(form, catalog, discovered);
  const toolChoices = Array.from(
    new Set([
      ...effective,
      ...listedTools(form.tools?.allow),
      ...listedTools(form.tools?.deny),
      ...(form.toolsets || []).flatMap((name) => toolsetMembers(name, catalog, discovered)),
    ]),
  ).sort();
  const isDetail = Boolean(selectedId);
  const rosterPage = paginate(roster, page);

  const definitionForSave = useMemo(() => {
    const agents = { ...(payload?.draft?.definition.agents || payload?.definition.agents || {}) };
    const id = agentId.trim();
    if (id) {
      agents[id] = {
        ...form,
        workspace: `/workspace/${id}`,
        skills: skillsText
          .split(",")
          .map((item) => item.trim())
          .filter(Boolean),
        default: false,
      };
    }
    return {
      definition_type: "agents",
      definition_version: "1.0.0",
      agents,
    };
  }, [agentId, form, payload, skillsText]);

  async function persistDefinition(definition: AgentsDefinition) {
    const draft = payload?.draft;
    if (draft) {
      await apiFetch(`/admin/agents/drafts/${encodeURIComponent(draft.id)}`, {
        method: "PUT",
        body: JSON.stringify({
          definition,
          expected_revision: draft.revision,
        }),
      });
      return;
    }
    await apiFetch("/admin/agents/drafts", {
      method: "POST",
      body: JSON.stringify({ definition }),
    });
  }

  async function saveDraft() {
    setBusy("save");
    try {
      await persistDefinition(definitionForSave);
      if (agentId.trim()) {
        window.location.hash = `#/system/agents/${encodeURIComponent(agentId.trim())}`;
      }
      onDirtyChange?.(false);
      setActivated(false);
      await load();
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(null);
    }
  }

  async function resetOrRemove() {
    const id = agentId.trim();
    if (!id) return;
    const agents = { ...(payload?.draft?.definition.agents || payload?.definition.agents || {}) };
    delete agents[id];
    setBusy("reset");
    try {
      await persistDefinition({
        definition_type: "agents",
        definition_version: "1.0.0",
        agents,
      });
      onDirtyChange?.(false);
      window.location.hash = "#/system/agents";
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(null);
    }
  }

  async function validateDraft() {
    const draft = payload?.draft;
    if (!draft) {
      setError("Save a draft before validating.");
      return;
    }
    setBusy("validate");
    try {
      const result = await apiFetch<{ valid?: boolean; issues?: { message?: string }[] }>(
        `/admin/agents/drafts/${encodeURIComponent(draft.id)}/validate`,
        { method: "POST" }
      );
      if (result.valid) {
        setError(null);
      } else {
        setError(
          (result.issues || [])
            .map((issue) => issue.message)
            .filter(Boolean)
            .join("; ") || "validation failed"
        );
      }
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(null);
    }
  }

  async function activate() {
    const draft = payload?.draft;
    if (!draft) return;
    setBusy("activate");
    try {
      await apiFetch("/admin/agents/activate", {
        method: "POST",
        body: JSON.stringify({
          draft_id: draft.id,
          expected_revision: draft.revision,
        }),
      });
      setActivated(true);
      await load();
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(null);
    }
  }

  if (loading && !payload) {
    return <PageState kind="loading" title="Loading agents" />;
  }
  if (error && !payload) {
    return (
      <PageState
        kind="error"
        title="Unable to load agents"
        detail={error}
        onRetry={() => void load()}
      />
    );
  }

  if (!isDetail) {
    return (
      <section className="panel-pad" aria-label="Agents roster">
        <div className="row-between">
          <h3>Agents</h3>
          <Button
            variant="primary"
            type="button"
            icon={<PlusIcon />}
            onClick={() => (window.location.hash = "#/system/agents/new")}
          >
            New agent
          </Button>
        </div>
        {error && <p className="muted">{error}</p>}
        <table className="data-table">
          <thead>
            <tr>
              <th>Id</th>
              <th>Source</th>
              <th>Model</th>
            </tr>
          </thead>
          <tbody>
            {rosterPage.items.map((row) => (
              <tr key={row.id}>
                <td>
                  <a href={`#/system/agents/${encodeURIComponent(row.id)}`}>{row.id}</a>
                </td>
                <td>{row.source === "overlay" ? "Overlay" : "Inherited"}</td>
                <td>{row.model}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <Pagination
          page={rosterPage.page}
          pageCount={rosterPage.pageCount}
          total={rosterPage.total}
          start={rosterPage.start}
          end={rosterPage.end}
          onPageChange={setPage}
          itemLabel="agents"
        />
      </section>
    );
  }

  return (
    <section className="panel-pad agent-form" aria-label="Agent wizard">
      <p>
        <a href="#/system/agents">Back to roster</a>
      </p>
      {error && <p className="muted">{error}</p>}
      <label className="form-field">
        <span>Agent id</span>
        <input
          value={agentId}
          readOnly={Boolean(selectedId && selectedId !== "new")}
          onChange={(event) => {
            setAgentId(event.target.value);
            onDirtyChange?.(true);
          }}
        />
      </label>
      <label className="form-field">
        <span>Display name</span>
        <input
          value={form.display_name || ""}
          onChange={(event) => {
            setForm({ ...form, display_name: event.target.value });
            onDirtyChange?.(true);
          }}
        />
      </label>
      <label className="form-field">
        <span>Soul</span>
        <textarea
          value={form.soul || ""}
          onChange={(event) => {
            setForm({ ...form, soul: event.target.value });
            onDirtyChange?.(true);
          }}
        />
      </label>
      <label className="form-field">
        <span>Model</span>
        <input
          value={form.model || ""}
          onChange={(event) => {
            setForm({ ...form, model: event.target.value });
            onDirtyChange?.(true);
          }}
        />
      </label>
      <label className="form-field">
        <span>Skills</span>
        <input
          value={skillsText}
          onChange={(event) => {
            setSkillsText(event.target.value);
            onDirtyChange?.(true);
          }}
        />
      </label>
      <fieldset>
        <legend>Toolsets</legend>
        {toolsets.map((toolset) => (
          <label key={toolset}>
            <input
              type="checkbox"
              checked={(form.toolsets || []).includes(toolset)}
              onChange={(event) => {
                const next = new Set(form.toolsets || []);
                if (event.target.checked) next.add(toolset);
                else next.delete(toolset);
                setForm({ ...form, toolsets: Array.from(next) });
                onDirtyChange?.(true);
              }}
            />
            {toolset}
          </label>
        ))}
      </fieldset>
      <label className="form-field">
        <span>Google account</span>
        <select
          aria-label="Google account"
          value={form.connector_bindings?.["google-workspace"] || ""}
          onChange={(event) => {
            setForm({
              ...form,
              connector_bindings: {
                ...(form.connector_bindings || {}),
                "google-workspace": event.target.value,
              },
            });
            onDirtyChange?.(true);
          }}
        >
          <option value="">None</option>
          {googleAccounts.map((account) => (
            <option key={account} value={account}>
              {account}
            </option>
          ))}
        </select>
      </label>
      <fieldset>
        <legend>Tools</legend>
        <p className="muted">
          A checked tool is one this agent can run. Toolset membership still applies to every
          agent that uses that toolset. Unchecking a tool here denies it for this agent only.
        </p>
        {toolChoices.map((tool) => (
          <label key={tool}>
            <input
              type="checkbox"
              aria-label={tool}
              checked={effective.has(tool)}
              onChange={(event) => {
                const allow = new Set(listedTools(form.tools?.allow));
                const deny = new Set(listedTools(form.tools?.deny));
                if (event.target.checked) {
                  deny.delete(tool);
                  allow.add(tool);
                } else {
                  allow.delete(tool);
                  deny.add(tool);
                }
                setForm({
                  ...form,
                  tools: { allow: Array.from(allow), deny: Array.from(deny) },
                });
                onDirtyChange?.(true);
              }}
            />
            {tool}
          </label>
        ))}
      </fieldset>
      <label>
        <input
          type="checkbox"
          checked={Boolean(form.delegatable)}
          onChange={(event) => {
            setForm({ ...form, delegatable: event.target.checked });
            onDirtyChange?.(true);
          }}
        />
        Delegatable
      </label>
      <div className="row">
        <Button busy={busy === "save"} onClick={() => void saveDraft()}>
          Save draft
        </Button>
        <Button busy={busy === "validate"} onClick={() => void validateDraft()}>
          Validate
        </Button>
        <Button busy={busy === "activate"} onClick={() => void activate()}>
          Activate
        </Button>
        {selectedId && selectedId !== "new" ? (
          <Button busy={busy === "reset"} onClick={() => void resetOrRemove()}>
            {(payload?.yaml_agent_ids || []).includes(agentId.trim()) ? "Reset" : "Remove"}
          </Button>
        ) : null}
      </div>
      {activated && (
        <p>
          <a href="#/system/settings/routing">Add a routing intent</a>
          {" · "}
          <a href="#/system/mcp">MCP</a>
        </p>
      )}
    </section>
  );
}
