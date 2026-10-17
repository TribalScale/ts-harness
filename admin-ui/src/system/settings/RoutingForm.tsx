import { useEffect, useRef, useState } from "react";
import { TextInput } from "@astryxdesign/core/TextInput";
import { apiFetch } from "../../api/client";
import { Button } from "../../components/Button";
import type { RoutingDraft, RoutingGet, RoutingIntent } from "./types";

function overlayIds(routing: RoutingGet, draft: RoutingDraft): Set<string> {
  return new Set((draft.definition.intents || routing.definition.intents || []).map((item) => item.id));
}

function yamlIds(routing: RoutingGet): Set<string> {
  return new Set((routing.yaml_intents || []).map((item) => item.id));
}

function optionsWithCurrent(options: string[], current?: string): string[] {
  if (current && !options.includes(current)) return [current, ...options];
  return options;
}

export function RoutingForm({
  routing,
  draft,
  onChange,
}: {
  routing: RoutingGet;
  draft: RoutingDraft;
  onChange: (next: RoutingDraft) => void;
}) {
  const overlay = overlayIds(routing, draft);
  const inherited = yamlIds(routing);
  const [selectedId, setSelectedId] = useState<string>(
    (routing.merged_intents[0] || draft.definition.intents[0])?.id || ""
  );
  const [newId, setNewId] = useState("");
  const [programs, setPrograms] = useState<string[]>([]);
  const [workflows, setWorkflows] = useState<string[]>([]);
  const [agents, setAgents] = useState<Array<{ id: string; label: string }>>([]);
  const revisionRef = useRef(draft.revision);
  const [lockedIds, setLockedIds] = useState(() => {
    const ids = new Set((routing.definition.intents || []).map((item) => item.id));
    for (const item of routing.draft?.definition.intents || []) ids.add(item.id);
    return ids;
  });

  useEffect(() => {
    void Promise.all([
      apiFetch<{ definitions?: Array<{ program_type: string }> }>("/admin/program-definitions"),
      apiFetch<{
        workflows?: Array<{ workflow_type: string }>;
        templates?: Array<{ program_type: string }>;
      }>("/admin/program-definitions/catalog"),
      apiFetch<{
        agents?: Array<{ id: string; label: string }>;
        templates?: Array<{ workflow_type: string }>;
      }>("/admin/workflow-definitions/catalog"),
    ])
      .then(([defs, programCatalog, workflowCatalog]) => {
        setPrograms(
          [
            ...(defs.definitions || []).map((item) => item.program_type),
            ...(programCatalog.templates || []).map((item) => item.program_type),
          ].filter((value, index, all) => value && all.indexOf(value) === index)
        );
        setWorkflows(
          [
            ...(programCatalog.workflows || []).map((item) => item.workflow_type),
            ...(workflowCatalog.templates || []).map((item) => item.workflow_type),
          ].filter((value, index, all) => value && all.indexOf(value) === index)
        );
        setAgents(workflowCatalog.agents || []);
      })
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    if (revisionRef.current === draft.revision) return;
    revisionRef.current = draft.revision;
    setLockedIds((prev) => {
      const next = new Set(prev);
      for (const item of draft.definition.intents || []) next.add(item.id);
      return next;
    });
  }, [draft.revision, draft.definition.intents]);

  const merged = routing.merged_intents.map((intent) => {
    const overlayIntent = (draft.definition.intents || []).find((item) => item.id === intent.id);
    return overlayIntent || intent;
  });
  const extra = (draft.definition.intents || []).filter(
    (item) => !routing.merged_intents.some((intent) => intent.id === item.id)
  );
  const rows = [...merged, ...extra];
  const selected = rows.find((item) => item.id === selectedId);

  function writeIntents(intents: RoutingIntent[]) {
    onChange({
      ...draft,
      definition: {
        ...draft.definition,
        intents,
      },
    });
  }

  function upsert(intent: RoutingIntent) {
    const intents = [...(draft.definition.intents || [])];
    const index = intents.findIndex((item) => item.id === intent.id);
    if (index >= 0) intents[index] = intent;
    else intents.push(intent);
    writeIntents(intents);
  }

  function sourceOf(id: string): "Inherited" | "Overlay" {
    if (overlay.has(id) && (draft.definition.intents || []).some((item) => item.id === id)) {
      return "Overlay";
    }
    if (inherited.has(id) && !(draft.definition.intents || []).some((item) => item.id === id)) {
      return "Inherited";
    }
    return overlay.has(id) || (draft.definition.intents || []).some((item) => item.id === id)
      ? "Overlay"
      : "Inherited";
  }

  return (
    <div>
      <h3>Routing</h3>
      <section>
      <div className="form-pair-wide">
        <div className="form-field">
          <TextInput
            label="New intent id"
            value={newId}
            onChange={(value) => setNewId(value)}
          />
        </div>
        <Button
          variant="secondary"
          onClick={() => {
            const id = newId.trim() || `intent_${rows.length + 1}`;
            if (lockedIds.has(id) || rows.some((item) => item.id === id)) return;
            const intent: RoutingIntent = {
              id,
              signals: ["start program"],
              program_type: "software_delivery",
            };
            upsert(intent);
            setSelectedId(id);
            setNewId("");
          }}
        >
          Add intent
        </Button>
      </div>
      <table>
        <thead>
          <tr>
            <th>Id</th>
            <th>Target</th>
            <th>Signals</th>
            <th>Source</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((intent) => {
            const source = sourceOf(intent.id);
            const target =
              intent.program_type || intent.workflow_type || intent.delegate_agent || "";
            return (
              <tr key={intent.id}>
                <td>
                  <button type="button" onClick={() => setSelectedId(intent.id)}>
                    {intent.id}
                  </button>
                </td>
                <td>{target}</td>
                <td>{(intent.signals || []).join(", ")}</td>
                <td>{source}</td>
                <td>
                  {source === "Overlay" && !inherited.has(intent.id) && (
                    <Button
                      variant="ghost"
                      onClick={() =>
                        writeIntents((draft.definition.intents || []).filter((item) => item.id !== intent.id))
                      }
                    >
                      Delete
                    </Button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      </section>
      {selected && (
        <section>
          <p>Selected: {selected.id}</p>
          <div className="form-field">
            <TextInput
              label="Intent id"
              value={selected.id}
              isDisabled={lockedIds.has(selected.id)}
              onChange={(value) => {
                writeIntents(
                  (draft.definition.intents || []).map((item) =>
                    item.id === selected.id ? { ...item, id: value } : item
                  )
                );
                setSelectedId(value);
              }}
            />
          </div>
          <div className="form-grid-two">
            <div className="form-field">
              <TextInput
                label="Intent signals"
                value={(selected.signals || []).join(", ")}
                onChange={(value) =>
                  upsert({
                    ...selected,
                    signals: value
                      .split(",")
                      .map((item) => item.trim())
                      .filter(Boolean),
                  })
                }
              />
            </div>
            <div className="form-field">
              <TextInput
                label="Intent exclude"
                value={(selected.exclude || []).join(", ")}
                onChange={(value) =>
                  upsert({
                    ...selected,
                    exclude: value
                      .split(",")
                      .map((item) => item.trim())
                      .filter(Boolean),
                  })
                }
              />
            </div>
            <label className="form-field">
              <span>Program type</span>
              <select
                aria-label="Intent program type"
                value={selected.program_type || ""}
                onChange={(e) =>
                  upsert({
                    ...selected,
                    program_type: e.target.value || undefined,
                    workflow_type: e.target.value ? undefined : selected.workflow_type,
                    delegate_agent: e.target.value ? undefined : selected.delegate_agent,
                  })
                }
              >
                <option value="">Select program</option>
                {optionsWithCurrent(programs, selected.program_type).map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
            <label className="form-field">
              <span>Workflow type</span>
              <select
                aria-label="Intent workflow type"
                value={selected.workflow_type || ""}
                onChange={(e) =>
                  upsert({
                    ...selected,
                    workflow_type: e.target.value || undefined,
                    program_type: e.target.value ? undefined : selected.program_type,
                    delegate_agent: e.target.value ? undefined : selected.delegate_agent,
                  })
                }
              >
                <option value="">Select workflow</option>
                {optionsWithCurrent(workflows, selected.workflow_type).map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
            <label className="form-field">
              <span>Delegate agent</span>
              <select
                aria-label="Intent delegate agent"
                value={selected.delegate_agent || ""}
                onChange={(e) =>
                  upsert({
                    ...selected,
                    delegate_agent: e.target.value || undefined,
                    program_type: e.target.value ? undefined : selected.program_type,
                    workflow_type: e.target.value ? undefined : selected.workflow_type,
                  })
                }
              >
                <option value="">Select agent</option>
                {optionsWithCurrent(
                  agents.map((item) => item.id),
                  selected.delegate_agent
                ).map((value) => (
                  <option key={value} value={value}>
                    {agents.find((item) => item.id === value)?.label || value}
                  </option>
                ))}
              </select>
            </label>
            <div className="form-field">
              <TextInput
                label="Intent entry phase"
                value={selected.entry_phase || ""}
                onChange={(value) =>
                  upsert({
                    ...selected,
                    entry_phase: value || undefined,
                  })
                }
              />
            </div>
          </div>
          <label className="form-field">
            <span>Orchestrator hint</span>
            <textarea
              aria-label="Intent orchestrator hint"
              rows={3}
              value={selected.orchestrator_hint || ""}
              onChange={(e) =>
                upsert({
                  ...selected,
                  orchestrator_hint: e.target.value || undefined,
                })
              }
            />
          </label>
          <div className="form-grid-two">
            <div className="form-field">
              <TextInput
                label="Intent policy mode"
                value={selected.policy?.mode || ""}
                onChange={(value) =>
                  upsert({
                    ...selected,
                    policy: { ...selected.policy, mode: value || undefined },
                  })
                }
              />
            </div>
            <div className="form-field">
              <TextInput
                label="Intent deny tools"
                value={(selected.policy?.deny_tools || []).join(", ")}
                onChange={(value) =>
                  upsert({
                    ...selected,
                    policy: {
                      ...selected.policy,
                      deny_tools: value
                        .split(",")
                        .map((item) => item.trim())
                        .filter(Boolean),
                    },
                  })
                }
              />
            </div>
            <div className="form-field">
              <TextInput
                label="Intent deny start workflow types"
                value={(selected.policy?.deny_start_workflow_types || []).join(", ")}
                onChange={(value) =>
                  upsert({
                    ...selected,
                    policy: {
                      ...selected.policy,
                      deny_start_workflow_types: value
                        .split(",")
                        .map((item) => item.trim())
                        .filter(Boolean),
                    },
                  })
                }
              />
            </div>
          </div>
          {inherited.has(selected.id) && (
            <Button
              variant="secondary"
              size="sm"
              type="button"
              onClick={() =>
                writeIntents((draft.definition.intents || []).filter((item) => item.id !== selected.id))
              }
            >
              Reset to inherited
            </Button>
          )}
        </section>
      )}
    </div>
  );
}
