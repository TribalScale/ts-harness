import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { apiFetch } from "../api/client";
import { Button } from "../components/Button";
import { PageState } from "../components/PageState";
import { pageForIndex, paginate, Pagination } from "../components/Pagination";
import { PlusIcon } from "../components/PlusIcon";
import { Tabs, tabButtonId, tabPanelId } from "../components/Tabs";

const ADD_TABS = [
  { id: "http", label: "HTTP" },
  { id: "catalog", label: "Catalog" },
];

type McpServer = {
  transport?: string;
  url?: string;
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  headers?: Record<string, string>;
  enabled?: boolean;
};

type McpDefinition = {
  definition_type: string;
  definition_version: string;
  servers: Record<string, McpServer>;
};

type McpDraft = {
  id: string;
  revision: string;
  definition: McpDefinition;
};

type CatalogEntry = {
  id: string;
  display_name: string;
  transport: string;
  command: string;
  args: string[];
  env_names: string[];
  notes?: string;
};

type McpGet = {
  draft: McpDraft | null;
  definition: McpDefinition;
  merged: Record<string, McpServer>;
  sources: Record<string, string>;
  catalog: CatalogEntry[];
  yaml?: Record<string, McpServer>;
};

type LiveTools = { name: string; server?: string };

function emptyDefinition(): McpDefinition {
  return { definition_type: "mcp_settings", definition_version: "1.0.0", servers: {} };
}

function serverNames(merged: Record<string, McpServer>, overlay: Record<string, McpServer>): string[] {
  return Array.from(new Set([...Object.keys(merged), ...Object.keys(overlay)])).sort();
}

