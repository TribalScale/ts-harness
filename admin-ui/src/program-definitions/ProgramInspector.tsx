import { useEffect, useState } from "react";
import type {
  InputBinding,
  ProgramDefinition,
  ProgramDefinitionCatalog,
  ProgramEdge,
  ProgramJiraFiling,
  ProgramNode,
  ProgramRepoPicker,
  ProgramRuntimeConfig,
  ProgramTicketPicker,
  WorkflowVariant,
} from "./types";

export type EditableProgramNode = ProgramNode & { startable?: boolean };

function parseCsv(value: string): string[] {
  return value
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function variantWhenText(when: Record<string, string | number | boolean | null>): string {
  return Object.entries(when || {})
    .map(([key, value]) => `${key}=${value === null ? "null" : String(value)}`)
    .join(", ");
}

function parseVariantWhen(text: string): Record<string, string | number | boolean | null> {
  const when: Record<string, string | number | boolean | null> = {};
  for (const part of text.split(",").map((entry) => entry.trim()).filter(Boolean)) {
    const [key, ...rest] = part.split("=");
    if (!key) continue;
    const raw = rest.join("=");
    if (raw === "true") when[key] = true;
    else if (raw === "false") when[key] = false;
    else if (raw === "null") when[key] = null;
    else if (raw !== "" && !Number.isNaN(Number(raw))) when[key] = Number(raw);
    else when[key] = raw;
  }
  return when;
}

function bindingText(binding: InputBinding): string {
  const parts = [binding.kind, binding.target];
  if (binding.name) parts.push(`name=${binding.name}`);
  if (binding.limit !== undefined) parts.push(`limit=${binding.limit}`);
  if (binding.value !== undefined) parts.push(`value=${JSON.stringify(binding.value)}`);
  return parts.join(" ");
}

function parseBinding(text: string): InputBinding | null {
  const tokens = text.trim().split(/\s+/).filter(Boolean);
  if (tokens.length < 2) return null;
  const [kind, target, ...rest] = tokens;
  const binding: InputBinding = { kind, target };
  for (const token of rest) {
    const [key, ...valueParts] = token.split("=");
    const value = valueParts.join("=");
    if (key === "name") binding.name = value;
    if (key === "limit") binding.limit = Number(value);
    if (key === "value") {
      try {
        binding.value = JSON.parse(value);
      } catch {
        binding.value = value;
      }
    }
  }
  return binding;
}

function isInheritSection(
  section: { mode?: string } | undefined,
  customWhenOmitted: boolean
): boolean {
  if (!section) return true;
  if (section.mode === "inherit") return true;
  if (section.mode === "custom") return false;
  return !customWhenOmitted;
}

function JiraDefaultsCard({
  config,
  readOnly,
  onChange,
}: {
  config?: ProgramRuntimeConfig;
  readOnly: boolean;
  onChange: (config: ProgramRuntimeConfig) => void;
}) {
  const [repoId, setRepoId] = useState("");
  const [branch, setBranch] = useState("");
  const [projectKey, setProjectKey] = useState("");
  const [projectType, setProjectType] = useState("");
  const [statusText, setStatusText] = useState("");

  const repoPicker = config?.repo_picker;
  const filing = config?.jira_filing;
  const picker = config?.ticket_picker;
  const repoInherit = isInheritSection(repoPicker, Boolean(repoPicker?.repos?.length));
  const filingInherit = isInheritSection(
    filing,
    Boolean(filing?.default_issue_type || Object.keys(filing?.issue_type_by_project || {}).length)
  );
  const pickerInherit = isInheritSection(
    picker,
    Boolean(picker?.assignee || picker?.statuses?.length)
  );

  function patch(next: Partial<ProgramRuntimeConfig>) {
    onChange({ ...config, ...next });
  }

  return (
    <section>
      <h3>Jira defaults</h3>
      {readOnly && (
        <>
          <p className="muted">Built-in maps are read-only.</p>
          <p className="muted">
            <a className="inspector-settings-link" href="#/system/settings">
              Edit tenant defaults in Settings
            </a>
            {" "}or clone this program to change inherit vs custom.
          </p>
        </>
      )}
      <fieldset disabled={readOnly}>
        <legend>Repo allowlist</legend>
        <div className="radio-row">
          <label className="check-row">
            <input
              aria-label="Repo allowlist use tenant defaults"
              type="radio"
              name="repo-allowlist-mode"
              checked={repoInherit}
              onChange={() => patch({ repo_picker: { mode: "inherit" } })}
            />
            <span>Use tenant defaults</span>
          </label>
          <label className="check-row">
            <input
              aria-label="Repo allowlist custom"
              type="radio"
              name="repo-allowlist-mode"
              checked={!repoInherit}
              onChange={() =>
                patch({
                  repo_picker: { mode: "custom", repos: repoPicker?.repos || [] },
                })
              }
            />
            <span>Custom</span>
          </label>
        </div>
        {!repoInherit && (
          <>
            <table>
              <thead>
                <tr>
                  <th>Repo</th>
                  <th>Default branch</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {(repoPicker?.repos || []).map((row, index) => (
                  <tr key={`${row.id}-${index}`}>
                    <td>{row.id}</td>
                    <td>{row.default_branch || "dev"}</td>
                    <td>
                      <button
                        type="button"
                        className="link-button"
                        onClick={() =>
                          patch({
                            repo_picker: {
                              mode: "custom",
                              repos: (repoPicker?.repos || []).filter((_, itemIndex) => itemIndex !== index),
                            },
                          })
                        }
                      >
                        Remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="form-pair">
              <label className="form-field">
                <span>Repo</span>
                <input
                  aria-label="Program repo id"
                  placeholder="org/repo"
                  value={repoId}
                  onChange={(e) => setRepoId(e.target.value)}
                />
              </label>
              <label className="form-field">
                <span>Default branch</span>
                <input
                  aria-label="Program default branch"
                  placeholder="dev"
                  value={branch}
                  onChange={(e) => setBranch(e.target.value)}
                />
              </label>
              <button
                type="button"
                className="link-button"
                onClick={() => {
                  const id = repoId.trim();
                  if (!id) return;
                  const next: ProgramRepoPicker = {
                    mode: "custom",
                    repos: [
                      ...(repoPicker?.repos || []),
                      { id, default_branch: branch.trim() || "dev" },
                    ],
                  };
                  patch({ repo_picker: next });
                  setRepoId("");
                  setBranch("");
                }}
              >
                Add repo
              </button>
            </div>
          </>
        )}
      </fieldset>
      <fieldset disabled={readOnly}>
        <legend>Jira filing</legend>
        <div className="radio-row">
          <label className="check-row">
            <input
              aria-label="Jira filing use tenant defaults"
              type="radio"
              name="jira-filing-mode"
              checked={filingInherit}
              onChange={() => patch({ jira_filing: { mode: "inherit" } })}
            />
            <span>Use tenant defaults</span>
          </label>
          <label className="check-row">
            <input
              aria-label="Jira filing custom"
              type="radio"
              name="jira-filing-mode"
              checked={!filingInherit}
              onChange={() =>
                patch({
                  jira_filing: {
                    mode: "custom",
                    default_issue_type: filing?.default_issue_type || "Story",
                    issue_type_by_project: filing?.issue_type_by_project || {},
                  },
                })
              }
            />
            <span>Custom</span>
          </label>
        </div>
        {!filingInherit && (
          <>
            <label className="form-field">
              <span>Default issue type</span>
              <input
                aria-label="Program default issue type"
                value={filing?.default_issue_type || ""}
                onChange={(e) =>
                  patch({
                    jira_filing: {
                      mode: "custom",
                      default_issue_type: e.target.value,
                      issue_type_by_project: filing?.issue_type_by_project || {},
                    } satisfies ProgramJiraFiling,
                  })
                }
              />
            </label>
            <table>
              <thead>
                <tr>
                  <th>Project</th>
                  <th>Type</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(filing?.issue_type_by_project || {}).map(([key, value]) => (
                  <tr key={key}>
                    <td>{key}</td>
                    <td>{value}</td>
                    <td>
                      <button
                        type="button"
                        className="link-button"
                        aria-label={`Remove project ${key}`}
                        onClick={() => {
                          const next = { ...(filing?.issue_type_by_project || {}) };
                          delete next[key];
                          patch({
                            jira_filing: {
                              mode: "custom",
                              default_issue_type: filing?.default_issue_type || "Story",
                              issue_type_by_project: next,
                            },
                          });
                        }}
                      >
                        Remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="form-pair">
              <label className="form-field">
                <span>Project</span>
                <input
                  aria-label="Program filing project"
                  value={projectKey}
                  onChange={(e) => setProjectKey(e.target.value)}
                />
              </label>
              <label className="form-field">
                <span>Type</span>
                <input
                  aria-label="Program filing type"
                  value={projectType}
                  onChange={(e) => setProjectType(e.target.value)}
                />
              </label>
              <button
                type="button"
                className="link-button"
                onClick={() => {
                  if (!projectKey.trim() || !projectType.trim()) return;
                  patch({
                    jira_filing: {
                      mode: "custom",
                      default_issue_type: filing?.default_issue_type || "Story",
                      issue_type_by_project: {
                        ...(filing?.issue_type_by_project || {}),
                        [projectKey.trim()]: projectType.trim(),
                      },
                    },
                  });
                  setProjectKey("");
                  setProjectType("");
                }}
              >
                Add project type
              </button>
            </div>
          </>
        )}
      </fieldset>
      <fieldset disabled={readOnly}>
        <legend>Ticket picker</legend>
        <div className="radio-row">
          <label className="check-row">
            <input
              aria-label="Ticket picker use tenant defaults"
              type="radio"
              name="ticket-picker-mode"
              checked={pickerInherit}
              onChange={() => patch({ ticket_picker: { mode: "inherit" } })}
            />
            <span>Use tenant defaults</span>
          </label>
          <label className="check-row">
            <input
              aria-label="Ticket picker custom"
              type="radio"
              name="ticket-picker-mode"
              checked={!pickerInherit}
              onChange={() =>
                patch({
                  ticket_picker: {
                    mode: "custom",
                    assignee: "mapped_user",
                    statuses: picker?.statuses || [],
                  },
                })
              }
            />
            <span>Custom</span>
          </label>
        </div>
        {!pickerInherit && (
          <>
            <ul className="status-chips">
              {(picker?.statuses || []).map((status) => (
                <li key={status} className="status-chip">
                  <span>{status}</span>
                  <button
                    type="button"
                    className="link-button"
                    aria-label={`Remove status ${status}`}
                    onClick={() =>
                      patch({
                        ticket_picker: {
                          mode: "custom",
                          assignee: "mapped_user",
                          statuses: (picker?.statuses || []).filter((item) => item !== status),
                        },
                      })
                    }
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
            {!(picker?.statuses || []).length && <p className="muted">No custom statuses</p>}
            <div className="form-pair-wide">
              <label className="form-field">
                <span>Add status</span>
                <input
                  aria-label="Program ticket status"
                  value={statusText}
                  onChange={(e) => setStatusText(e.target.value)}
                />
              </label>
              <button
                type="button"
                className="link-button"
                onClick={() => {
                  const next = statusText.trim();
                  if (!next) return;
                  const statuses: ProgramTicketPicker = {
                    mode: "custom",
                    assignee: "mapped_user",
                    statuses: [...(picker?.statuses || []), next],
                  };
                  patch({ ticket_picker: statuses });
                  setStatusText("");
                }}
              >
                Add status
              </button>
            </div>
          </>
        )}
      </fieldset>
    </section>
  );
}

export function ProgramInspector({
  definition,
  selectedNode,
  selectedEdge,
  catalog,
  readOnly,
  onProgramChange,
  onNodeChange,
  onEdgeChange,
  onOpenWorkflow,
}: {
  definition: ProgramDefinition;
  selectedNode?: EditableProgramNode;
  selectedEdge?: ProgramEdge & { id: string };
  catalog: ProgramDefinitionCatalog;
  readOnly: boolean;
  onProgramChange: (definition: ProgramDefinition) => void;
  onNodeChange: (node: EditableProgramNode) => void;
  onEdgeChange: (edge: ProgramEdge & { id: string }) => void;
  onOpenWorkflow?: (workflowType: string) => void;
}) {
  const [programDraft, setProgramDraft] = useState(definition);
  const [nodeDraft, setNodeDraft] = useState<EditableProgramNode | undefined>(selectedNode);
  const [edgeDraft, setEdgeDraft] = useState<(ProgramEdge & { id: string }) | undefined>(selectedEdge);

  useEffect(() => setProgramDraft(definition), [definition]);
  useEffect(() => setNodeDraft(selectedNode), [selectedNode?.id, selectedNode?.startable]);
  useEffect(() => setEdgeDraft(selectedEdge), [selectedEdge?.id]);

  function updateProgram(patch: Partial<ProgramDefinition>) {
    const next = { ...programDraft, ...patch };
    setProgramDraft(next);
    onProgramChange(next);
  }

  function updateNode(patch: Partial<EditableProgramNode>) {
    if (!nodeDraft) return;
    const next = { ...nodeDraft, ...patch };
    setNodeDraft(next);
    onNodeChange(next);
  }

  function updateEdge(patch: Partial<ProgramEdge>) {
    if (!edgeDraft) return;
    const next = { ...edgeDraft, ...patch };
    setEdgeDraft(next);
    onEdgeChange(next);
  }

  function updateVariant(index: number, patch: Partial<WorkflowVariant>) {
    const variants = [...(nodeDraft?.workflow_variants || [])];
    variants[index] = { ...variants[index], ...patch };
    updateNode({ workflow_variants: variants });
  }

  function updateBinding(index: number, text: string) {
    const bindings = [...(nodeDraft?.input_bindings || [])];
    const parsed = parseBinding(text);
    if (!parsed) return;
    bindings[index] = parsed;
    updateNode({ input_bindings: bindings });
  }

  const workflowOptions = catalog.workflows.map((item) => item.workflow_type);
  const adapterOptions = catalog.adapters.map((item) => item.id);
  const modeOptions = catalog.transition_modes.length
    ? catalog.transition_modes
    : ["manual", "automatic"];

  return (
    <aside className="workflow-inspector" aria-label="Program inspector">
      <section>
        <h3>Program</h3>
        <label className="form-field">
          <span>Program type</span>
          <input
            aria-label="Program type"
            value={programDraft.program_type}
            disabled={readOnly}
            onChange={(e) => updateProgram({ program_type: e.target.value })}
          />
        </label>
        <label className="form-field">
          <span>Display name</span>
          <input
            aria-label="Display name"
            value={programDraft.display_name || ""}
            disabled={readOnly}
            onChange={(e) => updateProgram({ display_name: e.target.value })}
          />
        </label>
        <label className="form-field">
          <span>Definition version</span>
          <input
            aria-label="Definition version"
            value={programDraft.definition_version}
            disabled={readOnly}
            onChange={(e) => updateProgram({ definition_version: e.target.value })}
          />
        </label>
      </section>

      <JiraDefaultsCard
        config={programDraft.config}
        readOnly={readOnly}
        onChange={(config) => updateProgram({ config })}
      />

      <section>
        <h3>Node</h3>
        {!nodeDraft ? (
          <p className="muted">Select a node to edit its bindings.</p>
        ) : (
          <>
            <label className="form-field">
              <span>Node id</span>
              <input
                aria-label="Node id"
                value={nodeDraft.id}
                disabled={readOnly}
                onChange={(e) => updateNode({ id: e.target.value })}
              />
            </label>
            <label className="check-row">
              <input
                aria-label="Startable"
                type="checkbox"
                checked={Boolean(nodeDraft.startable)}
                disabled={readOnly}
                onChange={(e) => updateNode({ startable: e.target.checked })}
              />
              <span>Startable</span>
            </label>
            <label className="check-row">
              <input
                aria-label="Terminal"
                type="checkbox"
                checked={Boolean(nodeDraft.terminal)}
                disabled={readOnly}
                onChange={(e) => updateNode({ terminal: e.target.checked })}
              />
              <span>Terminal</span>
            </label>
            <label className="form-field">
              <span>Default workflow</span>
              <select
                aria-label="Default workflow"
                value={nodeDraft.default_workflow_type}
                disabled={readOnly}
                onChange={(e) => updateNode({ default_workflow_type: e.target.value })}
              >
                <option value="">Select workflow</option>
                {!workflowOptions.includes(nodeDraft.default_workflow_type) &&
                  nodeDraft.default_workflow_type && (
                    <option value={nodeDraft.default_workflow_type}>
                      {nodeDraft.default_workflow_type}
                    </option>
                  )}
                {workflowOptions.map((workflowType) => (
                  <option key={workflowType} value={workflowType}>
                    {workflowType}
                  </option>
                ))}
              </select>
            </label>
            {onOpenWorkflow && nodeDraft.default_workflow_type && (
              <button
                type="button"
                className="link-button"
                onClick={() => onOpenWorkflow(nodeDraft.default_workflow_type)}
              >
                Open workflow
              </button>
            )}
            <label className="form-field">
              <span>Completion adapter</span>
              <select
                aria-label="Completion adapter"
                value={nodeDraft.completion_adapter}
                disabled={readOnly}
                onChange={(e) => updateNode({ completion_adapter: e.target.value })}
              >
                <option value="">Select adapter</option>
                {!adapterOptions.includes(nodeDraft.completion_adapter) &&
                  nodeDraft.completion_adapter && (
                    <option value={nodeDraft.completion_adapter}>
                      {nodeDraft.completion_adapter}
                    </option>
                  )}
                {adapterOptions.map((adapter) => (
                  <option key={adapter} value={adapter}>
                    {adapter}
                  </option>
                ))}
              </select>
            </label>
            <label className="form-field">
              <span>Required artifacts</span>
              <input
                aria-label="Required artifacts"
                value={(nodeDraft.required_artifacts || []).join(", ")}
                disabled={readOnly}
                onChange={(e) => updateNode({ required_artifacts: parseCsv(e.target.value) })}
              />
            </label>
            <label className="form-field">
              <span>Produces artifacts</span>
              <input
                aria-label="Produces artifacts"
                value={(nodeDraft.produces_artifacts || []).join(", ")}
                disabled={readOnly}
                onChange={(e) => updateNode({ produces_artifacts: parseCsv(e.target.value) })}
              />
            </label>
            <label className="form-field">
              <span>Complete channel text</span>
              <textarea
                aria-label="Complete channel text"
                rows={3}
                value={nodeDraft.complete_channel_text || ""}
                disabled={readOnly}
                onChange={(e) => updateNode({ complete_channel_text: e.target.value || undefined })}
              />
            </label>
            <label className="form-field">
              <span>Reentry JSON</span>
              <textarea
                aria-label="Reentry JSON"
                rows={4}
                disabled={readOnly}
                value={JSON.stringify(nodeDraft.reentry || {}, null, 2)}
                onChange={(e) => {
                  try {
                    const parsed = JSON.parse(e.target.value || "{}");
                    updateNode({
                      reentry:
                        parsed && Object.keys(parsed).length ? parsed : undefined,
                    });
                  } catch {
                    /* keep typing */
                  }
                }}
              />
              <small className="muted">
                Self-loop only: <code>target_node</code> must be this node. List mode loops over{" "}
                <code>remaining_list_artifact</code>, passing each item as{" "}
                <code>next_item_artifact</code> and recording it in{" "}
                <code>completed_list_artifact</code>. Optional <code>mode</code> (list or
                work_orders) and <code>max_iterations</code> (1-500, default 50 in list mode).
              </small>
            </label>
            <div className="input-map-editor">
              <h4>Workflow variants</h4>
              {(nodeDraft.workflow_variants || []).map((variant, index) => (
                <div className="key-value-row" key={`variant-${index}`}>
                  <input
                    aria-label={`Variant workflow ${index + 1}`}
                    value={variant.workflow_type}
                    disabled={readOnly}
                    onChange={(e) => updateVariant(index, { workflow_type: e.target.value })}
                  />
                  <input
                    aria-label={`Variant when ${index + 1}`}
                    value={variantWhenText(variant.when || {})}
                    disabled={readOnly}
                    onChange={(e) => updateVariant(index, { when: parseVariantWhen(e.target.value) })}
                  />
                </div>
              ))}
              <button
                type="button"
                className="link-button"
                disabled={readOnly}
                onClick={() =>
                  updateNode({
                    workflow_variants: [
                      ...(nodeDraft.workflow_variants || []),
                      { workflow_type: "", when: {} },
                    ],
                  })
                }
              >
                Add variant
              </button>
            </div>
            <div className="input-map-editor">
              <h4>Input bindings</h4>
              {(nodeDraft.input_bindings || []).map((binding, index) => (
                <input
                  key={`binding-${index}`}
                  aria-label={`Input binding ${index + 1}`}
                  value={bindingText(binding)}
                  disabled={readOnly}
                  onChange={(e) => updateBinding(index, e.target.value)}
                />
              ))}
              <button
                type="button"
                className="link-button"
                disabled={readOnly}
                onClick={() =>
                  updateNode({
                    input_bindings: [
                      ...(nodeDraft.input_bindings || []),
                      { kind: "artifact", target: "target", name: "artifact" },
                    ],
                  })
                }
              >
                Add binding
              </button>
            </div>
          </>
        )}
      </section>

      <section>
        <h3>Edge</h3>
        {!edgeDraft ? (
          <p className="muted">Select an edge to edit transition settings.</p>
        ) : (
          <>
            <p className="muted mono">
              {edgeDraft.source} → {edgeDraft.target}
            </p>
            <label className="form-field">
              <span>Transition mode</span>
              <select
                aria-label="Transition mode"
                value={edgeDraft.mode}
                disabled={readOnly}
                onChange={(e) => updateEdge({ mode: e.target.value })}
              >
                {modeOptions.map((mode) => (
                  <option key={mode} value={mode}>
                    {mode}
                  </option>
                ))}
              </select>
            </label>
            <label className="check-row">
              <input
                aria-label="Confirm required"
                type="checkbox"
                checked={Boolean(edgeDraft.confirm_required)}
                disabled={readOnly}
                onChange={(e) => updateEdge({ confirm_required: e.target.checked })}
              />
              <span>Confirm required</span>
            </label>
            <label className="form-field">
              <span>Signals</span>
              <input
                aria-label="Signals"
                value={(edgeDraft.signals || []).join(", ")}
                disabled={readOnly}
                onChange={(e) => updateEdge({ signals: parseCsv(e.target.value) })}
              />
            </label>
            <label className="form-field">
              <span>Artifact patch</span>
              <input
                aria-label="Artifact patch"
                value={variantWhenText(edgeDraft.artifact_patch || {})}
                disabled={readOnly}
                onChange={(e) => updateEdge({ artifact_patch: parseVariantWhen(e.target.value) })}
              />
            </label>
          </>
        )}
      </section>

      <section>
        <h3>Schedule</h3>
        <p className="muted">Start a new run of this program on a cron schedule.</p>
        <p className="muted">
          <a
            className="inspector-settings-link"
            href={`#/system/cron?attachProgram=${encodeURIComponent(programDraft.program_type || "")}`}
          >
            Schedule this program
          </a>
        </p>
      </section>

      <section>
        <h3>Slack start</h3>
        <p className="muted">Managed routing overlay intents for this program.</p>
        <p className="muted">
          <a className="inspector-settings-link" href="#/system/settings/routing">
            Edit in Settings → Routing
          </a>
        </p>
        <p className="muted">
          Use distinct signals from YAML <code>governed_prd</code> (for example{" "}
          <code>create a managed prd</code>).
        </p>
        <label className="form-field">
          <span>Suggested intent JSON</span>
          <textarea
            aria-label="Suggested Slack intent JSON"
            readOnly
            rows={10}
            value={JSON.stringify(
              {
                definition_type: "channel_routing",
                definition_version: "1.0.0",
                intents: [
                  {
                    id: `start_${programDraft.program_type || "program"}`,
                    signals: [`start ${programDraft.program_type || "program"}`],
                    exclude: [],
                    program_type: programDraft.program_type,
                    entry_phase: (programDraft.startable_nodes || [])[0] || "",
                    orchestrator_hint: `Call propose_start_program with program_type=${programDraft.program_type}.`,
                    policy: {
                      mode: "require_workflow",
                      deny_tools: ["propose_delegate_task", "delegate_task"],
                    },
                  },
                ],
              },
              null,
              2
            )}
          />
        </label>
      </section>

      <section>
        <h3>JSON preview</h3>
        <textarea
          aria-label="Current definition JSON"
          readOnly
          value={JSON.stringify(definition, null, 2)}
        />
      </section>
    </aside>
  );
}
