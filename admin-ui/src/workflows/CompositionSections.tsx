import { useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import { Button } from "../components/Button";
import type { ChildWorkflowOption, FragmentDefinition, FragmentSummary, WorkflowStage } from "./types";

export function FragmentIncludeSection({
  stage,
  stageNames,
  readOnly,
  onChange,
}: {
  stage: WorkflowStage;
  stageNames: string[];
  readOnly: boolean;
  onChange: (patch: Partial<WorkflowStage>) => void;
}) {
  const fragmentId = String(stage.fragment || "");
  const version = String(stage.version || "");
  const [definition, setDefinition] = useState<FragmentDefinition | null>(null);
  const [latest, setLatest] = useState<string | undefined>();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (!fragmentId || !version) {
      setDefinition(null);
      return;
    }
    apiFetch<{ definition: FragmentDefinition }>(
      `/admin/workflow-fragments/${encodeURIComponent(fragmentId)}?version=${encodeURIComponent(version)}`
    )
      .then((data) => {
        if (cancelled) return;
        setDefinition(data.definition);
        setError(null);
      })
      .catch((e: any) => {
        if (cancelled) return;
        setDefinition(null);
        setError(String(e.message || e));
      });
    return () => {
      cancelled = true;
    };
  }, [fragmentId, version]);

  useEffect(() => {
    let cancelled = false;
    apiFetch<{ fragments: FragmentSummary[] }>("/admin/workflow-fragments")
      .then((data) => {
        if (cancelled) return;
        const current = (data.fragments || []).find(
          (item) => item.fragment_id === fragmentId && item.status !== "draft"
        );
        setLatest(current?.definition_version);
      })
      .catch(() => {
        if (!cancelled) setLatest(undefined);
      });
    return () => {
      cancelled = true;
    };
  }, [fragmentId]);

  const versions = Array.from(new Set([version, latest].filter(Boolean))) as string[];
  const params = definition?.params || {};
  const inputs = definition?.inputs || {};

  return (
    <div className="fragment-include-section" aria-label="Fragment include">
      <h4>Fragment</h4>
      <p className="muted">
        {definition?.display_name || fragmentId} · stages expand when the workflow loads
      </p>
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
      <label className="form-field">
        <span>Fragment version</span>
        <select
          aria-label="Fragment version"
          value={version}
          disabled={readOnly}
          onChange={(e) => onChange({ version: e.target.value })}
        >
          {versions.map((item) => (
            <option key={item} value={item}>
              {item}
            </option>
          ))}
        </select>
      </label>
      {latest && latest !== version && (
        <p className="warning" role="status">
          Version {latest} is available. Upgrading changes the expanded stages; validate before activating.
        </p>
      )}
      <label className="form-field">
        <span>Stage name prefix</span>
        <input
          aria-label="Fragment prefix"
          value={String(stage.prefix || "")}
          disabled={readOnly}
          placeholder="Needed when the fragment is included twice"
          onChange={(e) => onChange({ prefix: e.target.value || undefined })}
        />
      </label>
      {Object.entries(params).map(([name, spec]) => (
        <label key={name} className="form-field">
          <span>
            Param {name}
            {spec.required !== false ? " (required)" : ""}
          </span>
          {spec.kind === "stage_ref" || spec.kind === undefined ? (
            <select
              aria-label={`Fragment param ${name}`}
              value={String(stage.params?.[name] || "")}
              disabled={readOnly}
              onChange={(e) => onChange({ params: { ...(stage.params || {}), [name]: e.target.value } })}
            >
              <option value="">Select a stage</option>
              {stageNames.map((stageName) => (
                <option key={stageName} value={stageName}>
                  {stageName}
                </option>
              ))}
            </select>
          ) : (
            <input
              aria-label={`Fragment param ${name}`}
              value={String(stage.params?.[name] || "")}
              disabled={readOnly}
              onChange={(e) => onChange({ params: { ...(stage.params || {}), [name]: e.target.value } })}
            />
          )}
          {spec.description && <small className="muted">{spec.description}</small>}
        </label>
      ))}
      {Object.entries(inputs).map(([name, spec]) => (
        <label key={name} className="form-field">
          <span>
            Input {name}
            {spec.required !== false ? " (required)" : ""}
          </span>
          <input
            aria-label={`Fragment input ${name}`}
            className="mono"
            value={String(stage.inputs?.[name] || "")}
            disabled={readOnly}
            placeholder="$prior.<stage>.output.<field>"
            onChange={(e) => {
              const next = { ...(stage.inputs || {}) };
              if (e.target.value) next[name] = e.target.value;
              else delete next[name];
              onChange({ inputs: next });
            }}
          />
        </label>
      ))}
    </div>
  );
}

export interface ChildJoinState {
  enabled: boolean;
  timeoutSeconds?: number;
}

