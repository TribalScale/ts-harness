import React from "react";
import type {
  PhaseState,
  PinnedWorkflowVersion,
  ProgramTopology,
  ProgramTopologyEdge,
} from "../api/types";

function humanize(value: string): string {
  return value.replace(/_/g, " ");
}

function progressClass(state?: string, current = false): string {
  if (["succeeded", "complete", "completed"].includes(String(state || "").toLowerCase())) {
    return "complete";
  }
  if (current) return "current";
  return "upcoming";
}

function pinFor(
  phase: string,
  phases: Record<string, PhaseState>,
  resolved?: Record<string, PinnedWorkflowVersion>
): { workflowType?: string; version?: string } {
  const pinned = resolved?.[phase];
  const info = phases[phase];
  return {
    workflowType: pinned?.workflow_type || info?.workflow_type,
    version: pinned?.definition_version || info?.workflow_definition_version,
  };
}

function topologyNodeIds(topology?: ProgramTopology): string[] {
  const nodes = topology?.nodes || [];
  return nodes
    .map((node) => String(node?.id || "").trim())
    .filter(Boolean);
}

function topologyEdges(topology?: ProgramTopology): ProgramTopologyEdge[] {
  return (topology?.edges || []).filter(
    (edge): edge is ProgramTopologyEdge =>
      Boolean(edge && String(edge.source || "").trim() && String(edge.target || "").trim())
  );
}

function resolvePhaseOrder(
  phaseOrder: string[] | undefined,
  phases: Record<string, PhaseState>,
  topology?: ProgramTopology
): string[] {
  if (phaseOrder && phaseOrder.length) return phaseOrder;
  const fromTopology = topologyNodeIds(topology);
  if (fromTopology.length) return fromTopology;
  return Object.keys(phases);
}

export function PhaseStrip({
  phases,
  phaseOrder,
  topology,
  resolvedWorkflowVersions,
  currentPhase,
  selectedPhase,
  onSelect,
  children,
}: {
  phases: Record<string, PhaseState>;
  phaseOrder?: string[];
  topology?: ProgramTopology;
  resolvedWorkflowVersions?: Record<string, PinnedWorkflowVersion>;
  currentPhase?: string;
  selectedPhase?: string;
  onSelect: (phase: string) => void;
  children?: React.ReactNode;
}) {
  const visible = resolvePhaseOrder(phaseOrder, phases, topology);
  const allNodeIds = (() => {
    const ids = topologyNodeIds(topology);
    if (ids.length) return ids;
    const fromPhases = Object.keys(phases);
    const merged = [...visible];
    for (const id of fromPhases) {
      if (!merged.includes(id)) merged.push(id);
    }
    return merged;
  })();
  const alternateIds = allNodeIds.filter((id) => !visible.includes(id));
  const edges = topologyEdges(topology);
  const showTopology = alternateIds.length > 0 || edges.length > 0;

  return (
    <div className="phase-strip-wrap">
      <ol className="phase-strip" aria-label="Program phases">
        {visible.map((phase, index) => {
          const info = phases[phase] || { state: "not_started" };
          const active = selectedPhase === phase || (!selectedPhase && currentPhase === phase);
          const className = progressClass(info.state, phase === currentPhase);
          const pin = pinFor(phase, phases, resolvedWorkflowVersions);
          return (
            <li key={phase} className={`phase-item ${className}`}>
              <button
                type="button"
                className={`phase-block ${active ? "active" : ""}`}
                onClick={() => onSelect(phase)}
                aria-pressed={active}
                aria-current={phase === currentPhase ? "step" : undefined}
              >
                <span className="phase-index">{index + 1}</span>
                <span>
                  <strong>{humanize(phase)}</strong>
                  <span className="state muted">{humanize(info.state)}</span>
                  {pin.workflowType && (
                    <span className="muted mono phase-pin">
                      {pin.workflowType}
                      {pin.version ? `@${pin.version}` : ""}
                    </span>
                  )}
                  {info.active_step && <span className="muted mono">{info.active_step}</span>}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
      {children}
      {showTopology && (
        <details className="topology-expander">
          <summary>Full topology</summary>
          <div className="topology-body" aria-label="Full program topology">
            <p className="muted">
              Showing the selected/executed branch above. Alternatives and edges from the pinned
              snapshot appear here.
            </p>
            {alternateIds.length > 0 && (
              <div className="topology-alternatives" aria-label="Alternate phases">
                <strong>Alternatives</strong>
                <ul>
                  {alternateIds.map((phase) => {
                    const info = phases[phase] || { state: "not_started" };
                    const pin = pinFor(phase, phases, resolvedWorkflowVersions);
                    return (
                      <li key={phase}>
                        <button type="button" className="linkish" onClick={() => onSelect(phase)}>
                          {humanize(phase)}
                        </button>
                        <span className="muted"> · {humanize(info.state)}</span>
                        {pin.workflowType && (
                          <span className="muted mono">
                            {" "}
                            · {pin.workflowType}
                            {pin.version ? `@${pin.version}` : ""}
                          </span>
                        )}
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}
            {edges.length > 0 && (
              <div className="topology-edges" aria-label="Topology edges">
                <strong>Edges</strong>
                <ul>
                  {edges.map((edge, index) => (
                    <li key={`${edge.source}->${edge.target}-${index}`} className="mono">
                      {edge.source} → {edge.target}
                      {edge.mode ? ` (${edge.mode})` : ""}
                      {visible.includes(String(edge.target)) ? "" : " · alternate"}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {allNodeIds.length > 0 && (
              <div className="topology-nodes" aria-label="Topology nodes">
                <strong>Nodes</strong>
                <ul>
                  {allNodeIds.map((phase) => {
                    const onBranch = visible.includes(phase);
                    const pin = pinFor(phase, phases, resolvedWorkflowVersions);
                    return (
                      <li key={phase}>
                        <span className="mono">{phase}</span>
                        <span className="muted">
                          {" "}
                          · {onBranch ? "on branch" : "alternate"}
                          {pin.workflowType
                            ? ` · ${pin.workflowType}${pin.version ? `@${pin.version}` : ""}`
                            : ""}
                        </span>
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}
          </div>
        </details>
      )}
    </div>
  );
}