export function McpPage({
  selectedName,
  onDirtyChange,
}: {
  selectedName?: string;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const [payload, setPayload] = useState<McpGet | null>(null);
  const [draft, setDraft] = useState<McpDraft | null>(null);
  const [definition, setDefinition] = useState<McpDefinition>(emptyDefinition());
  const [liveTools, setLiveTools] = useState<LiveTools[]>([]);
  const [toolCounts, setToolCounts] = useState<Record<string, number>>({});
  const [testedTools, setTestedTools] = useState<Record<string, LiveTools[]>>({});
  const [error, setError] = useState<string | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [httpName, setHttpName] = useState("");
  const [httpTransport, setHttpTransport] = useState("streamable_http");
  const [httpUrl, setHttpUrl] = useState("");
  const [httpHeaderName, setHttpHeaderName] = useState("Authorization");
  const [httpHeaderSecret, setHttpHeaderSecret] = useState("");
  const [catalogId, setCatalogId] = useState("");
  const [catalogSecrets, setCatalogSecrets] = useState<Record<string, string>>({});
  const [page, setPage] = useState(1);
  const [adding, setAdding] = useState(false);
  const [addTab, setAddTab] = useState<"http" | "catalog">("http");
  const dialogRef = useRef<HTMLDialogElement>(null);
  const firstFieldRef = useRef<HTMLInputElement>(null);
  const formTitleId = useId();

  const markDirty = useCallback(
    (next: boolean) => {
      setDirty(next);
      onDirtyChange?.(next);
    },
    [onDirtyChange]
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiFetch<McpGet>("/admin/mcp-settings");
      setPayload(data);
      const nextDraft = data.draft;
      setDraft(nextDraft);
      setDefinition(nextDraft?.definition || data.definition || emptyDefinition());
      markDirty(false);
      try {
        const tools = await apiFetch<{ tools: LiveTools[] }>("/mcp/tools");
        setLiveTools(tools.tools || []);
      } catch {
        setLiveTools([]);
      }
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }, [markDirty]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  const overlayServers = definition.servers || {};
  const merged = payload?.merged || {};
  const sources = payload?.sources || {};
  const names = useMemo(() => serverNames(merged, overlayServers), [merged, overlayServers]);
  const selected = selectedName && names.includes(selectedName) ? selectedName : names[0];
  const catalog = payload?.catalog || [];
  const selectedCatalog = catalog.find((entry) => entry.id === catalogId);

  useEffect(() => {
    if (!selectedName) return;
    const index = names.indexOf(selectedName);
    if (index >= 0) setPage(pageForIndex(index));
  }, [selectedName, names]);

  const serverPage = paginate(names, page);

  function closeAdd() {
    setAdding(false);
    setAddTab("http");
    setHttpName("");
    setHttpUrl("");
    setHttpTransport("streamable_http");
    setHttpHeaderName("Authorization");
    setHttpHeaderSecret("");
    setCatalogId("");
    setCatalogSecrets({});
  }

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (adding && !dialog.open) {
      if (typeof dialog.showModal === "function") {
        dialog.showModal();
      } else {
        dialog.setAttribute("open", "");
      }
      firstFieldRef.current?.focus();
    } else if (!adding && dialog.open) {
      if (typeof dialog.close === "function") {
        dialog.close();
      } else {
        dialog.removeAttribute("open");
      }
    }
  }, [adding]);

  async function ensureDraft(next: McpDefinition): Promise<McpDraft> {
    if (draft) return draft;
    const created = await apiFetch<{ draft: McpDraft }>("/admin/mcp-settings/drafts", {
      method: "POST",
      body: JSON.stringify({ definition: next }),
    });
    setDraft(created.draft);
    return created.draft;
  }

  async function persist(next: McpDefinition) {
    const current = await ensureDraft(next);
    const updated = await apiFetch<{ draft: McpDraft }>(
      `/admin/mcp-settings/drafts/${encodeURIComponent(current.id)}`,
      {
        method: "PUT",
        body: JSON.stringify({
          definition: next,
          expected_revision: current.revision,
        }),
      }
    );
    setDraft(updated.draft);
    setDefinition(updated.draft.definition || next);
    markDirty(false);
  }

  async function addHttp() {
    setError(null);
    setBanner(null);
    setBusy("add");
    try {
      const name = httpName.trim();
      if (!name) throw new Error("Server name is required");
      const headers: Record<string, string> = {};
      if (httpHeaderSecret.trim()) {
        headers[httpHeaderName.trim() || "Authorization"] = `secret:${httpHeaderSecret.trim()}`;
      }
      const next: McpDefinition = {
        ...definition,
        servers: {
          ...overlayServers,
          [name]: {
            transport: httpTransport,
            url: httpUrl.trim(),
            enabled: true,
            ...(Object.keys(headers).length ? { headers } : {}),
          },
        },
      };
      await persist(next);
      closeAdd();
      window.location.hash = `#/system/mcp/${encodeURIComponent(name)}`;
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(null);
    }
  }

  async function installCatalog() {
    setError(null);
    setBanner(null);
    setBusy("install");
    try {
      if (!selectedCatalog) throw new Error("Pick a catalog entry");
      const env: Record<string, string> = {};
      for (const envName of selectedCatalog.env_names || []) {
        const secret = (catalogSecrets[envName] || "").trim();
        if (!secret) throw new Error(`Secret name required for ${envName}`);
        env[envName] = `secret:${secret}`;
      }
      const next: McpDefinition = {
        ...definition,
        servers: {
          ...overlayServers,
          [selectedCatalog.id]: {
            transport: selectedCatalog.transport,
            command: selectedCatalog.command,
            args: selectedCatalog.args || [],
            enabled: true,
            ...(Object.keys(env).length ? { env } : {}),
          },
        },
      };
      await persist(next);
      closeAdd();
      window.location.hash = `#/system/mcp/${encodeURIComponent(selectedCatalog.id)}`;
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(null);
    }
  }

  async function setEnabled(name: string, enabled: boolean) {
    setError(null);
    const next: McpDefinition = {
      ...definition,
      servers: {
        ...overlayServers,
        [name]: { ...(overlayServers[name] || {}), enabled },
      },
    };
    setDefinition(next);
    markDirty(true);
    setBusy(`enable:${name}`);
    try {
      await persist(next);
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(null);
    }
  }

  async function resetOrRemove(name: string) {
    setError(null);
    const nextServers = { ...overlayServers };
    delete nextServers[name];
    const next = { ...definition, servers: nextServers };
    setDefinition(next);
    markDirty(true);
    setBusy(`reset:${name}`);
    try {
      await persist(next);
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(null);
    }
  }

  async function runTest(name: string) {
    setError(null);
    setBanner(null);
    setBusy(`test:${name}`);
    try {
      const result = await apiFetch<{ ok: boolean; tools: LiveTools[]; error?: string }>(
        `/admin/mcp/servers/${encodeURIComponent(name)}/test`,
        { method: "POST" }
      );
      setToolCounts((current) => ({ ...current, [name]: (result.tools || []).length }));
      setTestedTools((current) => ({ ...current, [name]: result.tools || [] }));
      if (!result.ok) {
        setBanner(result.error || `Test failed for ${name}`);
      } else {
        setBanner(`Tested ${name}: ${(result.tools || []).length} tools`);
      }
    } catch (e: any) {
      setBanner(String(e.message || e));
    } finally {
      setBusy(null);
    }
  }

  async function activate() {
    setError(null);
    setBanner(null);
    if (!draft) {
      setBanner("Save a draft before Activate");
      return;
    }
    setBusy("activate");
    try {
      if (dirty) await persist(definition);
      const result = await apiFetch<{ degraded?: string[] }>("/admin/mcp-settings/activate", {
        method: "POST",
        body: JSON.stringify({ draft_id: draft.id, expected_revision: draft.revision }),
      });
      const degraded = result.degraded || [];
      setBanner(
        degraded.length
          ? `Activated with degraded servers: ${degraded.join(", ")}`
          : "Activated MCP settings"
      );
      await load();
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(null);
    }
  }

  function toolCount(name: string): string {
    if (toolCounts[name] != null) return String(toolCounts[name]);
    const live = liveTools.filter((tool) => tool.server === name || tool.name?.startsWith(`mcp-${name}-`));
    return live.length ? String(live.length) : "—";
  }

  if (loading && !payload) {
    return <PageState kind="loading" title="Loading MCP settings" />;
  }

  return (
    <div className="mcp-page">
      <div className="cron-toolbar">
        <div>
          <h3>MCP servers</h3>
          <p className="muted">Overlay HTTP or catalog servers onto the YAML bootstrap. Secrets stay named, never listed.</p>
        </div>
        <div className="row-actions">
          <Button
            type="button"
            variant="primary"
            icon={<PlusIcon />}
            onClick={() => {
              setError(null);
              setBanner(null);
              setAdding(true);
            }}
          >
            Add MCP
          </Button>
          <Button type="button" variant="secondary" busy={busy === "activate"} onClick={() => void activate()}>
            Activate
          </Button>
        </div>
      </div>
      {error && !adding && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
      {banner && (
        <div className="success" role="status">
          {banner}
        </div>
      )}
      {names.length ? (
        <>
        <table className="cron-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Source</th>
              <th>Transport</th>
              <th>Enabled</th>
              <th>Tools</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {serverPage.items.map((name) => {
              const server = { ...(merged[name] || {}), ...(overlayServers[name] || {}) };
              const source = overlayServers[name] ? "overlay" : sources[name] || "inherited";
              const inherited = Boolean((payload?.yaml || {})[name]);
              return (
                <tr key={name} data-selected={name === selected ? "true" : "false"}>
                  <td>
                    <a href={`#/system/mcp/${encodeURIComponent(name)}`}>{name}</a>
                  </td>
                  <td>{source}</td>
                  <td className="mono">{server.transport || "—"}</td>
                  <td>{server.enabled === false ? "No" : "Yes"}</td>
                  <td>{toolCount(name)}</td>
                  <td>
                    <div className="row-actions">
                      <Button
                        variant="ghost"
                        busy={busy === `enable:${name}`}
                        onClick={() => void setEnabled(name, server.enabled === false)}
                      >
                        {server.enabled === false ? "Enable" : "Disable"}
                      </Button>
                      <Button variant="ghost" busy={busy === `test:${name}`} onClick={() => void runTest(name)}>
                        Test
                      </Button>
                      <Button variant="ghost" busy={busy === `reset:${name}`} onClick={() => void resetOrRemove(name)}>
                        {source === "overlay" && !inherited ? "Remove" : "Reset"}
                      </Button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <Pagination
          page={serverPage.page}
          pageCount={serverPage.pageCount}
          total={serverPage.total}
          start={serverPage.start}
          end={serverPage.end}
          onPageChange={setPage}
          itemLabel="servers"
        />
        </>
      ) : (
        <PageState
          kind="empty"
          title="No MCP servers"
          detail="Use Add MCP to add an HTTP server or install from the reviewed catalog."
        />
      )}
      {selected && (
        <section className="mcp-detail" aria-label={`${selected} detail`}>
          <h4>{selected}</h4>
          <p className="muted">
            {(overlayServers[selected] ? "overlay" : sources[selected] || "inherited")} ·{" "}
            {(overlayServers[selected] || merged[selected] || {}).transport || "—"}
          </p>
          <p className="mono">{(overlayServers[selected] || merged[selected] || {}).url || (overlayServers[selected] || merged[selected] || {}).command || "—"}</p>
          <h4>Tools</h4>
          {(testedTools[selected] || liveTools.filter((tool) => tool.server === selected || tool.name?.startsWith(`mcp-${selected}-`))).length ? (
            <ul>
              {(testedTools[selected] || liveTools.filter((tool) => tool.server === selected || tool.name?.startsWith(`mcp-${selected}-`))).map((tool) => (
                <li key={tool.name} className="mono">
                  {tool.name}
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">No tools yet. Run Test or Activate to discover them.</p>
          )}
        </section>
      )}
      <dialog
        ref={dialogRef}
        aria-labelledby={formTitleId}
        className="dialog dialog-form"
        onCancel={(event) => {
          event.preventDefault();
          closeAdd();
        }}
      >
        {adding && (
          <section className="mcp-add-form" aria-label="Add MCP server">
            <h2 id={formTitleId}>Add MCP server</h2>
            <Tabs
              idBase="mcp-add"
              ariaLabel="Add MCP method"
              active={addTab}
              onChange={(id) => setAddTab(id as "http" | "catalog")}
              tabs={ADD_TABS}
            />
            <div
              id={tabPanelId("mcp-add", "http")}
              role="tabpanel"
              hidden={addTab !== "http"}
              aria-labelledby={tabButtonId("mcp-add", "http")}
            >
              <label className="form-field">
                <span>Name</span>
                <input
                  ref={firstFieldRef}
                  aria-label="HTTP server name"
                  value={httpName}
                  onChange={(e) => setHttpName(e.target.value)}
                />
              </label>
              <label className="form-field">
                <span>Transport</span>
                <select
                  aria-label="HTTP transport"
                  value={httpTransport}
                  onChange={(e) => setHttpTransport(e.target.value)}
                >
                  <option value="streamable_http">streamable_http</option>
                  <option value="sse">sse</option>
                </select>
              </label>
              <label className="form-field">
                <span>URL</span>
                <input aria-label="HTTP server URL" value={httpUrl} onChange={(e) => setHttpUrl(e.target.value)} />
              </label>
              <label className="form-field">
                <span>Header name</span>
                <input
                  aria-label="Header name"
                  value={httpHeaderName}
                  onChange={(e) => setHttpHeaderName(e.target.value)}
                />
              </label>
              <label className="form-field">
                <span>Header secret name</span>
                <input
                  aria-label="Header secret name"
                  value={httpHeaderSecret}
                  onChange={(e) => setHttpHeaderSecret(e.target.value)}
                  placeholder="FIGMA_MCP_TOKEN"
                />
              </label>
            </div>
            <div
              id={tabPanelId("mcp-add", "catalog")}
              role="tabpanel"
              hidden={addTab !== "catalog"}
              aria-labelledby={tabButtonId("mcp-add", "catalog")}
            >
              <label className="form-field">
                <span>Catalog entry</span>
                <select aria-label="Catalog entry" value={catalogId} onChange={(e) => setCatalogId(e.target.value)}>
                  <option value="">Select…</option>
                  {catalog.map((entry) => (
                    <option key={entry.id} value={entry.id}>
                      {entry.display_name || entry.id}
                    </option>
                  ))}
                </select>
              </label>
              {(selectedCatalog?.env_names || []).map((envName) => (
                <label key={envName} className="form-field">
                  <span>{envName} secret name</span>
                  <input
                    aria-label={`${envName} secret name`}
                    value={catalogSecrets[envName] || ""}
                    onChange={(e) =>
                      setCatalogSecrets((current) => ({ ...current, [envName]: e.target.value }))
                    }
                  />
                </label>
              ))}
            </div>
            {error && <p className="field-error">{error}</p>}
            <div className="dialog-actions">
              <Button type="button" variant="ghost" disabled={busy === "add" || busy === "install"} onClick={closeAdd}>
                Cancel
              </Button>
              {addTab === "http" ? (
                <Button type="button" variant="primary" busy={busy === "add"} onClick={() => void addHttp()}>
                  Add HTTP
                </Button>
              ) : (
                <Button type="button" variant="primary" busy={busy === "install"} onClick={() => void installCatalog()}>
                  Install
                </Button>
              )}
            </div>
          </section>
        )}
      </dialog>
    </div>
  );
}