export function ChildWorkflowSection({
  stage,
  childWorkflows,
  join,
  readOnly,
  onChange,
  onJoinChange,
}: {
  stage: WorkflowStage;
  childWorkflows: ChildWorkflowOption[];
  join: ChildJoinState;
  readOnly: boolean;
  onChange: (patch: Partial<WorkflowStage>) => void;
  onJoinChange?: (join: ChildJoinState) => void;
}) {
  const barrier = (stage.barrier || {}) as {
    hook?: string;
    workflow_type?: string;
    for_each?: string;
    label?: string;
    input_map?: Record<string, string>;
  };
  const inputMap = barrier.input_map || {};
  const child = childWorkflows.find((item) => item.workflow_type === barrier.workflow_type);
  const unmapped = (child?.workflow_input_keys || []).filter((key) => !(key in inputMap));

  function updateBarrier(patch: Partial<typeof barrier>) {
    const next: Record<string, unknown> = { ...barrier, ...patch, hook: "fork_children" };
    Object.keys(next).forEach((key) => {
      if (next[key] === "" || next[key] === undefined) delete next[key];
    });
    onChange({ barrier: next });
  }

  function selectChild(workflowType: string) {
    const option = childWorkflows.find((item) => item.workflow_type === workflowType);
    const prefilled = { ...inputMap };
    (option?.workflow_input_keys || []).forEach((key) => {
      if (!(key in prefilled)) prefilled[key] = "";
    });
    updateBarrier({ workflow_type: workflowType, input_map: prefilled });
  }

  function updateInput(key: string, nextKey: string, value: string) {
    const entries = Object.entries(inputMap).filter(([entryKey]) => entryKey !== key);
    if (nextKey) entries.push([nextKey, value]);
    updateBarrier({ input_map: Object.fromEntries(entries) });
  }

  return (
    <div className="child-workflow-section" aria-label="Child workflows">
      <h4>Child workflows</h4>
      <label className="form-field">
        <span>Child workflow</span>
        <select
          aria-label="Child workflow"
          value={String(barrier.workflow_type || "")}
          disabled={readOnly}
          onChange={(e) => selectChild(e.target.value)}
        >
          <option value="">Select a workflow</option>
          {childWorkflows.map((item) => (
            <option key={item.workflow_type} value={item.workflow_type}>
              {item.workflow_type}
              {item.active_version ? ` (${item.active_version})` : ""}
            </option>
          ))}
        </select>
      </label>
      {barrier.workflow_type && (
        <a className="link-button" href={`#/workflows/${encodeURIComponent(barrier.workflow_type)}`}>
          Open child definition
        </a>
      )}
      <label className="form-field">
        <span>For each (one child per item)</span>
        <input
          aria-label="Fork for_each"
          className="mono"
          value={String(barrier.for_each || "")}
          disabled={readOnly}
          placeholder="$prior.<stage>.output.<list>; empty starts one child"
          onChange={(e) => updateBarrier({ for_each: e.target.value })}
        />
      </label>
      <label className="form-field">
        <span>Child label</span>
        <input
          aria-label="Fork label"
          className="mono"
          value={String(barrier.label || "")}
          disabled={readOnly}
          placeholder="$item.id (default: item index)"
          onChange={(e) => updateBarrier({ label: e.target.value })}
        />
      </label>
      <div className="input-map-editor" aria-label="Child input map">
        <span>Child intake input</span>
        {Object.entries(inputMap).map(([key, value]) => (
          <div key={key} className="input-map-row">
            <input
              aria-label={`Child input key ${key}`}
              value={key}
              disabled={readOnly}
              onChange={(e) => updateInput(key, e.target.value, value)}
            />
            <input
              aria-label={`Child input ${key}`}
              className="mono"
              value={value}
              disabled={readOnly}
              placeholder="$item.<field> or $prior.<stage>.output.<field>"
              onChange={(e) => updateInput(key, key, e.target.value)}
            />
          </div>
        ))}
        <button
          type="button"
          className="link-button"
          disabled={readOnly}
          onClick={() => updateInput("", `input_${Object.keys(inputMap).length + 1}`, "")}
        >
          Add child input
        </button>
        {unmapped.length > 0 && (
          <p className="warning" role="status">
            {barrier.workflow_type} reads {unmapped.map((key) => `$workflow.${key}`).join(", ")}; map them above.
          </p>
        )}
      </div>
      <label className="check-row">
        <input
          aria-label="Wait for children"
          type="checkbox"
          checked={join.enabled}
          disabled={readOnly || !onJoinChange}
          onChange={(e) => onJoinChange?.({ ...join, enabled: e.target.checked })}
        />
        <span>Wait for children (adds a join stage)</span>
      </label>
      {join.enabled && (
        <label className="form-field">
          <span>Join timeout seconds</span>
          <input
            aria-label="Join timeout seconds"
            type="number"
            value={join.timeoutSeconds ?? ""}
            disabled={readOnly || !onJoinChange}
            onChange={(e) =>
              onJoinChange?.({
                enabled: true,
                timeoutSeconds: e.target.value === "" ? undefined : Number(e.target.value),
              })
            }
          />
        </label>
      )}
      <Button variant="ghost" disabled={readOnly} onClick={() => onChange({ barrier: undefined })}>
        Remove child workflows
      </Button>
    </div>
  );
}
