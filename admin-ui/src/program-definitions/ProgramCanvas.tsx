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
import { Button } from "../components/Button";
import type { ProgramGraph, ProgramGraphEdge, ProgramGraphNode, ProgramNodeData } from "./programGraph";

if (typeof window !== "undefined" && typeof window.ResizeObserver === "undefined") {
  window.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

function ProgramNodeCard({ data }: NodeProps<ProgramGraphNode>) {
  const variants = data.workflow_variants || [];
  return (
    <div className={`program-node ${data.terminal ? "program-node-terminal" : ""}`}>
      <Handle type="target" position={Position.Left} />
      <strong>{data.label}</strong>
      <span>default: {data.default_workflow_type}</span>
      {variants.length > 0 && (
        <small>
          variants: {variants.map((variant) => variant.workflow_type).join(", ")}
        </small>
      )}
      {data.required_artifacts.length > 0 && (
        <small>requires: {data.required_artifacts.join(", ")}</small>
      )}
      {data.startable && <small>startable</small>}
      {data.terminal && <small>terminal</small>}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

function nextId(graph: ProgramGraph, prefix: string): string {
  const names = new Set(graph.nodes.map((node) => String(node.data.nodeId || node.id)));
  let index = graph.nodes.length + 1;
  let candidate = `${prefix}_${index}`;
  while (names.has(candidate)) {
    index += 1;
    candidate = `${prefix}_${index}`;
  }
  return candidate;
}

export function ProgramCanvas({
  graph,
  selectedNodeId,
  selectedEdgeId,
  readOnly = true,
  onGraphChange,
  onSelectNode,
  onSelectEdge,
}: {
  graph: ProgramGraph;
  selectedNodeId?: string;
  selectedEdgeId?: string;
  readOnly?: boolean;
  onGraphChange?: (graph: ProgramGraph) => void;
  onSelectNode: (nodeId?: string) => void;
  onSelectEdge?: (edgeId?: string) => void;
}) {
  const nodeTypes = useMemo(() => ({ programNode: ProgramNodeCard }), []);
  const [flow, setFlow] = useState<ReactFlowInstance<ProgramGraphNode, ProgramGraphEdge> | null>(
    null
  );
  const selectedNode = graph.nodes.find((node) => node.id === selectedNodeId);

  function commit(next: ProgramGraph) {
    onGraphChange?.(next);
  }

  function updateNodes(changes: NodeChange<ProgramGraphNode>[]) {
    if (readOnly || !onGraphChange) return;
    const nodes = applyNodeChanges(changes, graph.nodes);
    commit({
      ...graph,
      nodes,
      layout: Object.fromEntries(nodes.map((node) => [node.id, node.position])),
    });
  }

  function updateEdges(changes: EdgeChange<ProgramGraphEdge>[]) {
    if (readOnly || !onGraphChange) return;
    commit({ ...graph, edges: applyEdgeChanges(changes, graph.edges) as ProgramGraphEdge[] });
  }

  function connect(connection: Connection) {
    if (readOnly || !onGraphChange || !connection.source || !connection.target) return;
    commit({
      ...graph,
      edges: addEdge(
        {
          ...connection,
          id: `${connection.source}->${connection.target}:manual`,
          label: "manual",
          data: {
            mode: "manual",
            signals: [],
            confirm_required: false,
            artifact_patch: {},
            label: "manual",
          },
        },
        graph.edges
      ),
    });
  }

  function addNode(terminal: boolean) {
    if (readOnly || !onGraphChange) return;
    const id = nextId(graph, terminal ? "terminal" : "node");
    const data: ProgramNodeData = {
      label: id,
      nodeId: id,
      default_workflow_type: "",
      workflow_variants: [],
      required_artifacts: [],
      produces_artifacts: [],
      input_bindings: [],
      terminal,
      startable: graph.nodes.length === 0,
      completion_adapter: "",
    };
    const node: ProgramGraphNode = {
      id,
      type: "programNode",
      position: { x: 80 + graph.nodes.length * 24, y: 80 + graph.nodes.length * 24 },
      data,
    };
    commit({
      ...graph,
      nodes: [...graph.nodes, node],
      layout: { ...graph.layout, [id]: node.position },
    });
    onSelectNode(id);
    onSelectEdge?.(undefined);
  }

  function duplicateSelected() {
    if (readOnly || !onGraphChange || !selectedNode) return;
    const id = nextId(graph, `${selectedNode.data.nodeId || selectedNode.id}_copy`);
    const node: ProgramGraphNode = {
      ...selectedNode,
      id,
      selected: false,
      position: { x: selectedNode.position.x + 40, y: selectedNode.position.y + 40 },
      data: {
        ...selectedNode.data,
        label: id,
        nodeId: id,
        startable: false,
      },
    };
    commit({
      ...graph,
      nodes: [...graph.nodes, node],
      layout: { ...graph.layout, [id]: node.position },
    });
    onSelectNode(id);
    onSelectEdge?.(undefined);
  }

  function deleteSelected() {
    if (readOnly || !onGraphChange) return;
    if (selectedEdgeId) {
      commit({
        ...graph,
        edges: graph.edges.filter((edge) => edge.id !== selectedEdgeId),
      });
      onSelectEdge?.(undefined);
      return;
    }
    if (!selectedNodeId) return;
    const hasDownstream = graph.edges.some((edge) => edge.source === selectedNodeId);
    if (hasDownstream && !window.confirm("Delete this node and its connected edges?")) return;
    commit({
      ...graph,
      nodes: graph.nodes.filter((node) => node.id !== selectedNodeId),
      edges: graph.edges.filter(
        (edge) => edge.source !== selectedNodeId && edge.target !== selectedNodeId
      ),
      layout: Object.fromEntries(
        Object.entries(graph.layout).filter(([key]) => key !== selectedNodeId)
      ),
    });
    onSelectNode(undefined);
  }

  function selectByOffset(offset: number) {
    if (!graph.nodes.length) return;
    const index = Math.max(
      0,
      graph.nodes.findIndex((node) => node.id === selectedNodeId)
    );
    const next = graph.nodes[(index + offset + graph.nodes.length) % graph.nodes.length];
    onSelectNode(next.id);
    onSelectEdge?.(undefined);
  }

  return (
    <ReactFlowProvider>
      <section
        className="program-canvas-shell"
        aria-label="Program canvas"
        tabIndex={0}
        onKeyDown={(event) => {
          if (!readOnly && (event.key === "Delete" || event.key === "Backspace")) {
            event.preventDefault();
            deleteSelected();
          }
          if (event.key === "ArrowDown" || event.key === "ArrowRight") {
            event.preventDefault();
            selectByOffset(1);
          }
          if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
            event.preventDefault();
            selectByOffset(-1);
          }
        }}
      >
        <div className="workflow-toolbar" role="toolbar" aria-label="Program canvas tools">
          <Button disabled={readOnly} variant="secondary" onClick={() => addNode(false)}>
            Add node
          </Button>
          <Button disabled={readOnly} variant="secondary" onClick={() => addNode(true)}>
            Add terminal node
          </Button>
          <Button disabled={readOnly} variant="secondary" onClick={duplicateSelected}>
            Duplicate selected
          </Button>
          <Button disabled={readOnly} variant="danger" onClick={deleteSelected}>
            Delete selected
          </Button>
          <Button variant="secondary" onClick={() => flow?.fitView()}>
            Fit view
          </Button>
        </div>
        <div className="program-canvas-grid">
          <div className="workflow-flow" aria-label="Visual program graph">
            <ReactFlow
              nodes={graph.nodes.map((node) => ({
                ...node,
                selected: node.id === selectedNodeId,
              }))}
              edges={graph.edges.map((edge) => ({
                ...edge,
                selected: edge.id === selectedEdgeId,
              }))}
              nodeTypes={nodeTypes}
              nodesDraggable={!readOnly}
              nodesConnectable={!readOnly}
              elementsSelectable
              onNodesChange={readOnly ? undefined : updateNodes}
              onEdgesChange={readOnly ? undefined : updateEdges}
              onConnect={connect}
              onInit={setFlow}
              onNodeClick={(_, node) => {
                onSelectNode(node.id);
                onSelectEdge?.(undefined);
              }}
              onEdgeClick={(_, edge) => {
                onSelectEdge?.(edge.id);
                onSelectNode(undefined);
              }}
              onPaneClick={() => {
                onSelectNode(undefined);
                onSelectEdge?.(undefined);
              }}
              fitView
            >
              <Background />
              <Controls />
            </ReactFlow>
          </div>
          <aside className="workflow-stage-list">
            <h3>Nodes</h3>
            <ul aria-label="Program nodes">
              {graph.nodes.map((node) => (
                <li key={node.id}>
                  <button
                    className={node.id === selectedNodeId ? "selected" : ""}
                    onClick={() => {
                      onSelectNode(node.id);
                      onSelectEdge?.(undefined);
                    }}
                  >
                    {node.data.label}
                    {node.data.startable ? " startable" : ""}
                    {node.data.terminal ? " terminal" : ""}
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
