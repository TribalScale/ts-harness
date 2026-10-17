import type { Edge, Node, XYPosition } from "@xyflow/react";
import type {
  InputBinding,
  ProgramDefinition,
  ProgramEdge,
  ProgramNode,
  ValidationIssue,
  WorkflowVariant,
} from "./types";

export type ProgramNodeData = {
  label: string;
  nodeId: string;
  default_workflow_type: string;
  workflow_variants: WorkflowVariant[];
  required_artifacts: string[];
  produces_artifacts: string[];
  input_bindings: InputBinding[];
  terminal: boolean;
  startable: boolean;
  completion_adapter: string;
};

export type ProgramGraphNode = Node<ProgramNodeData, "programNode">;
export type ProgramGraphEdge = Edge<{
  mode: string;
  signals: string[];
  confirm_required: boolean;
  artifact_patch: Record<string, string | number | boolean | null>;
  label: string;
}>;

export interface ProgramGraph {
  nodes: ProgramGraphNode[];
  edges: ProgramGraphEdge[];
  layout: Record<string, XYPosition>;
}

export type EditableProgramNode = ProgramNode & { startable?: boolean };

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function edgeLabel(edge: Pick<ProgramEdge, "mode" | "signals">): string {
  const mode = edge.mode === "automatic" ? "automatic" : "manual";
  const signals = (edge.signals || []).filter(Boolean);
  if (!signals.length) return mode;
  return `${mode}: ${signals.join(", ")}`;
}

function depthsFor(definition: ProgramDefinition): Map<string, number> {
  const nodeIds = new Set((definition.nodes || []).map((node) => node.id));
  const incoming = new Map<string, string[]>();
  nodeIds.forEach((id) => incoming.set(id, []));
  (definition.edges || []).forEach((edge) => {
    if (!nodeIds.has(edge.source) || !nodeIds.has(edge.target)) return;
    incoming.get(edge.target)?.push(edge.source);
  });

  const depths = new Map<string, number>();
  function depthOf(id: string, stack: Set<string>): number {
    if (depths.has(id)) return depths.get(id)!;
    if (stack.has(id)) return 0;
    stack.add(id);
    const parents = incoming.get(id) || [];
    const depth = parents.length ? Math.max(...parents.map((parent) => depthOf(parent, stack))) + 1 : 0;
    stack.delete(id);
    depths.set(id, depth);
    return depth;
  }
  nodeIds.forEach((id) => depthOf(id, new Set()));
  return depths;
}

function issue(code: string, path: string, message: string): ValidationIssue {
  return { severity: "error", code, path, message };
}

export function definitionToProgramGraph(
  definition: ProgramDefinition,
  layout: Record<string, XYPosition> = {}
): ProgramGraph {
  const startable = new Set(definition.startable_nodes || []);
  const depths = depthsFor(definition);
  const columns = new Map<number, number>();
  const usedIds = new Map<string, number>();

  const nodes: ProgramGraphNode[] = (definition.nodes || []).map((node, index) => {
    const depth = depths.get(node.id) || 0;
    const column = columns.get(depth) || 0;
    columns.set(depth, column + 1);
    const occurrence = usedIds.get(node.id) || 0;
    usedIds.set(node.id, occurrence + 1);
    const graphId = occurrence === 0 ? node.id : `${node.id}__dup_${index}`;
    const position = layout[node.id] || layout[graphId] || { x: depth * 280, y: column * 160 };
    return {
      id: graphId,
      type: "programNode",
      position,
      data: {
        label: node.id,
        nodeId: node.id,
        default_workflow_type: node.default_workflow_type,
        workflow_variants: clone(node.workflow_variants || []),
        required_artifacts: [...(node.required_artifacts || [])],
        produces_artifacts: [...(node.produces_artifacts || [])],
        input_bindings: clone(node.input_bindings || []),
        terminal: Boolean(node.terminal),
        startable: startable.has(node.id),
        completion_adapter: node.completion_adapter,
      },
    };
  });

  const edges: ProgramGraphEdge[] = (definition.edges || []).map((edge, index) => {
    const label = edgeLabel(edge);
    return {
      id: `${edge.source}->${edge.target}:${edge.mode}:${index}`,
      source: edge.source,
      target: edge.target,
      label,
      data: {
        mode: edge.mode,
        signals: [...(edge.signals || [])],
        confirm_required: Boolean(edge.confirm_required),
        artifact_patch: clone(edge.artifact_patch || {}),
        label,
      },
    };
  });

  return {
    nodes,
    edges,
    layout: Object.fromEntries(nodes.map((node) => [node.id, node.position])),
  };
}

