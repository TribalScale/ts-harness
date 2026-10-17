import { useEffect, useState } from "react";
import { ChildWorkflowSection, FragmentIncludeSection, type ChildJoinState } from "./CompositionSections";
import { findChildJoin, isFragmentStage } from "./graph";
import type {
  WorkflowDefinition,
  WorkflowDefinitionCatalog,
  WorkflowHook,
  WorkflowStage,
} from "./types";

const PRESENT_PROMPTS = ["approval", "inform", "intake", "merge", "none"] as const;
const NOTIFICATION_EVENTS = [
  "completed",
  "blocked",
  "failed",
  "awaiting_approval",
  "awaiting_external",
] as const;
// Mirrors STAGE_ALWAYS_ALLOWED_TOOLS in runtime tools/policy.py.
const STAGE_ALWAYS_ALLOWED_TOOLS = new Set(["read_skill_reference"]);

function hookName(hook?: WorkflowHook): string {
  return String(hook?.hook || "");
}

function withHookName(existing: WorkflowHook | undefined, name: string): WorkflowHook | undefined {
  if (!name) return undefined;
  return { ...(existing || {}), hook: name };
}

function csv(values?: string[]): string {
  return (values || []).join(", ");
}

function parseCsv(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function stageKindLabel(stage?: WorkflowStage): string {
  if (!stage) return "task";
  if (isFragmentStage(stage)) return "fragment";
  if (stage.wait_external) return "wait";
  if (stage.barrier) return "barrier";
  if (stage.approval_required) return "gate";
  return String(stage.kind || "task");
}

export function WorkflowInspector({
  definition,
  selectedStage,
  catalog,
  readOnly,
  onWorkflowChange,
  onStageChange,
  onChildJoinChange,
}: {
  definition: WorkflowDefinition;
  selectedStage?: WorkflowStage;
  catalog: WorkflowDefinitionCatalog;
  readOnly: boolean;
  onWorkflowChange: (definition: WorkflowDefinition) => void;
  onStageChange: (stage: WorkflowStage) => void;
  /** Adds, removes or retimes the join stage that waits on a fork stage's children. */
  onChildJoinChange?: (forkStage: string, join: ChildJoinState) => void;
}) {
  const [workflowDraft, setWorkflowDraft] = useState(definition);
  const [stageDraft, setStageDraft] = useState<WorkflowStage | undefined>(selectedStage);

  useEffect(() => setWorkflowDraft(definition), [definition]);
  useEffect(() => setStageDraft(selectedStage), [selectedStage]);

  function updateWorkflow(patch: Partial<WorkflowDefinition>) {
    const next = { ...workflowDraft, ...patch };
    setWorkflowDraft(next);
    onWorkflowChange(next);
  }

  function updateStage(patch: Partial<WorkflowStage>) {
    if (!stageDraft) return;
    const next = { ...stageDraft, ...patch };
    setStageDraft(next);
    onStageChange(next);
  }

  function agentTools(agentId: string | undefined): string[] {
    const tools = (agentId && catalog.agent_tools?.[agentId]) || [];
    return tools.filter((name) => !STAGE_ALWAYS_ALLOWED_TOOLS.has(name));
  }

  function updateAgent(agentId: string) {
    if (!stageDraft) return;
    if (!stageDraft.tool_allow) {
      updateStage({ agent_id: agentId });
      return;
    }
    const granted = new Set(agentTools(agentId));
    updateStage({ agent_id: agentId, tool_allow: stageDraft.tool_allow.filter((name) => granted.has(name)) });
  }

  function setRestrictTools(enabled: boolean) {
    if (!stageDraft) return;
    if (enabled) {
      updateStage({ tool_allow: [] });
      return;
    }
    // graphToDefinition clones node data, so an undefined value would still serialize as a key.
    const { tool_allow: _removed, ...next } = stageDraft;
    setStageDraft(next);
    onStageChange(next);
  }

  function toggleStageTool(name: string, enabled: boolean) {
    const current = stageDraft?.tool_allow || [];
    updateStage({
      tool_allow: enabled ? [...current.filter((item) => item !== name), name] : current.filter((item) => item !== name),
    });
  }

  function updateRetry(key: "max_retries" | "timeout_seconds", value: string) {
    updateStage({ retry: { ...(stageDraft?.retry || {}), [key]: value === "" ? undefined : Number(value) } });
  }

  function updateInputMap(key: string, nextKey: string, nextValue: string) {
    const current = stageDraft?.input_map || {};
    const entries = Object.entries(current).filter(([entryKey]) => entryKey !== key);
    if (nextKey) entries.push([nextKey, nextValue]);
    updateStage({ input_map: Object.fromEntries(entries) });
  }

  function updateHookField(
    field: "on_complete" | "on_reject" | "on_block" | "on_failure",
    patch: Partial<WorkflowHook> | undefined
  ) {
    if (!patch) {
      updateStage({ [field]: undefined });
      return;
    }
    const next = { ...(stageDraft?.[field] || {}), ...patch };
    if (!next.hook) {
      updateStage({ [field]: undefined });
      return;
    }
    updateStage({ [field]: next });
  }

  const hookOptions = ["", ...(catalog.hooks || [])];
  const presentPrompts = catalog.present_prompts?.length ? catalog.present_prompts : [...PRESENT_PROMPTS];
  const notificationEvents = catalog.notification_events?.length
    ? catalog.notification_events
    : [...NOTIFICATION_EVENTS];
  const validators = catalog.semantic_validators || [];
  const contractNames = Object.keys(workflowDraft.contracts || {});
  const stageNames = (workflowDraft.stages || []).map((stage) => stage.name);

  function childJoinState(forkStage: string): ChildJoinState {
    const join = findChildJoin(definition.stages || [], forkStage);
    if (!join) return { enabled: false };
    const timeout = join.wait_external?.timeout_seconds;
    return { enabled: true, timeoutSeconds: timeout === undefined ? undefined : Number(timeout) };
  }

  return (
    <aside className="workflow-inspector" aria-label="Workflow inspector">
      <section>
        <h3>Workflow</h3>
        <label className="form-field">
          <span>Workflow type</span>
          <input
            aria-label="Workflow type"
            value={workflowDraft.workflow_type}
            disabled={readOnly}
            onChange={(e) => updateWorkflow({ workflow_type: e.target.value })}
          />
        </label>
        <label className="form-field">
          <span>Display name</span>
          <input
            aria-label="Display name"
            value={workflowDraft.display_name || ""}
            disabled={readOnly}
            onChange={(e) => updateWorkflow({ display_name: e.target.value })}
          />
        </label>
        <label className="form-field">
          <span>Definition version</span>
          <input
            aria-label="Definition version"
            value={workflowDraft.definition_version}
            disabled={readOnly}
            onChange={(e) => updateWorkflow({ definition_version: e.target.value })}
          />
        </label>
        <label className="check-row">
          <input
            aria-label="Notifications enabled"
            type="checkbox"
            checked={Boolean(workflowDraft.notifications?.bound_channel)}
            disabled={readOnly}
            onChange={(e) =>
              updateWorkflow({
                notifications: {
                  ...(workflowDraft.notifications || {}),
                  bound_channel: e.target.checked,
                },
              })
            }
          />
          <span>Bound-channel notifications</span>
        </label>
        <label className="form-field">
          <span>Notification events (comma-separated)</span>
          <input
            aria-label="Notification events"
            value={csv(workflowDraft.notifications?.events as string[] | undefined)}
            disabled={readOnly}
            placeholder={notificationEvents.join(", ")}
            onChange={(e) =>
              updateWorkflow({
                notifications: {
                  ...(workflowDraft.notifications || {}),
                  events: parseCsv(e.target.value),
                },
              })
            }
          />
        </label>
        <label className="form-field">
          <span>Intake stage</span>
          <input
            aria-label="Intake stage"
            value={String(workflowDraft.promotion?.intake_stage || "")}
            disabled={readOnly}
            onChange={(e) =>
              updateWorkflow({
                promotion: { ...(workflowDraft.promotion || {}), intake_stage: e.target.value },
              })
            }
          />
        </label>
        <label className="check-row">
          <input
            aria-label="Promotion confirm required"
            type="checkbox"
            checked={Boolean(workflowDraft.promotion?.confirm_required)}
            disabled={readOnly}
            onChange={(e) =>
              updateWorkflow({
                promotion: {
                  ...(workflowDraft.promotion || {}),
                  confirm_required: e.target.checked,
                },
              })
            }
          />
          <span>Promotion confirm required</span>
        </label>
        <label className="form-field">
          <span>Binding format</span>
          <input
            aria-label="Binding format"
            value={String(workflowDraft.promotion?.binding?.format || "")}
            disabled={readOnly}
            placeholder="{channel}:{channel_id}:{thread_id}"
            onChange={(e) =>
              updateWorkflow({
                promotion: {
                  ...(workflowDraft.promotion || {}),
                  binding: {
                    ...(workflowDraft.promotion?.binding || {}),
                    format: e.target.value,
                  },
                },
              })
            }
          />
        </label>
        <label className="form-field">
          <span>on_workflow_complete</span>
          <select
            aria-label="on_workflow_complete"
            value={hookName(workflowDraft.on_workflow_complete)}
            disabled={readOnly}
            onChange={(e) =>
              updateWorkflow({
                on_workflow_complete: withHookName(workflowDraft.on_workflow_complete, e.target.value),
              })
            }
          >
            {hookOptions.map((hook) => (
              <option key={hook || "none"} value={hook}>
                {hook || "None"}
              </option>
            ))}
          </select>
        </label>
        <label className="form-field">
          <span>on_workflow_cancel</span>
          <select
            aria-label="on_workflow_cancel"
            value={hookName(workflowDraft.on_workflow_cancel)}
            disabled={readOnly}
            onChange={(e) =>
              updateWorkflow({
                on_workflow_cancel: withHookName(workflowDraft.on_workflow_cancel, e.target.value),
              })
            }
          >
            {hookOptions.map((hook) => (
              <option key={hook || "none"} value={hook}>
                {hook || "None"}
              </option>
            ))}
          </select>
        </label>
      </section>

      <section>
        <h3>Session</h3>
        <label className="form-field">
          <span>Orchestrator agent</span>
          <input
            aria-label="Orchestrator agent"
            value={String(workflowDraft.session?.orchestrator_agent_id || "")}
            disabled={readOnly}
            onChange={(e) =>
              updateWorkflow({
                session: {
                  ...(workflowDraft.session || {}),
                  orchestrator_agent_id: e.target.value,
                },
              })
            }
          />
        </label>
        <label className="form-field">
          <span>Artifact steps (comma-separated)</span>
          <input
            aria-label="Artifact steps"
            value={csv(workflowDraft.session?.artifact_steps)}
            disabled={readOnly}
            onChange={(e) =>
              updateWorkflow({
                session: {
                  ...(workflowDraft.session || {}),
                  artifact_steps: parseCsv(e.target.value),
                },
              })
            }
          />
        </label>
        <label className="form-field">
          <span>Phase tracking JSON</span>
          <textarea
            aria-label="Phase tracking JSON"
            rows={4}
            disabled={readOnly}
            value={JSON.stringify(workflowDraft.session?.phase_tracking || {}, null, 2)}
            onChange={(e) => {
              try {
                const parsed = JSON.parse(e.target.value || "{}");
                updateWorkflow({
                  session: { ...(workflowDraft.session || {}), phase_tracking: parsed },
                });
              } catch {
                /* keep typing */
              }
            }}
          />
        </label>
        <label className="form-field">
          <span>Artifact step prompts JSON</span>
          <textarea
            aria-label="Artifact step prompts JSON"
            rows={3}
            disabled={readOnly}
            value={JSON.stringify(workflowDraft.session?.artifact_step_prompts || {}, null, 2)}
            onChange={(e) => {
              try {
                const parsed = JSON.parse(e.target.value || "{}");
                updateWorkflow({
                  session: {
                    ...(workflowDraft.session || {}),
                    artifact_step_prompts: parsed,
                  },
                });
              } catch {
                /* keep typing */
              }
            }}
          />
        </label>
        <label className="form-field">
          <span>on_gate_complete_hook</span>
          <select
            aria-label="on_gate_complete_hook"
            value={String(workflowDraft.session?.on_gate_complete_hook || "")}
            disabled={readOnly}
            onChange={(e) =>
              updateWorkflow({
                session: {
                  ...(workflowDraft.session || {}),
                  on_gate_complete_hook: e.target.value || undefined,
                },
              })
            }
          >
            {hookOptions.map((hook) => (
              <option key={hook || "none"} value={hook}>
                {hook || "None"}
              </option>
            ))}
          </select>
        </label>
      </section>

      <section>
        <h3>Program block</h3>
        <label className="form-field">
          <span>Phase</span>
          <input
            aria-label="Program phase"
            value={String(workflowDraft.program?.phase || "")}
            disabled={readOnly}
            onChange={(e) =>
              updateWorkflow({
                program: { ...(workflowDraft.program || {}), phase: e.target.value },
              })
            }
          />
        </label>
        <label className="check-row">
          <input
            aria-label="Program terminal"
            type="checkbox"
            checked={Boolean(workflowDraft.program?.terminal)}
            disabled={readOnly}
            onChange={(e) =>
              updateWorkflow({
                program: { ...(workflowDraft.program || {}), terminal: e.target.checked },
              })
            }
          />
          <span>Terminal phase</span>
        </label>
        <label className="form-field">
          <span>Required artifacts (comma-separated)</span>
          <input
            aria-label="Program required artifacts"
            value={csv(workflowDraft.program?.required_artifacts)}
            disabled={readOnly}
            onChange={(e) =>
              updateWorkflow({
                program: {
                  ...(workflowDraft.program || {}),
                  required_artifacts: parseCsv(e.target.value),
                },
              })
            }
          />
        </label>
      </section>

      <section>
        <h3>Contracts</h3>
        <label className="form-field">
          <span>Contracts JSON</span>
          <textarea
            aria-label="Contracts JSON"
            rows={8}
            disabled={readOnly}
            value={JSON.stringify(workflowDraft.contracts || {}, null, 2)}
            onChange={(e) => {
              try {
                const parsed = JSON.parse(e.target.value || "{}");
                updateWorkflow({ contracts: parsed });
              } catch {
                /* keep typing */
              }
            }}
          />
        </label>
        {validators.length > 0 && (
          <p className="muted">Known semantic validators: {validators.join(", ")}</p>
        )}
      </section>

      <section>
        <h3>Stage</h3>
        {!stageDraft ? (
          <p className="muted">Select a stage to edit its configuration.</p>
        ) : isFragmentStage(stageDraft) ? (
          <>
            <p className="muted">Node kind: fragment</p>
            <label className="form-field">
              <span>Name</span>
              <input
                aria-label="Stage name"
                value={stageDraft.name}
                disabled={readOnly}
                onChange={(e) => updateStage({ name: e.target.value })}
              />
            </label>
            <FragmentIncludeSection
              stage={stageDraft}
              stageNames={stageNames.filter((name) => name !== stageDraft.name)}
              readOnly={readOnly}
              onChange={updateStage}
            />
          </>
        ) : (
          <>
            <p className="muted">Node kind: {stageKindLabel(stageDraft)}</p>
            <label className="form-field">
              <span>Name</span>
              <input
                aria-label="Stage name"
                value={stageDraft.name}
                disabled={readOnly}
                onChange={(e) => updateStage({ name: e.target.value })}
              />
            </label>
            <label className="form-field">
              <span>Kind</span>
              <select
                aria-label="Stage kind"
                value={String(stageDraft.kind || "task")}
                disabled={readOnly}
                onChange={(e) => updateStage({ kind: e.target.value })}
              >
                {(catalog.stage_kinds?.length ? catalog.stage_kinds : ["task", "expand", "compensate"]).map(
                  (kind) => (
                    <option key={kind} value={kind}>
                      {kind}
                    </option>
                  )
                )}
              </select>
            </label>
            <label className="form-field">
              <span>Agent</span>
              <select
                aria-label="Agent"
                value={stageDraft.agent_id || ""}
                disabled={readOnly}
                onChange={(e) => updateAgent(e.target.value)}
              >
                <option value="">Select agent</option>
                {catalog.agents.map((agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agent.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="form-field">
              <span>Allowed agents</span>
              <select
                aria-label="Allowed agents"
                multiple
                disabled={readOnly}
                value={stageDraft.allowed_agents || []}
                onChange={(e) =>
                  updateStage({
                    allowed_agents: Array.from(e.target.selectedOptions).map((option) => option.value),
                  })
                }
              >
                {catalog.agents.map((agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agent.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="form-field">
              <span>Skill hints</span>
              <select
                aria-label="Skill hints"
                multiple
                disabled={readOnly}
                value={stageDraft.skill_hints || []}
                onChange={(e) =>
                  updateStage({
                    skill_hints: Array.from(e.target.selectedOptions).map((option) => option.value),
                  })
                }
              >
                {catalog.skills.map((skill) => (
                  <option key={skill.id} value={skill.id}>
                    {skill.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="check-row">
              <input
                aria-label="Restrict tools"
                type="checkbox"
                checked={stageDraft.tool_allow !== undefined}
                disabled={readOnly}
                onChange={(e) => setRestrictTools(e.target.checked)}
              />
              <span>Restrict tools</span>
            </label>
            {stageDraft.tool_allow !== undefined && (
              <div className="form-field">
                <span>Stage tools</span>
                {agentTools(stageDraft.agent_id).length === 0 ? (
                  <p className="muted">The selected agent has no tools to choose from.</p>
                ) : (
                  agentTools(stageDraft.agent_id).map((name) => (
                    <label key={name} className="check-row">
                      <input
                        aria-label={`Stage tool ${name}`}
                        type="checkbox"
                        checked={(stageDraft.tool_allow || []).includes(name)}
                        disabled={readOnly}
                        onChange={(e) => toggleStageTool(name, e.target.checked)}
                      />
                      <span>{name}</span>
                    </label>
                  ))
                )}
                <p className="muted">
                  The stage can only narrow the agent's tools, never add ones the agent lacks. Agent deny and
                  approval rules still apply. An empty restriction fails Validate. read_skill_reference is
                  always available, so the agent can read its assigned skills' files.
                </p>
              </div>
            )}
            <label className="check-row">
              <input
                aria-label="Approval required"
                type="checkbox"
                checked={Boolean(stageDraft.approval_required)}
                disabled={readOnly}
                onChange={(e) => updateStage({ approval_required: e.target.checked })}
              />
              <span>Approval</span>
            </label>
            <label className="check-row">
              <input
                aria-label="Plan approval required"
                type="checkbox"
                checked={Boolean(stageDraft.plan_approval_required)}
                disabled={readOnly}
                onChange={(e) => updateStage({ plan_approval_required: e.target.checked })}
              />
              <span>Plan approval</span>
            </label>
            <label className="check-row">
              <input
                aria-label="Present output"
                type="checkbox"
                checked={Boolean(stageDraft.present_output)}
                disabled={readOnly}
                onChange={(e) => updateStage({ present_output: e.target.checked })}
              />
              <span>Present output</span>
            </label>
            <label className="form-field">
              <span>Present prompt</span>
              <select
                aria-label="Present prompt"
                value={stageDraft.present_prompt || ""}
                disabled={readOnly}
                onChange={(e) => updateStage({ present_prompt: e.target.value || undefined })}
              >
                <option value="">Default</option>
                {presentPrompts.map((prompt) => (
                  <option key={prompt} value={prompt}>
                    {prompt}
                  </option>
                ))}
              </select>
            </label>
            <div className="form-grid-two">
              <label className="form-field">
                <span>Retry count</span>
                <input
                  aria-label="Retry count"
                  type="number"
                  value={stageDraft.retry?.max_retries ?? ""}
                  disabled={readOnly}
                  onChange={(e) => updateRetry("max_retries", e.target.value)}
                />
              </label>
              <label className="form-field">
                <span>Timeout seconds</span>
                <input
                  aria-label="Timeout seconds"
                  type="number"
                  value={stageDraft.retry?.timeout_seconds ?? ""}
                  disabled={readOnly}
                  onChange={(e) => updateRetry("timeout_seconds", e.target.value)}
                />
              </label>
            </div>
            <label className="form-field">
              <span>Contract ref</span>
              <input
                list="contract-ref-options"
                aria-label="Contract ref"
                value={stageDraft.contract_ref || ""}
                disabled={readOnly}
                onChange={(e) => updateStage({ contract_ref: e.target.value })}
              />
              <datalist id="contract-ref-options">
                {contractNames.map((name) => (
                  <option key={name} value={name} />
                ))}
              </datalist>
            </label>
            <label className="form-field">
              <span>Required artifacts (comma-separated)</span>
              <input
                aria-label="Stage required artifacts"
                value={csv(stageDraft.required_artifacts)}
                disabled={readOnly}
                onChange={(e) => updateStage({ required_artifacts: parseCsv(e.target.value) })}
              />
            </label>
            <label className="form-field">
              <span>Allowed contracts (comma-separated)</span>
              <input
                aria-label="Allowed contracts"
                value={csv(stageDraft.allowed_contracts)}
                disabled={readOnly}
                onChange={(e) => updateStage({ allowed_contracts: parseCsv(e.target.value) })}
              />
            </label>
            <label className="form-field">
              <span>Allowed external effects (comma-separated)</span>
              <input
                aria-label="Allowed external effects"
                value={csv(stageDraft.allowed_external_effects)}
                disabled={readOnly}
                onChange={(e) =>
                  updateStage({ allowed_external_effects: parseCsv(e.target.value) })
                }
              />
            </label>

            <div className="input-map-editor">
              <h4>Input map</h4>
              {Object.entries(stageDraft.input_map || {}).map(([key, value]) => (
                <div className="key-value-row" key={key}>
                  <input
                    aria-label={`Input key ${key}`}
                    value={key}
                    disabled={readOnly}
                    onChange={(e) => updateInputMap(key, e.target.value, value)}
                  />
                  <input
                    aria-label={`Input value ${key}`}
                    value={value}
                    disabled={readOnly}
                    onChange={(e) => updateInputMap(key, key, e.target.value)}
                  />
                </div>
              ))}
              <button
                type="button"
                className="link-button"
                disabled={readOnly}
                onClick={() =>
                  updateInputMap("", `input_${Object.keys(stageDraft.input_map || {}).length + 1}`, "")
                }
              >
                Add input row
              </button>
            </div>

            <h4>Wait external</h4>
            <label className="form-field">
              <span>Event type</span>
              <input
                aria-label="Wait event type"
                value={String(stageDraft.wait_external?.event_type || "")}
                disabled={readOnly}
                onChange={(e) => {
                  const eventType = e.target.value.trim();
                  if (!eventType) {
                    updateStage({ wait_external: undefined });
                    return;
                  }
                  updateStage({
                    wait_external: {
                      ...(stageDraft.wait_external || {}),
                      hook: "wait_external",
                      event_type: eventType,
                    },
                  });
                }}
              />
            </label>
            <label className="form-field">
              <span>Timeout seconds</span>
              <input
                aria-label="Wait timeout seconds"
                type="number"
                value={
                  stageDraft.wait_external?.timeout_seconds === undefined
                    ? ""
                    : Number(stageDraft.wait_external.timeout_seconds)
                }
                disabled={readOnly || !stageDraft.wait_external}
                onChange={(e) =>
                  updateStage({
                    wait_external: {
                      ...(stageDraft.wait_external || {}),
                      timeout_seconds: e.target.value === "" ? undefined : Number(e.target.value),
                    },
                  })
                }
              />
            </label>
            <label className="form-field">
              <span>match_all JSON</span>
              <textarea
                aria-label="Wait match_all JSON"
                rows={3}
                disabled={readOnly || !stageDraft.wait_external}
                value={JSON.stringify(stageDraft.wait_external?.match_all || {}, null, 2)}
                onChange={(e) => {
                  try {
                    const parsed = JSON.parse(e.target.value || "{}");
                    updateStage({
                      wait_external: {
                        ...(stageDraft.wait_external || {}),
                        match_all: parsed,
                      },
                    });
                  } catch {
                    /* keep typing */
                  }
                }}
              />
            </label>

            <h4>Barrier / join</h4>
            {stageDraft.barrier?.hook === "fork_children" ? (
              <ChildWorkflowSection
                stage={stageDraft}
                childWorkflows={catalog.child_workflows || []}
                join={childJoinState(stageDraft.name)}
                readOnly={readOnly}
                onChange={updateStage}
                onJoinChange={
                  onChildJoinChange ? (join) => onChildJoinChange(stageDraft.name, join) : undefined
                }
              />
            ) : (
              <label className="form-field">
                <span>Barrier hook</span>
                <input
                  aria-label="Barrier hook"
                  value={String(stageDraft.barrier?.hook || "")}
                  disabled={readOnly}
                  onChange={(e) => {
                    const hook = e.target.value.trim();
                    updateStage({ barrier: hook ? { hook } : undefined });
                  }}
                />
              </label>
            )}
            <label className="form-field">
              <span>Join hook</span>
              <input
                aria-label="Join hook"
                value={String(stageDraft.join?.hook || "")}
                disabled={readOnly}
                onChange={(e) => {
                  const hook = e.target.value.trim();
                  updateStage({ join: hook ? { hook } : undefined });
                }}
              />
            </label>

            {(["on_complete", "on_reject", "on_block", "on_failure"] as const).map((field) => {
              const config = stageDraft[field];
              const name = hookName(config);
              return (
                <div key={field} className="hook-config-block">
                  <label className="form-field">
                    <span>{field}</span>
                    <select
                      aria-label={field}
                      value={name}
                      disabled={readOnly}
                      onChange={(e) => updateHookField(field, withHookName(config, e.target.value))}
                    >
                      {hookOptions.map((hook) => (
                        <option key={hook || "none"} value={hook}>
                          {hook || "None"}
                        </option>
                      ))}
                    </select>
                  </label>
                  {name === "hydrate_input" && (
                    <label className="form-field">
                      <span>downstream (comma-separated)</span>
                      <input
                        aria-label={`${field} downstream`}
                        value={csv(config?.downstream)}
                        disabled={readOnly}
                        list={`${field}-downstream-options`}
                        onChange={(e) =>
                          updateHookField(field, {
                            ...(config || {}),
                            hook: "hydrate_input",
                            downstream: parseCsv(e.target.value),
                          })
                        }
                      />
                      <datalist id={`${field}-downstream-options`}>
                        {stageNames.map((stageName) => (
                          <option key={stageName} value={stageName} />
                        ))}
                      </datalist>
                    </label>
                  )}
                  {name === "compensate_on_failure" && (
                    <label className="form-field">
                      <span>compensate_stage</span>
                      <input
                        aria-label={`${field} compensate_stage`}
                        value={String(config?.compensate_stage || "")}
                        disabled={readOnly}
                        onChange={(e) =>
                          updateHookField(field, {
                            ...(config || {}),
                            hook: "compensate_on_failure",
                            compensate_stage: e.target.value,
                          })
                        }
                      />
                    </label>
                  )}
                  {name === "revise_on_reject" && (
                    <>
                      <label className="form-field">
                        <span>max_revisions</span>
                        <input
                          aria-label={`${field} max_revisions`}
                          type="number"
                          value={config?.max_revisions ?? ""}
                          disabled={readOnly}
                          onChange={(e) =>
                            updateHookField(field, {
                              ...(config || {}),
                              hook: "revise_on_reject",
                              max_revisions:
                                e.target.value === "" ? undefined : Number(e.target.value),
                            })
                          }
                        />
                      </label>
                      <label className="form-field">
                        <span>revise JSON</span>
                        <textarea
                          aria-label={`${field} revise JSON`}
                          rows={4}
                          disabled={readOnly}
                          value={JSON.stringify(config?.revise || {}, null, 2)}
                          onChange={(e) => {
                            try {
                              const parsed = JSON.parse(e.target.value || "{}");
                              updateHookField(field, {
                                ...(config || {}),
                                hook: "revise_on_reject",
                                revise: parsed,
                              });
                            } catch {
                              /* keep typing */
                            }
                          }}
                        />
                      </label>
                      <label className="form-field">
                        <span>re_gate JSON</span>
                        <textarea
                          aria-label={`${field} re_gate JSON`}
                          rows={3}
                          disabled={readOnly}
                          value={JSON.stringify(config?.re_gate || {}, null, 2)}
                          onChange={(e) => {
                            try {
                              const parsed = JSON.parse(e.target.value || "{}");
                              updateHookField(field, {
                                ...(config || {}),
                                hook: "revise_on_reject",
                                re_gate: parsed,
                              });
                            } catch {
                              /* keep typing */
                            }
                          }}
                        />
                      </label>
                      <label className="form-field">
                        <span>resume_after JSON</span>
                        <textarea
                          aria-label={`${field} resume_after JSON`}
                          rows={3}
                          disabled={readOnly}
                          value={JSON.stringify(config?.resume_after || {}, null, 2)}
                          onChange={(e) => {
                            try {
                              const parsed = JSON.parse(e.target.value || "{}");
                              updateHookField(field, {
                                ...(config || {}),
                                hook: "revise_on_reject",
                                resume_after: parsed,
                              });
                            } catch {
                              /* keep typing */
                            }
                          }}
                        />
                      </label>
                    </>
                  )}
                </div>
              );
            })}
          </>
        )}
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
