import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { ProgramDefinitionStudio } from "./ProgramDefinitionStudio";

vi.mock("@xyflow/react", async () => {
  const React = await import("react");
  return {
    Background: () => null,
    Controls: () => null,
    ReactFlowProvider: ({ children }: { children: React.ReactNode }) =>
      React.createElement("div", null, children),
    ReactFlow: ({ children }: { children: React.ReactNode }) =>
      React.createElement("div", { "aria-label": "mock react flow" }, children),
    addEdge: (edge: unknown, edges: unknown[]) => [...edges, edge],
    applyEdgeChanges: (_changes: unknown, edges: unknown[]) => edges,
    applyNodeChanges: (_changes: unknown, nodes: unknown[]) => nodes,
  };
});

vi.mock("../workflows/WorkflowBuilderPage", () => ({
  WorkflowBuilderPage: ({ route }: { route: { kind: string; workflowType?: string } }) => (
    <div>
      Workflow builder {route.kind}
      {route.workflowType ? `:${route.workflowType}` : ""}
    </div>
  ),
}));

const definition = {
  program_type: "software_delivery",
  definition_version: "1.0.0",
  display_name: "Software delivery",
  startable_nodes: ["prd", "jira"],
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
      id: "jira",
      default_workflow_type: "jira_from_prd",
      workflow_variants: [],
      required_artifacts: ["approved_prd"],
      produces_artifacts: [],
      input_bindings: [],
      completion_adapter: "extract_jira_outputs",
      terminal: true,
    },
  ],
  edges: [
    {
      source: "prd",
      target: "jira",
      mode: "manual",
      signals: ["create jira"],
      artifact_patch: {},
    },
  ],
};

beforeEach(() => {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: query.includes("min-width: 1024px"),
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
});

function mockProgramApis() {
  mockFetch((url) => {
    if (url === "/admin/program-definitions") {
      return jsonResponse({
        definitions: [
          {
            program_type: "software_delivery",
            definition_version: "1.0.0",
            display_name: "Software delivery",
            status: "built_in",
            read_only: true,
          },
        ],
      });
    }
    if (url === "/admin/program-definitions/catalog") {
      return jsonResponse({
        workflows: [{ workflow_type: "prd_request" }],
        adapters: [{ id: "extract_prd_outputs", label: "extract_prd_outputs" }],
        transition_modes: ["manual", "automatic"],
        templates: [],
      });
    }
    if (url === "/admin/program-definitions/software_delivery") {
      return jsonResponse({ status: "built_in", read_only: true, definition });
    }
    throw new Error(`Unexpected URL ${url}`);
  });
}

it("loads a built-in program map and drills into a workflow", async () => {
  mockProgramApis();
  const onNavigateWorkflow = vi.fn();
  const onChangeTab = vi.fn();

  renderPage(
    <ProgramDefinitionStudio
      tab="programs"
      programRoute={{ kind: "program", programType: "software_delivery" }}
      workflowRoute={{ kind: "library" }}
      onNavigateProgram={() => undefined}
      onNavigateWorkflow={onNavigateWorkflow}
      onChangeTab={onChangeTab}
    />
  );

  expect(await screen.findByRole("heading", { name: "Software delivery" })).toBeInTheDocument();
  expect(screen.getByText(/read-only map/i)).toBeInTheDocument();
  expect(screen.getByRole("list", { name: /program nodes/i })).toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: /open workflow/i }));
  expect(onNavigateWorkflow).toHaveBeenCalledWith({
    kind: "workflow",
    workflowType: "prd_request",
  });
});

it("shows breadcrumb back to referencing program definitions on workflow drill-down", async () => {
  mockProgramApis();

  renderPage(
    <ProgramDefinitionStudio
      tab="workflows"
      programRoute={{ kind: "library" }}
      workflowRoute={{ kind: "workflow", workflowType: "prd_request" }}
      onNavigateProgram={() => undefined}
      onNavigateWorkflow={() => undefined}
      onChangeTab={() => undefined}
    />
  );

  expect(await screen.findByRole("navigation", { name: /program definition breadcrumb/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Software delivery" })).toBeInTheDocument();
  expect(screen.getByText(/Workflow builder workflow:prd_request/)).toBeInTheDocument();
});

it("opens the editable builder for new program routes", async () => {
  mockProgramApis();

  renderPage(
    <ProgramDefinitionStudio
      tab="programs"
      programRoute={{ kind: "new" }}
      workflowRoute={{ kind: "library" }}
      onNavigateProgram={() => undefined}
      onNavigateWorkflow={() => undefined}
      onChangeTab={() => undefined}
    />
  );

  expect(await screen.findByRole("heading", { name: "New program" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /save draft/i })).toBeInTheDocument();
});

it("surfaces load errors for missing program definitions", async () => {
  mockFetch((url) => {
    if (url === "/admin/program-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/program-definitions/catalog") {
      return jsonResponse({
        workflows: [],
        adapters: [],
        transition_modes: ["manual", "automatic"],
        templates: [],
      });
    }
    if (url.startsWith("/admin/program-definitions/")) {
      return jsonResponse({ error: "missing" }, 404);
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(
    <ProgramDefinitionStudio
      tab="programs"
      programRoute={{ kind: "program", programType: "missing" }}
      workflowRoute={{ kind: "library" }}
      onNavigateProgram={() => undefined}
      onNavigateWorkflow={() => undefined}
      onChangeTab={() => undefined}
    />
  );

  expect(await screen.findByText(/could not load program definition/i)).toBeInTheDocument();
});
