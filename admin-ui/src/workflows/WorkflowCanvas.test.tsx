import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";

const fitView = vi.hoisted(() => vi.fn());

vi.mock("@xyflow/react", async () => {
  const React = await import("react");
  return {
    Background: () => null,
    Controls: () => null,
    ReactFlowProvider: ({ children }: { children: React.ReactNode }) => React.createElement("div", null, children),
    ReactFlow: ({
      children,
      nodes,
      nodeTypes,
      onInit,
    }: {
      children: React.ReactNode;
      nodes?: Array<{ id: string; type?: string; data: unknown }>;
      nodeTypes?: Record<string, React.ComponentType<{ data: unknown }>>;
      onInit?: (instance: { fitView: () => void }) => void;
    }) => {
      React.useEffect(() => {
        onInit?.({ fitView });
      }, [onInit]);
      return React.createElement(
        "div",
        { "aria-label": "mock react flow" },
        (nodes || []).map((node) => {
          const Node = node.type ? nodeTypes?.[node.type] : undefined;
          return Node
            ? React.createElement(Node, { key: node.id, data: node.data })
            : null;
        }),
        children
      );
    },
    Handle: ({ position, type }: { position?: string; type?: string }) =>
      React.createElement("span", { "data-handlepos": position, "data-handletype": type }),
    Position: { Left: "left", Right: "right", Top: "top", Bottom: "bottom" },
    addEdge: (edge: unknown, edges: unknown[]) => [...edges, edge],
    applyEdgeChanges: (_changes: unknown[], edges: unknown[]) => edges,
    applyNodeChanges: (_changes: unknown[], nodes: unknown[]) => nodes,
  };
});

import { renderPage } from "../test/render";
import { definitionToGraph } from "./graph";
import { WorkflowCanvas } from "./WorkflowCanvas";
import type { WorkflowDefinition } from "./types";

const definition: WorkflowDefinition = {
  workflow_type: "ship_feature",
  definition_version: "1.0.0",
  stages: [
    { name: "draft_plan", kind: "task", agent_id: "pm-agent" },
    { name: "approve_plan", kind: "task", agent_id: "orchestrator", approval_required: true, depends_on: ["draft_plan"] },
  ],
};

it("renders accessible stage list and supports toolbar node creation", async () => {
  const onGraphChange = vi.fn();
  const onSelectStage = vi.fn();

  renderPage(
    <WorkflowCanvas
      graph={definitionToGraph(definition)}
      selectedStageId="draft_plan"
      readOnly={false}
      onGraphChange={onGraphChange}
      onSelectStage={onSelectStage}
    />
  );

  expect(screen.getByRole("list", { name: /workflow stages/i })).toBeInTheDocument();
  expect(document.querySelectorAll('[data-handlepos="left"]').length).toBeGreaterThan(0);
  expect(document.querySelectorAll('[data-handlepos="right"]').length).toBeGreaterThan(0);
  expect(screen.getByRole("button", { name: /draft_plan task/i })).toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: /^add gate$/i }));

  expect(onGraphChange).toHaveBeenCalledWith(
    expect.objectContaining({
      nodes: expect.arrayContaining([
        expect.objectContaining({ data: expect.objectContaining({ approval_required: true }) }),
      ]),
    })
  );
});

it("fits the canvas through the React Flow instance", async () => {
  fitView.mockClear();

  renderPage(
    <WorkflowCanvas
      graph={definitionToGraph(definition)}
      selectedStageId="draft_plan"
      readOnly={false}
      onGraphChange={() => undefined}
      onSelectStage={() => undefined}
    />
  );

  await userEvent.click(screen.getByRole("button", { name: /fit view/i }));

  expect(fitView).toHaveBeenCalled();
});
