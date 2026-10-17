import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  ReactFlowProvider,
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  type Connection,
  type EdgeChange,
  type NodeChange,
  type NodeProps,
  type ReactFlowInstance,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useMemo, useState } from "react";
import { apiFetch } from "../api/client";
import { WorkflowToolbar } from "./WorkflowToolbar";
import { nodeTypeForStage, type WorkflowEdge, type WorkflowGraph, type WorkflowNode, type WorkflowNodeData } from "./graph";
import type { FragmentDefinition, FragmentSummary } from "./types";

if (typeof window !== "undefined" && typeof window.ResizeObserver === "undefined") {
  window.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

function nodeBadge(data: WorkflowNodeData): string {
  if (data.kind === "fragment") return "fragment";
  if (data.wait_external) return "wait";
  if (data.barrier?.hook === "fork_children") return "children";
  if (data.barrier) return "barrier";
  if (data.approval_required) return "gate";
  return String(data.kind || "task");
}

function childType(data: WorkflowNodeData): string {
  if (data.barrier?.hook !== "fork_children") return "";
  return String(data.barrier.workflow_type || "");
}

function StageNode({ data }: NodeProps<WorkflowNode>) {
  const badge = nodeBadge(data);
  const child = childType(data);
  return (
    <div className={`workflow-node ${badge === "gate" ? "workflow-node-gate" : ""}`}>
      <Handle type="target" position={Position.Left} />
      <strong>{data.name}</strong>
      <span>{badge}</span>
      {child && <span className="workflow-node-child">→ {child}</span>}
      {data.agent_id && <small>{data.agent_id}</small>}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

export function FragmentNode({ data }: NodeProps<WorkflowNode>) {
  const [expanded, setExpanded] = useState(false);
  const [stages, setStages] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function toggle() {
    const next = !expanded;
    setExpanded(next);
    if (!next || stages) return;
    try {
      const result = await apiFetch<{ definition: FragmentDefinition }>(
        `/admin/workflow-fragments/${encodeURIComponent(String(data.fragment || ""))}?version=${encodeURIComponent(
          String(data.version || "")
        )}`
      );
      setStages((result.definition.stages || []).map((stage) => stage.name));
      setError(null);
    } catch (e: any) {
      setError(String(e.message || e));
    }
  }

  return (
    <div className="workflow-node workflow-node-fragment">
      <Handle type="target" position={Position.Left} />
      <strong>{data.name}</strong>
      <span>
        {String(data.fragment || "?")} v{String(data.version || "?")}
      </span>
      <button
        type="button"
        className="link-button nodrag"
        aria-expanded={expanded}
        aria-label={`${expanded ? "Collapse" : "Expand"} fragment ${data.name}`}
        onClick={(event) => {
          event.stopPropagation();
          void toggle();
        }}
      >
        {expanded ? "Hide stages" : "Show stages"}
      </button>
      {expanded && error && <small className="error">{error}</small>}
      {expanded && stages && (
        <ul className="workflow-node-fragment-stages" aria-label={`Fragment ${data.name} stages`}>
          {stages.map((stage) => (
            <li key={stage}>{stage}</li>
          ))}
        </ul>
      )}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

function nextName(graph: WorkflowGraph, prefix: string): string {
  const names = new Set(graph.nodes.map((node) => String(node.data.name)));
  let index = graph.nodes.length + 1;
  let candidate = `${prefix}_${index}`;
  while (names.has(candidate)) {
    index += 1;
    candidate = `${prefix}_${index}`;
  }
  return candidate;
}

export function WorkflowCanvas({
  graph,
  selectedStageId,
  readOnly,
  onGraphChange,
  onSelectStage,
  allowFragments = true,
}: {
  graph: WorkflowGraph;
  selectedStageId?: string;
  readOnly: boolean;
  onGraphChange: (graph: WorkflowGraph) => void;
  onSelectStage: (stageId?: string) => void;
  /** Fragments cannot include other fragments, so the fragment builder turns this off. */
  allowFragments?: boolean;
}) {
  const nodeTypes = useMemo(() => ({ workflowStage: StageNode, workflowFragment: FragmentNode }), []);
  const selectedNode = graph.nodes.find((node) => node.id === selectedStageId);
  const [flow, setFlow] = useState<ReactFlowInstance<WorkflowNode, WorkflowEdge> | null>(null);
  const [picker, setPicker] = useState<{ open: boolean; fragments: FragmentSummary[]; error?: string | null }>({
    open: false,
    fragments: [],
  });

  async function toggleFragmentPicker() {
    if (picker.open) {
      setPicker({ ...picker, open: false });
      return;
    }
    setPicker({ ...picker, open: true, error: null });
    try {
      const data = await apiFetch<{ fragments: FragmentSummary[] }>("/admin/workflow-fragments");
      const usable = (data.fragments || []).filter((item) => item.status === "active" || item.status === "built_in");
      setPicker({ open: true, fragments: usable, error: null });
    } catch (e: any) {
      setPicker({ open: true, fragments: [], error: String(e.message || e) });
    }
  }

  function insertFragment(fragment: FragmentSummary) {
    const params = Object.fromEntries(Object.keys(fragment.params || {}).map((name) => [name, ""]));
    const inputs = Object.fromEntries(Object.keys(fragment.inputs || {}).map((name) => [name, ""]));
    addNode({
      prefix: fragment.fragment_id,
      kind: "fragment",
      agent_id: undefined,
      fragment: fragment.fragment_id,
      version: fragment.definition_version,
      params,
      inputs,
    });
    setPicker({ ...picker, open: false });
  }

  function updateNodes(changes: NodeChange<WorkflowNode>[]) {
    onGraphChange({ ...graph, nodes: applyNodeChanges(changes, graph.nodes) });
  }

  function updateEdges(changes: EdgeChange[]) {
    onGraphChange({ ...graph, edges: applyEdgeChanges(changes, graph.edges) });
  }

  function connect(connection: Connection) {
    if (readOnly) return;
    onGraphChange({
      ...graph,
      edges: addEdge({ ...connection, id: `${connection.source}->${connection.target}` }, graph.edges),
    });
  }

  function addNode(data: Partial<WorkflowNodeData> & { prefix: string }) {
    const { prefix, ...stageData } = data;
    const name = nextName(graph, prefix);
    const nodeData = {
      name,
      label: name,
      kind: "task",
      agent_id: "",
      ...stageData,
    } as WorkflowNodeData;
    if (nodeData.agent_id === undefined) delete nodeData.agent_id;
    const node: WorkflowNode = {
      id: name,
      type: nodeTypeForStage(nodeData),
      position: { x: 80 + graph.nodes.length * 24, y: 80 + graph.nodes.length * 24 },
      data: nodeData,
    };
    onGraphChange({
      ...graph,
      nodes: [...graph.nodes, node],
      layout: { ...graph.layout, [name]: node.position },
    });
    onSelectStage(name);
  }

  function duplicateSelected() {
    if (!selectedNode) return;
    const name = nextName(graph, `${selectedNode.data.name}_copy`);
    const node: WorkflowNode = {
      ...selectedNode,
      id: name,
      selected: false,
      position: { x: selectedNode.position.x + 40, y: selectedNode.position.y + 40 },
      data: { ...selectedNode.data, name, label: name },
    };
    onGraphChange({ ...graph, nodes: [...graph.nodes, node], layout: { ...graph.layout, [name]: node.position } });
    onSelectStage(name);
  }

  function deleteSelected() {
    if (!selectedStageId) return;
    const hasDownstream = graph.edges.some((edge) => edge.source === selectedStageId);
    if (hasDownstream && !window.confirm("Delete this stage and its downstream dependency links?")) return;
    onGraphChange({
      ...graph,
      nodes: graph.nodes.filter((node) => node.id !== selectedStageId),
      edges: graph.edges.filter((edge) => edge.source !== selectedStageId && edge.target !== selectedStageId),
      layout: Object.fromEntries(Object.entries(graph.layout).filter(([key]) => key !== selectedStageId)),
    });
    onSelectStage(undefined);
  }

  return (
    <ReactFlowProvider>
      <section
        className="workflow-canvas-shell"
        aria-label="Workflow canvas"
        tabIndex={0}
        onKeyDown={(event) => {
          if (!readOnly && (event.key === "Delete" || event.key === "Backspace")) deleteSelected();
        }}
      >
        <WorkflowToolbar
          readOnly={readOnly}
          onAddTask={() => addNode({ prefix: "task", kind: "task" })}
          onAddGate={() =>
            addNode({
              prefix: "gate",
              kind: "task",
              agent_id: "orchestrator",
              approval_required: true,
            })
          }
          onAddExpand={() => addNode({ prefix: "expand", kind: "expand", plan_approval_required: true })}
          onAddCompensate={() =>
            addNode({ prefix: "compensate", kind: "compensate", agent_id: "orchestrator" })
          }
          onAddWait={() =>
            addNode({
              prefix: "wait",
              kind: "task",
              agent_id: "orchestrator",
              wait_external: { hook: "wait_external", event_type: "channel.intake_response" },
            })
          }
          onAddBarrier={() =>
            addNode({
              prefix: "barrier",
              kind: "task",
              agent_id: "orchestrator",
              barrier: { hook: "collect_delivery_results" },
            })
          }
          onAddChildWorkflows={() =>
            addNode({
              prefix: "fork",
              kind: "task",
              agent_id: "orchestrator",
              barrier: { hook: "fork_children", workflow_type: "", input_map: {} },
            })
          }
          fragmentPicker={allowFragments ? picker : undefined}
          onToggleFragmentPicker={allowFragments ? () => void toggleFragmentPicker() : undefined}
          onInsertFragment={insertFragment}
          onDuplicate={duplicateSelected}
          onDelete={deleteSelected}
          onFitView={() => flow?.fitView()}
        />
        <div className="workflow-canvas-grid">
          <div className="workflow-flow" aria-label="Visual workflow graph">
            <ReactFlow
              nodes={graph.nodes.map((node) => ({ ...node, selected: node.id === selectedStageId }))}
              edges={graph.edges}
              nodeTypes={nodeTypes}
              onNodesChange={readOnly ? undefined : updateNodes}
              onEdgesChange={readOnly ? undefined : updateEdges}
              onConnect={connect}
              onInit={setFlow}
              onNodeClick={(_, node) => onSelectStage(node.id)}
              onPaneClick={() => onSelectStage(undefined)}
              fitView
            >
              <Background />
              <Controls />
            </ReactFlow>
          </div>
          <aside className="workflow-stage-list">
            <h3>Stages</h3>
            <ul aria-label="Workflow stages">
              {graph.nodes.map((node) => (
                <li key={node.id}>
                  <button
                    className={node.id === selectedStageId ? "selected" : ""}
                    onClick={() => onSelectStage(node.id)}
                  >
                    {node.data.name} {nodeBadge(node.data)}
                  </button>
                </li>
              ))}
            </ul>
          </aside>
        </div>
      </section>
    </ReactFlowProvider>
  );
}
