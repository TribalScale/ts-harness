import { describe, expect, it } from "vitest";
import { definitionToGraph, detectClientGraphIssues, graphToDefinition, renameStage } from "./graph";
import type { WorkflowDefinition } from "./types";

const baseDefinition: WorkflowDefinition = {
  workflow_type: "ship_feature",
  definition_version: "1.0.0",
  display_name: "Ship feature",
  notifications: { bound_channel: true },
  promotion: { intake_stage: "design" },
  custom_top_level: { keep: true },
  stages: [
    {
      name: "design",
      kind: "task",
      agent_id: "pm-agent",
      contract_ref: "design_contract",
      input_map: { approved_prd: "$workflow.approved_prd" },
      unsupported_stage_field: "preserve me",
    },
    {
      name: "approve_design",
      kind: "task",
      agent_id: "orchestrator",
      approval_required: true,
      depends_on: ["design"],
    },
  ],
};

describe("workflow graph conversion", () => {
  it("uses draft editor metadata layout without reading runtime definition JSON", () => {
    const graph = definitionToGraph(
      {
        ...baseDefinition,
        "x-builder-layout": { design: { x: 999, y: 999 } },
      },
      { design: { x: 120, y: 240 } }
    );

    expect(graph.nodes[0].position).toEqual({ x: 120, y: 240 });
    expect(graph.layout.design).toEqual({ x: 120, y: 240 });
  });

  it("round-trips definitions without leaking canvas layout into runtime JSON", () => {
    const graph = definitionToGraph(baseDefinition);
    graph.nodes[0].position = { x: 400, y: 800 };

    const next = graphToDefinition(graph, baseDefinition);

    expect(next.stages[1].depends_on).toEqual(["design"]);
    expect(next.stages[0].input_map).toEqual({ approved_prd: "$workflow.approved_prd" });
    expect(next.stages[0].unsupported_stage_field).toBe("preserve me");
    expect(next.custom_top_level).toEqual({ keep: true });
    expect(JSON.stringify(next)).not.toContain("position");
    expect(graph.layout.design).toEqual({ x: 0, y: 0 });
  });

  it("round-trips wait, revise payloads, session, and contracts for PRD-shaped graphs", () => {
    const prdShaped: WorkflowDefinition = {
      workflow_type: "prd_shaped_ui",
      definition_version: "1.0.0",
      display_name: "PRD shaped",
      contracts: {
        prd_draft: {
          output_schema: { type: "object", required: ["prd_markdown"] },
        },
      },
      session: {
        artifact_steps: ["draft_prd"],
        phase_tracking: {
          intake_step: "intake",
          draft_steps: ["draft_prd"],
          gate_steps: ["approve_prd"],
          approved_value: "approved",
        },
        on_gate_complete_hook: "mark_artifact_approved",
      },
      notifications: {
        bound_channel: true,
        events: ["completed", "awaiting_approval", "awaiting_external"],
      },
      on_workflow_complete: { hook: "complete_program_phase" },
      stages: [
        {
          name: "intake",
          kind: "task",
          agent_id: "pm-agent",
          present_prompt: "intake",
        },
        {
          name: "await_intake_answers",
          kind: "task",
          agent_id: "orchestrator",
          depends_on: ["intake"],
          wait_external: { event_type: "channel.intake_response" },
          on_complete: { hook: "hydrate_input", downstream: ["draft_prd"] },
        },
        {
          name: "draft_prd",
          kind: "task",
          agent_id: "pm-agent",
          depends_on: ["await_intake_answers"],
          present_prompt: "approval",
          contract_ref: "prd_draft",
        },
        {
          name: "approve_prd",
          kind: "task",
          agent_id: "orchestrator",
          depends_on: ["draft_prd"],
          approval_required: true,
          on_reject: {
            hook: "revise_on_reject",
            revise: { step_name: "revise_prd", agent_id: "pm-agent" },
            re_gate: { step_name: "approve_prd", approval_required: true },
          },
        },
      ],
    };

    const next = graphToDefinition(definitionToGraph(prdShaped), prdShaped);
    expect(next.session?.artifact_steps).toEqual(["draft_prd"]);
    expect(next.contracts?.prd_draft).toBeTruthy();
    expect(next.stages[1].wait_external).toEqual({ event_type: "channel.intake_response" });
    expect(next.stages[1].on_complete).toEqual({
      hook: "hydrate_input",
      downstream: ["draft_prd"],
    });
    expect(next.stages[3].on_reject?.revise).toEqual({
      step_name: "revise_prd",
      agent_id: "pm-agent",
    });
    expect(next.on_workflow_complete).toEqual({ hook: "complete_program_phase" });
  });

  it("renames dependency references and rejects duplicate names", () => {
    const graph = definitionToGraph(baseDefinition);

    expect(() => renameStage(graph, "design", "approve_design")).toThrow(/already exists/i);

    const renamed = renameStage(graph, "design", "technical_design");
    const next = graphToDefinition(renamed, baseDefinition);

    expect(next.stages.map((stage) => stage.name)).toEqual(["technical_design", "approve_design"]);
    expect(next.stages[1].depends_on).toEqual(["technical_design"]);
  });

  it("detects duplicate stages, missing dependencies, and cycles", () => {
    const graph = definitionToGraph(baseDefinition);
    graph.nodes.push({ ...graph.nodes[0], id: "duplicate", data: { ...graph.nodes[0].data, name: "design" } });
    graph.edges.push({ id: "missing", source: "missing_stage", target: "design" });
    graph.edges.push({ id: "cycle", source: "approve_design", target: "design" });

    const issues = detectClientGraphIssues(graph);

    expect(issues.map((issue) => issue.code)).toEqual(
      expect.arrayContaining(["duplicate_stage_name", "missing_dependency", "cycle"])
    );
  });

  it("reports missing stage agent IDs before saving", () => {
    const graph = definitionToGraph({
      ...baseDefinition,
      stages: [{ name: "draft_plan", kind: "task", agent_id: "" }],
    });

    expect(detectClientGraphIssues(graph)).toContainEqual(
      expect.objectContaining({
        code: "missing_agent_id",
        path: "/stages/0/agent_id",
      })
    );
  });
});
