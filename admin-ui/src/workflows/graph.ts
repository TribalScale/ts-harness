import type { Edge, Node, XYPosition } from "@xyflow/react";
import type { ValidationIssue, WorkflowDefinition, WorkflowStage } from "./types";

export type WorkflowNodeData = WorkflowStage & { label: string };
export type WorkflowNodeType = "workflowStage" | "workflowFragment";
export type WorkflowNode = Node<WorkflowNodeData, WorkflowNodeType>;

export const CHILD_TERMINAL_EVENT = "workflow.child_terminal";

/** Fragment stages depend on `{{param.<name>}}` placeholders that the including workflow fills in. */
export function isParamRef(stageName: string): boolean {
  return /^\{\{param\.[^}]+\}\}$/.test(stageName);
}

export function isFragmentStage(stage: Pick<WorkflowStage, "kind">): boolean {
  return stage.kind === "fragment";
}

export function nodeTypeForStage(stage: Pick<WorkflowStage, "kind">): WorkflowNodeType {
  return isFragmentStage(stage) ? "workflowFragment" : "workflowStage";
}
export type WorkflowEdge = Edge;

export interface WorkflowGraph {
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  layout: Record<string, XYPosition>;
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function stageName(node: WorkflowNode): string {
  return String(node.data.name || node.id);
}

export function definitionToGraph(
  definition: WorkflowDefinition,
  layout: Record<string, XYPosition> = {}
): WorkflowGraph {
  const nodes: WorkflowNode[] = (definition.stages || []).map((stage, index) => {
    const position = layout[stage.name] || { x: (index % 3) * 260, y: Math.floor(index / 3) * 150 };
    return {
      id: stage.name,
      type: nodeTypeForStage(stage),
      position,
      data: { ...clone(stage), label: stage.name },
    };
  });
  const edges = (definition.stages || []).flatMap((stage) =>
    (stage.depends_on || []).map((source) => ({
      id: `${source}->${stage.name}`,
      source,
      target: stage.name,
    }))
  );
  return { nodes, edges, layout: Object.fromEntries(nodes.map((node) => [node.id, node.position])) };
}

export function graphToDefinition(graph: WorkflowGraph, base: WorkflowDefinition): WorkflowDefinition {
  const next = clone(base);
  const edgesByTarget = new Map<string, string[]>();
  graph.edges.forEach((edge) => {
    const sources = edgesByTarget.get(edge.target) || [];
    sources.push(edge.source);
    edgesByTarget.set(edge.target, sources);
  });
  next.stages = graph.nodes.map((node) => {
    const stage = clone(node.data) as WorkflowStage & { label?: string };
    delete stage.label;
    stage.name = stageName(node);
    const dependsOn = Array.from(new Set(edgesByTarget.get(node.id) || []));
    if (dependsOn.length) {
      stage.depends_on = dependsOn;
    } else {
      delete stage.depends_on;
    }
    return stage;
  });
  delete next["x-builder-layout"];
  return next;
}

function issue(code: string, path: string, message: string): ValidationIssue {
  return { severity: "error", code, path, message };
}

export function detectClientGraphIssues(graph: WorkflowGraph): ValidationIssue[] {
  const issues: ValidationIssue[] = [];
  const names = new Map<string, number>();
  graph.nodes.forEach((node, index) => {
    const name = stageName(node).trim();
    if (!name) issues.push(issue("missing_stage_name", `/stages/${index}/name`, "Stage name is required."));
    if (isFragmentStage(node.data)) {
      if (!String(node.data.fragment || "").trim() || !String(node.data.version || "").trim()) {
        issues.push(issue("missing_fragment", `/stages/${index}/fragment`, "Fragment id and version are required."));
      }
    } else if (!String(node.data.agent_id || "").trim()) {
      issues.push(issue("missing_agent_id", `/stages/${index}/agent_id`, "Stage agent is required."));
    }
    names.set(name, (names.get(name) || 0) + 1);
  });
  for (const [name, count] of names) {
    if (count > 1) issues.push(issue("duplicate_stage_name", "/stages", `Stage name "${name}" is duplicated.`));
  }

  const nodeIds = new Set(graph.nodes.map((node) => node.id));
  const adjacency = new Map<string, string[]>();
  graph.nodes.forEach((node) => adjacency.set(node.id, []));
  graph.edges.forEach((edge) => {
    if (isParamRef(edge.source)) return;
    if (!nodeIds.has(edge.source) || !nodeIds.has(edge.target)) {
      issues.push(
        issue(
          "missing_dependency",
          `/stages/${edge.target}/depends_on`,
          `Dependency ${edge.source} -> ${edge.target} references a missing stage.`
        )
      );
      return;
    }
    adjacency.get(edge.source)?.push(edge.target);
  });

  const visiting = new Set<string>();
  const visited = new Set<string>();
  function visit(id: string): boolean {
    if (visiting.has(id)) return true;
    if (visited.has(id)) return false;
    visiting.add(id);
    for (const next of adjacency.get(id) || []) {
      if (visit(next)) return true;
    }
    visiting.delete(id);
    visited.add(id);
    return false;
  }
  if (graph.nodes.some((node) => visit(node.id))) {
    issues.push(issue("cycle", "/stages", "Workflow dependencies contain a cycle."));
  }
  return issues;
}

export function renameStage(graph: WorkflowGraph, stageId: string, nextName: string): WorkflowGraph {
  const trimmed = nextName.trim();
  if (!trimmed) throw new Error("Stage name is required");
  if (graph.nodes.some((node) => node.id !== stageId && stageName(node) === trimmed)) {
    throw new Error(`Stage "${trimmed}" already exists`);
  }
  return {
    layout: Object.fromEntries(
      Object.entries(graph.layout).map(([key, value]) => [key === stageId ? trimmed : key, value])
    ),
    nodes: graph.nodes.map((node) =>
      node.id === stageId
        ? { ...node, id: trimmed, data: { ...node.data, name: trimmed, label: trimmed } }
        : node
    ),
    edges: graph.edges.map((edge) => ({
      ...edge,
      id: `${edge.source === stageId ? trimmed : edge.source}->${edge.target === stageId ? trimmed : edge.target}`,
      source: edge.source === stageId ? trimmed : edge.source,
      target: edge.target === stageId ? trimmed : edge.target,
    })),
  };
}

export function childJoinName(forkStage: string): string {
  return `join_${forkStage}`;
}

/** The join stage waiting on `forkStage`'s children, if one exists. */
export function findChildJoin(stages: WorkflowStage[], forkStage: string): WorkflowStage | undefined {
  const source = `$prior.${forkStage}.output.children`;
  return stages.find((stage) => {
    const children = (stage.wait_external?.children || {}) as { source?: string };
    return children.source === source;
  });
}

export function addChildJoin(graph: WorkflowGraph, forkStage: string, timeoutSeconds?: number): WorkflowGraph {
  const fork = graph.nodes.find((node) => node.id === forkStage);
  if (!fork) return graph;
  if (findChildJoin(graph.nodes.map((node) => node.data), forkStage)) return graph;
  const name = childJoinName(forkStage);
  if (graph.nodes.some((node) => node.id === name)) throw new Error(`Stage "${name}" already exists`);
  const waitExternal: Record<string, unknown> = {
    hook: "wait_external",
    event_type: CHILD_TERMINAL_EVENT,
    children: { source: `$prior.${forkStage}.output.children` },
  };
  if (timeoutSeconds !== undefined) waitExternal.timeout_seconds = timeoutSeconds;
  const position = { x: fork.position.x, y: fork.position.y + 150 };
  const node: WorkflowNode = {
    id: name,
    type: "workflowStage",
    position,
    data: {
      name,
      label: name,
      kind: "task",
      agent_id: "orchestrator",
      depends_on: [forkStage],
      wait_external: waitExternal,
    },
  };
  return {
    nodes: [...graph.nodes, node],
    edges: [...graph.edges, { id: `${forkStage}->${name}`, source: forkStage, target: name }],
    layout: { ...graph.layout, [name]: position },
  };
}

export function removeChildJoin(graph: WorkflowGraph, forkStage: string): WorkflowGraph {
  const join = findChildJoin(graph.nodes.map((node) => node.data), forkStage);
  if (!join) return graph;
  return {
    nodes: graph.nodes.filter((node) => node.id !== join.name),
    edges: graph.edges.filter((edge) => edge.source !== join.name && edge.target !== join.name),
    layout: Object.fromEntries(Object.entries(graph.layout).filter(([key]) => key !== join.name)),
  };
}

export function setChildJoinTimeout(graph: WorkflowGraph, forkStage: string, timeoutSeconds?: number): WorkflowGraph {
  const join = findChildJoin(graph.nodes.map((node) => node.data), forkStage);
  if (!join) return graph;
  const waitExternal = { ...(join.wait_external || {}) };
  if (timeoutSeconds === undefined) delete waitExternal.timeout_seconds;
  else waitExternal.timeout_seconds = timeoutSeconds;
  return {
    ...graph,
    nodes: graph.nodes.map((node) =>
      node.id === join.name ? { ...node, data: { ...node.data, wait_external: waitExternal } } : node
    ),
  };
}

export function updateStageInGraph(
  graph: WorkflowGraph,
  stageId: string,
  stage: WorkflowStage
): WorkflowGraph {
  const existing = graph.nodes.find((node) => node.id === stageId);
  if (!existing) return graph;
  const renamed = stage.name !== stageId ? renameStage(graph, stageId, stage.name) : graph;
  return {
    ...renamed,
    nodes: renamed.nodes.map((node) =>
      node.id === stage.name
        ? { ...node, type: nodeTypeForStage(stage), data: { ...stage, label: stage.name } }
        : node
    ),
  };
}