export function graphToProgramDefinition(
  graph: ProgramGraph,
  base: ProgramDefinition
): ProgramDefinition {
  const next = clone(base);
  next.nodes = graph.nodes.map((node) => ({
    id: String(node.data.nodeId || node.id).trim(),
    default_workflow_type: node.data.default_workflow_type,
    workflow_variants: clone(node.data.workflow_variants || []),
    required_artifacts: [...(node.data.required_artifacts || [])],
    produces_artifacts: [...(node.data.produces_artifacts || [])],
    input_bindings: clone(node.data.input_bindings || []),
    completion_adapter: node.data.completion_adapter,
    terminal: Boolean(node.data.terminal),
  }));
  next.startable_nodes = graph.nodes
    .filter((node) => node.data.startable)
    .map((node) => String(node.data.nodeId || node.id).trim());
  next.edges = graph.edges.map((edge) => {
    const mapped: ProgramEdge = {
      source: edge.source,
      target: edge.target,
      mode: edge.data?.mode || "manual",
      signals: [...(edge.data?.signals || [])],
      artifact_patch: clone(edge.data?.artifact_patch || {}),
    };
    if (edge.data?.confirm_required) mapped.confirm_required = true;
    return mapped;
  });
  delete next["x-builder-layout"];
  return next;
}

export function detectClientProgramGraphIssues(graph: ProgramGraph): ValidationIssue[] {
  const issues: ValidationIssue[] = [];
  const idCounts = new Map<string, number>();
  graph.nodes.forEach((node, index) => {
    const id = String(node.data.nodeId || node.id).trim();
    if (!id) {
      issues.push(issue("missing_node_id", `/nodes/${index}/id`, "Node id is required."));
    }
    idCounts.set(id, (idCounts.get(id) || 0) + 1);
  });
  for (const [id, count] of idCounts) {
    if (id && count > 1) {
      issues.push(issue("duplicate_node", "/nodes", `duplicate node id: ${id}`));
    }
  }

  const startable = graph.nodes.filter((node) => node.data.startable).map((node) => node.data.nodeId || node.id);
  if (!startable.length) {
    issues.push(issue("missing_startable_nodes", "/startable_nodes", "At least one startable node is required."));
  }

  const nodeIds = new Set(graph.nodes.map((node) => node.id));
  const logicalIds = new Set(graph.nodes.map((node) => String(node.data.nodeId || node.id)));
  const adjacency = new Map<string, string[]>();
  graph.nodes.forEach((node) => adjacency.set(node.id, []));

  graph.edges.forEach((edge, index) => {
    const sourceOk = nodeIds.has(edge.source) || logicalIds.has(edge.source);
    const targetOk = nodeIds.has(edge.target) || logicalIds.has(edge.target);
    if (!sourceOk || !targetOk) {
      issues.push(
        issue(
          "unknown_edge_endpoint",
          `/edges/${index}`,
          `edge references unknown node: ${edge.source} -> ${edge.target}`
        )
      );
      return;
    }
    const sourceId = nodeIds.has(edge.source)
      ? edge.source
      : graph.nodes.find((node) => node.data.nodeId === edge.source)?.id || edge.source;
    const targetId = nodeIds.has(edge.target)
      ? edge.target
      : graph.nodes.find((node) => node.data.nodeId === edge.target)?.id || edge.target;
    adjacency.get(sourceId)?.push(targetId);
  });

  const startableGraphIds = graph.nodes.filter((node) => node.data.startable).map((node) => node.id);
  const reachable = new Set<string>();
  const stack = [...startableGraphIds];
  while (stack.length) {
    const id = stack.pop()!;
    if (reachable.has(id)) continue;
    reachable.add(id);
    for (const next of adjacency.get(id) || []) {
      if (!reachable.has(next)) stack.push(next);
    }
  }
  graph.nodes.forEach((node, index) => {
    if (!node.data.startable && !reachable.has(node.id)) {
      issues.push(
        issue("unreachable_node", `/nodes/${index}`, `node is unreachable from startable nodes: ${node.data.nodeId || node.id}`)
      );
    }
  });

  graph.nodes.forEach((node, index) => {
    const outs = adjacency.get(node.id) || [];
    if (node.data.terminal && outs.length) {
      issues.push(
        issue(
          "terminal_has_outgoing_edge",
          `/nodes/${index}`,
          `terminal node must not have outgoing edges: ${node.data.nodeId || node.id}`
        )
      );
    }
  });

  const visiting = new Set<string>();
  const visited = new Set<string>();
  function visit(id: string, path: string[]): boolean {
    if (visiting.has(id)) {
      issues.push(issue("cycle", "/edges", `edge cycle detected: ${[...path, id].join(" -> ")}`));
      return true;
    }
    if (visited.has(id)) return false;
    visiting.add(id);
    for (const next of adjacency.get(id) || []) {
      visit(next, [...path, id]);
    }
    visiting.delete(id);
    visited.add(id);
    return false;
  }
  graph.nodes.forEach((node) => visit(node.id, []));

  return issues;
}

