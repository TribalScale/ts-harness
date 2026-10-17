import { useEffect, useState } from "react";
import type { FragmentDefinition, FragmentInput, FragmentParam, WorkflowStage } from "../workflows/types";

function renameKey<T>(record: Record<string, T>, from: string, to: string): Record<string, T> {
  return Object.fromEntries(Object.entries(record).map(([key, value]) => [key === from ? to : key, value]));
}

function nextKey(record: Record<string, unknown>, prefix: string): string {
  let index = Object.keys(record).length + 1;
  while (`${prefix}_${index}` in record) index += 1;
  return `${prefix}_${index}`;
}

export function FragmentInspector({
  definition,
  selectedStage,
  readOnly,
  onChange,
  onStageChange,
}: {
  definition: FragmentDefinition;
  selectedStage?: WorkflowStage;
  readOnly: boolean;
  onChange: (definition: FragmentDefinition) => void;
  onStageChange: (stage: WorkflowStage) => void;
}) {
  const params = definition.params || {};
  const inputs = definition.inputs || {};
  const [stageJson, setStageJson] = useState("");
  const [stageError, setStageError] = useState<string | null>(null);

  const selectedName = selectedStage?.name;
  useEffect(() => {
    setStageJson(selectedStage ? JSON.stringify(selectedStage, null, 2) : "");
    setStageError(null);
    // Reformat only when another stage is selected so typing is not interrupted.
  }, [selectedName]);

  function update(patch: Partial<FragmentDefinition>) {
    onChange({ ...definition, ...patch });
  }

  function updateParam(name: string, patch: Partial<FragmentParam>) {
    update({ params: { ...params, [name]: { ...params[name], ...patch } } });
  }

  function updateInput(name: string, patch: Partial<FragmentInput>) {
    update({ inputs: { ...inputs, [name]: { ...inputs[name], ...patch } } });
  }

  function remove<T>(record: Record<string, T>, name: string): Record<string, T> {
    const next = { ...record };
    delete next[name];
    return next;
  }

  return (
    <aside className="workflow-inspector" aria-label="Fragment inspector">
      <section>
        <h3>Fragment</h3>
        <label className="form-field">
          <span>Fragment id</span>
          <input
            aria-label="Fragment id"
            value={definition.fragment_id}
            disabled={readOnly}
            onChange={(e) => update({ fragment_id: e.target.value })}
          />
        </label>
        <label className="form-field">
          <span>Definition version</span>
          <input
            aria-label="Fragment definition version"
            value={definition.definition_version}
            disabled={readOnly}
            onChange={(e) => update({ definition_version: e.target.value })}
          />
        </label>
        <label className="form-field">
          <span>Display name</span>
          <input
            aria-label="Fragment display name"
            value={definition.display_name || ""}
            disabled={readOnly}
            onChange={(e) => update({ display_name: e.target.value })}
          />
        </label>
        <label className="form-field">
          <span>Description</span>
          <textarea
            aria-label="Fragment description"
            rows={2}
            value={definition.description || ""}
            disabled={readOnly}
            onChange={(e) => update({ description: e.target.value })}
          />
        </label>
      </section>

      <section aria-label="Fragment params">
        <h3>Params</h3>
        <p className="muted">
          Stage references the including workflow fills in. Use <code className="mono">{"{{param.<name>}}"}</code> in stages.
        </p>
        {Object.entries(params).map(([name, spec]) => (
          <div key={name} className="input-map-row">
            <input
              aria-label={`Param name ${name}`}
              value={name}
              disabled={readOnly}
              onChange={(e) => update({ params: renameKey(params, name, e.target.value) })}
            />
            <label className="check-row">
              <input
                aria-label={`Param ${name} required`}
                type="checkbox"
                checked={spec.required !== false}
                disabled={readOnly}
                onChange={(e) => updateParam(name, { required: e.target.checked })}
              />
              <span>Required</span>
            </label>
            <button
              type="button"
              className="link-button"
              disabled={readOnly}
              onClick={() => update({ params: remove(params, name) })}
            >
              Remove param {name}
            </button>
          </div>
        ))}
        <button
          type="button"
          className="link-button"
          disabled={readOnly}
          onClick={() => update({ params: { ...params, [nextKey(params, "stage")]: { kind: "stage_ref", required: true } } })}
        >
          Add param
        </button>
      </section>

      <section aria-label="Fragment inputs">
        <h3>Inputs</h3>
        <p className="muted">
          Values the including workflow maps in. Use <code className="mono">$input.&lt;name&gt;</code> as a whole input_map value.
        </p>
        {Object.entries(inputs).map(([name, spec]) => (
          <div key={name} className="input-map-row">
            <input
              aria-label={`Input name ${name}`}
              value={name}
              disabled={readOnly}
              onChange={(e) => update({ inputs: renameKey(inputs, name, e.target.value) })}
            />
            <label className="check-row">
              <input
                aria-label={`Input ${name} required`}
                type="checkbox"
                checked={spec.required !== false}
                disabled={readOnly}
                onChange={(e) => updateInput(name, { required: e.target.checked })}
              />
              <span>Required</span>
            </label>
            <button
              type="button"
              className="link-button"
              disabled={readOnly}
              onClick={() => update({ inputs: remove(inputs, name) })}
            >
              Remove input {name}
            </button>
          </div>
        ))}
        <button
          type="button"
          className="link-button"
          disabled={readOnly}
          onClick={() => update({ inputs: { ...inputs, [nextKey(inputs, "input")]: { required: true } } })}
        >
          Add input
        </button>
      </section>

      <section>
        <h3>Stage</h3>
        {!selectedStage ? (
          <p className="muted">Select a stage to edit it as JSON.</p>
        ) : (
          <label className="form-field">
            <span>Stage JSON</span>
            <textarea
              aria-label="Fragment stage JSON"
              className="mono"
              rows={12}
              value={stageJson}
              disabled={readOnly}
              onChange={(e) => {
                setStageJson(e.target.value);
                try {
                  const parsed = JSON.parse(e.target.value) as WorkflowStage;
                  if (!parsed || typeof parsed !== "object" || !parsed.name) throw new Error("Stage needs a name");
                  setStageError(null);
                  onStageChange(parsed);
                } catch (err: any) {
                  setStageError(String(err.message || err));
                }
              }}
            />
            {stageError && <small className="error">{stageError}</small>}
          </label>
        )}
      </section>

      <section>
        <h3>JSON preview</h3>
        <textarea aria-label="Current fragment JSON" readOnly value={JSON.stringify(definition, null, 2)} />
      </section>
    </aside>
  );
}
