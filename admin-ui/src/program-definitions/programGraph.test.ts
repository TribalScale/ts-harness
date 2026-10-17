import { describe, expect, it } from "vitest";
import {
  definitionToProgramGraph,
  detectClientProgramGraphIssues,
  graphToProgramDefinition,
  programsReferencingWorkflow,
  updateNodeInGraph,
} from "./programGraph";
import type { ProgramDefinition } from "./types";

const softwareDelivery: ProgramDefinition = {
  program_type: "software_delivery",
  definition_version: "1.0.0",
  display_name: "Software delivery",
  startable_nodes: ["prd", "jira", "work_orders", "implement", "deploy"],
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
      produces_artifacts: ["jira_epic_key"],
      input_bindings: [],
      completion_adapter: "extract_jira_outputs",
      terminal: false,
    },
    {
      id: "work_orders",
      default_workflow_type: "work_orders_from_prd",
      workflow_variants: [],
      required_artifacts: ["approved_prd"],
      produces_artifacts: ["planning_track"],
      input_bindings: [],
      completion_adapter: "extract_work_orders_outputs",
      terminal: false,
    },
    {
      id: "implement",
      default_workflow_type: "implement_delivery",
      workflow_variants: [
        { workflow_type: "implement_work_orders", when: { planning_track: "work_orders" } },
      ],
      required_artifacts: [],
      produces_artifacts: ["pr_url"],
      input_bindings: [],
      completion_adapter: "extract_implement_outputs",
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
  edges: [
    {
      source: "prd",
      target: "jira",
      mode: "manual",
      confirm_required: true,
      signals: ["create jira", "jira"],
      artifact_patch: { planning_track: "jira" },
    },
    {
      source: "prd",
      target: "work_orders",
      mode: "manual",
      confirm_required: true,
      signals: ["work orders"],
      artifact_patch: { planning_track: "work_orders" },
    },
    {
      source: "jira",
      target: "implement",
      mode: "manual",
      signals: [],
      artifact_patch: {},
    },
    {
      source: "work_orders",
      target: "implement",
      mode: "automatic",
      signals: [],
      artifact_patch: {},
    },
    {
      source: "implement",
      target: "deploy",
      mode: "manual",
      signals: [],
      artifact_patch: {},
    },
  ],
};

describe("definitionToProgramGraph", () => {
  it("converts nodes with workflow bindings, artifacts, and terminal state", () => {
    const graph = definitionToProgramGraph(softwareDelivery);

    expect(graph.nodes).toHaveLength(5);
    const implement = graph.nodes.find((node) => node.id === "implement");
    expect(implement?.data.default_workflow_type).toBe("implement_delivery");
    expect(implement?.data.workflow_variants).toEqual([
      { workflow_type: "implement_work_orders", when: { planning_track: "work_orders" } },
    ]);
    expect(graph.nodes.find((node) => node.id === "deploy")?.data.terminal).toBe(true);
    expect(graph.nodes.find((node) => node.id === "jira")?.data.required_artifacts).toEqual([
      "approved_prd",
    ]);
  });

  it("preserves branching edges with mode and branch signals", () => {
    const graph = definitionToProgramGraph(softwareDelivery);
    const fromPrd = graph.edges.filter((edge) => edge.source === "prd");

    expect(fromPrd).toHaveLength(2);
    expect(fromPrd.map((edge) => edge.target).sort()).toEqual(["jira", "work_orders"]);
    expect(fromPrd.find((edge) => edge.target === "jira")?.data?.label).toContain("manual");
    expect(fromPrd.find((edge) => edge.target === "jira")?.data?.label).toContain("create jira");
    expect(graph.edges.find((edge) => edge.source === "work_orders")?.data?.mode).toBe("automatic");
  });

  it("uses editor layout metadata when provided", () => {
    const graph = definitionToProgramGraph(softwareDelivery, { prd: { x: 12, y: 34 } });
    expect(graph.nodes.find((node) => node.id === "prd")?.position).toEqual({ x: 12, y: 34 });
  });
});

describe("programsReferencingWorkflow", () => {
  it("returns every program definition that binds a workflow type", () => {
    const refs = programsReferencingWorkflow(
      [{ program_type: "software_delivery", display_name: "Software delivery", definition: softwareDelivery }],
      "implement_work_orders"
    );
    expect(refs).toEqual([
      { program_type: "software_delivery", display_name: "Software delivery" },
    ]);
  });
});

describe("graphToProgramDefinition", () => {
  it("round-trips nodes, edges, startability, and bindings without layout", () => {
    const graph = definitionToProgramGraph(softwareDelivery, { prd: { x: 9, y: 9 } });
    const next = graphToProgramDefinition(graph, softwareDelivery);
    expect(next).toEqual(softwareDelivery);
    expect(next).not.toHaveProperty("x-builder-layout");
  });
});

describe("detectClientProgramGraphIssues", () => {
  it("flags duplicate ids, missing endpoints, cycles, unreachable nodes, terminal edges, and absent startables", () => {
    const broken: ProgramDefinition = {
      program_type: "broken",
      definition_version: "0.1.0",
      startable_nodes: [],
      nodes: [
        {
          id: "a",
          default_workflow_type: "w",
          workflow_variants: [],
          required_artifacts: [],
          produces_artifacts: [],
          input_bindings: [],
          completion_adapter: "noop",
          terminal: true,
        },
        {
          id: "a",
          default_workflow_type: "w",
          workflow_variants: [],
          required_artifacts: [],
          produces_artifacts: [],
          input_bindings: [],
          completion_adapter: "noop",
          terminal: false,
        },
        {
          id: "b",
          default_workflow_type: "w",
          workflow_variants: [],
          required_artifacts: [],
          produces_artifacts: [],
          input_bindings: [],
          completion_adapter: "noop",
          terminal: false,
        },
        {
          id: "c",
          default_workflow_type: "w",
          workflow_variants: [],
          required_artifacts: [],
          produces_artifacts: [],
          input_bindings: [],
          completion_adapter: "noop",
          terminal: false,
        },
      ],
      edges: [
        { source: "b", target: "c", mode: "manual", signals: [] },
        { source: "c", target: "b", mode: "manual", signals: [] },
        { source: "a", target: "missing", mode: "manual", signals: [] },
        { source: "a", target: "b", mode: "manual", signals: [] },
      ],
    };
    const issues = detectClientProgramGraphIssues(definitionToProgramGraph(broken));
    const codes = new Set(issues.map((issue) => issue.code));
    expect(codes.has("duplicate_node")).toBe(true);
    expect(codes.has("missing_startable_nodes")).toBe(true);
    expect(codes.has("unknown_edge_endpoint")).toBe(true);
    expect(codes.has("cycle")).toBe(true);
    expect(codes.has("unreachable_node")).toBe(true);
    expect(codes.has("terminal_has_outgoing_edge")).toBe(true);
  });
});

describe("updateNodeInGraph", () => {
  it("renames a node and rewires edges and startability", () => {
    const graph = definitionToProgramGraph(softwareDelivery);
    const next = updateNodeInGraph(graph, "prd", {
      ...softwareDelivery.nodes[0],
      id: "intake",
      startable: true,
    });
    expect(next.nodes.some((node) => node.id === "intake")).toBe(true);
    expect(next.nodes.some((node) => node.id === "prd")).toBe(false);
    expect(next.edges.filter((edge) => edge.source === "intake")).toHaveLength(2);
    expect(next.nodes.find((node) => node.id === "intake")?.data.startable).toBe(true);
  });
});