export function renameNode(graph: ProgramGraph, nodeId: string, nextId: string): ProgramGraph {
  const trimmed = nextId.trim();
  if (!trimmed) throw new Error("Node id is required");
  if (
    graph.nodes.some(
      (node) => node.id !== nodeId && String(node.data.nodeId || node.id) === trimmed
    )
  ) {
    throw new Error(`Node "${trimmed}" already exists`);
  }
  return {
    layout: Object.fromEntries(
      Object.entries(graph.layout).map(([key, value]) => [key === nodeId ? trimmed : key, value])
    ),
    nodes: graph.nodes.map((node) =>
      node.id === nodeId
        ? {
            ...node,
            id: trimmed,
            data: { ...node.data, nodeId: trimmed, label: trimmed },
          }
        : node
    ),
    edges: graph.edges.map((edge) => {
      const source = edge.source === nodeId ? trimmed : edge.source;
      const target = edge.target === nodeId ? trimmed : edge.target;
      return {
        ...edge,
        id: `${source}->${target}:${edge.data?.mode || "manual"}`,
        source,
        target,
      };
    }),
  };
}

export function updateNodeInGraph(
  graph: ProgramGraph,
  nodeId: string,
  node: EditableProgramNode
): ProgramGraph {
  const existing = graph.nodes.find((entry) => entry.id === nodeId);
  if (!existing) return graph;
  const renamed = node.id !== nodeId ? renameNode(graph, nodeId, node.id) : graph;
  const targetId = node.id;
  return {
    ...renamed,
    nodes: renamed.nodes.map((entry) =>
      entry.id === targetId
        ? {
            ...entry,
            data: {
              ...entry.data,
              label: node.id,
              nodeId: node.id,
              default_workflow_type: node.default_workflow_type,
              workflow_variants: clone(node.workflow_variants || []),
              required_artifacts: [...(node.required_artifacts || [])],
              produces_artifacts: [...(node.produces_artifacts || [])],
              input_bindings: clone(node.input_bindings || []),
              terminal: Boolean(node.terminal),
              startable: Boolean(node.startable ?? entry.data.startable),
              completion_adapter: node.completion_adapter,
            },
          }
        : entry
    ),
  };
}

export function updateEdgeInGraph(
  graph: ProgramGraph,
  edgeId: string,
  edge: ProgramEdge
): ProgramGraph {
  return {
    ...graph,
    edges: graph.edges.map((entry) => {
      if (entry.id !== edgeId) return entry;
      const label = edgeLabel(edge);
      return {
        ...entry,
        id: `${edge.source}->${edge.target}:${edge.mode}`,
        source: edge.source,
        target: edge.target,
        label,
        data: {
          mode: edge.mode,
          signals: [...(edge.signals || [])],
          confirm_required: Boolean(edge.confirm_required),
          artifact_patch: clone(edge.artifact_patch || {}),
          label,
        },
      };
    }),
  };
}

export function programsReferencingWorkflow(
  definitions: Array<{ program_type: string; display_name?: string; definition?: ProgramDefinition }>,
  workflowType: string
): Array<{ program_type: string; display_name: string }> {
  const matches: Array<{ program_type: string; display_name: string }> = [];
  for (const item of definitions) {
    const definition = item.definition;
    if (!definition) continue;
    const referenced = (definition.nodes || []).some(
      (node) =>
        node.default_workflow_type === workflowType ||
        (node.workflow_variants || []).some((variant) => variant.workflow_type === workflowType)
    );
    if (referenced) {
      matches.push({
        program_type: item.program_type,
        display_name: item.display_name || definition.display_name || item.program_type,
      });
    }
  }
  return matches;
}
