import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";

const fitView = vi.hoisted(() => vi.fn());

vi.mock("@xyflow/react", async () => {
  const React = await import("react");
  return {
    Background: () => null,
    Controls: () => null,
    ReactFlowProvider: ({ children }: { children: React.ReactNode }) =>
      React.createElement("div", null, children),
    Handle: ({ position, type }: { position?: string; type?: string }) =>
      React.createElement("span", { "data-handlepos": position, "data-handletype": type }),
    Position: { Left: "left", Right: "right", Top: "top", Bottom: "bottom" },
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
  };
});

import { renderPage } from "../test/render";
import { ProgramCanvas } from "./ProgramCanvas";
import { definitionToProgramGraph } from "./programGraph";
import type { ProgramDefinition } from "./types";

const definition: ProgramDefinition = {
  program_type: "software_delivery",
  definition_version: "1.0.0",
  display_name: "Software delivery",
  startable_nodes: ["prd", "deploy"],
  nodes: [
    {
      id: "prd",
      default_workflow_type: "prd_request",
      workflow_variants: [],
      required_artifacts: [],
      produces_artifacts: ["approved_prd"],
      input_bindings: [],
      completion_adapter: "extract_prd_outputs",
      terminal: false,
    },
    {
      id: "deploy",
      default_workflow_type: "deploy_release",
      workflow_variants: [],
      required_artifacts: ["pr_url"],
      produces_artifacts: ["merge_sha"],
      input_bindings: [],
      completion_adapter: "extract_deploy_outputs",
      terminal: true,
    },
  ],
  edges: [{ source: "prd", target: "deploy", mode: "manual", signals: [] }],
};

it("renders an accessible node list and supports keyboard selection", async () => {
  const onSelectNode = vi.fn();
  const user = userEvent.setup();

  renderPage(
    <ProgramCanvas
      graph={definitionToProgramGraph(definition)}
      selectedNodeId="prd"
      onSelectNode={onSelectNode}
    />
  );

  expect(screen.getByRole("list", { name: /program nodes/i })).toBeInTheDocument();
  expect(document.querySelectorAll('[data-handlepos="left"]').length).toBeGreaterThan(0);
  expect(document.querySelectorAll('[data-handlepos="right"]').length).toBeGreaterThan(0);
  expect(screen.getByRole("button", { name: /deploy.*terminal/i })).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: /deploy.*terminal/i }));
  expect(onSelectNode).toHaveBeenCalledWith("deploy");

  const canvas = screen.getByRole("region", { name: /program canvas/i });
  canvas.focus();
  await user.keyboard("{ArrowDown}");
  expect(onSelectNode).toHaveBeenCalledWith("deploy");
});

it("fits the canvas through the React Flow instance", async () => {
  fitView.mockClear();

  renderPage(
    <ProgramCanvas
      graph={definitionToProgramGraph(definition)}
      selectedNodeId="prd"
      onSelectNode={() => undefined}
    />
  );

  await userEvent.click(screen.getByRole("button", { name: /fit view/i }));
  expect(fitView).toHaveBeenCalled();
});
