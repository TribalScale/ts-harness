import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { renderPage } from "../test/render";
import { WorkflowInspector } from "./WorkflowInspector";
import type { WorkflowDefinitionCatalog, WorkflowStage } from "./types";

const catalog: WorkflowDefinitionCatalog = {
  hooks: ["hydrate_input", "complete_program_phase", "revise_on_reject", "compensate_on_failure"],
  agents: [
    { id: "pm-agent", label: "PM Agent" },
    { id: "comms-agent", label: "Comms Agent" },
  ],
  agent_tools: {
    "pm-agent": ["google_workspace_docs_append", "read_file", "read_skill_reference"],
    "comms-agent": ["channel_send", "read_file"],
  },
  skills: [{ id: "prd-to-work-orders-governed", label: "Work orders" }],
  stage_kinds: ["task", "expand", "compensate"],
  present_prompts: ["approval", "inform", "intake", "merge", "none"],
  semantic_validators: ["validate_qa_pr_url_set"],
  templates: [],
};

const stage: WorkflowStage = {
  name: "draft_plan",
  kind: "task",
  agent_id: "pm-agent",
  skill_hints: ["prd-to-work-orders-governed"],
  input_map: { approved_prd: "$workflow.approved_prd" },
  retry: { max_retries: 1, timeout_seconds: 120 },
};

it("updates workflow and selected stage fields through callbacks", async () => {
  const onWorkflowChange = vi.fn();
  const onStageChange = vi.fn();

  renderPage(
    <WorkflowInspector
      definition={{
        workflow_type: "ship_feature",
        definition_version: "1.0.0",
        display_name: "Ship feature",
        notifications: { bound_channel: true },
        promotion: { intake_stage: "draft_plan" },
        stages: [stage],
      }}
      selectedStage={stage}
      catalog={catalog}
      readOnly={false}
      onWorkflowChange={onWorkflowChange}
      onStageChange={onStageChange}
    />
  );

  await userEvent.clear(screen.getByLabelText("Display name"));
  await userEvent.type(screen.getByLabelText("Display name"), "Ship faster");
  expect(onWorkflowChange).toHaveBeenLastCalledWith(expect.objectContaining({ display_name: "Ship faster" }));

  await userEvent.click(screen.getByLabelText("Approval required"));
  expect(onStageChange).toHaveBeenLastCalledWith(expect.objectContaining({ approval_required: true }));

  await userEvent.clear(screen.getByLabelText("Contract ref"));
  await userEvent.type(screen.getByLabelText("Contract ref"), "updated_contract");
  expect(onStageChange).toHaveBeenLastCalledWith(expect.objectContaining({ contract_ref: "updated_contract" }));

  expect((screen.getByLabelText("Current definition JSON") as HTMLTextAreaElement).value).toContain(
    "ship_feature"
  );

  await userEvent.selectOptions(screen.getByLabelText("Present prompt"), "approval");
  expect(onStageChange).toHaveBeenLastCalledWith(expect.objectContaining({ present_prompt: "approval" }));

  await userEvent.selectOptions(screen.getByLabelText("on_complete"), "hydrate_input");
  expect(onStageChange).toHaveBeenLastCalledWith(
    expect.objectContaining({ on_complete: expect.objectContaining({ hook: "hydrate_input" }) })
  );
});

it("writes tool_allow when restricted and removes the key when unrestricted", async () => {
  const onStageChange = vi.fn();

  renderPage(
    <WorkflowInspector
      definition={{ workflow_type: "ship_feature", definition_version: "1.0.0", stages: [stage] }}
      selectedStage={stage}
      catalog={catalog}
      readOnly={false}
      onWorkflowChange={vi.fn()}
      onStageChange={onStageChange}
    />
  );

  expect(screen.getByLabelText("Restrict tools")).not.toBeChecked();
  expect(screen.queryByLabelText("Stage tool read_file")).not.toBeInTheDocument();

  await userEvent.click(screen.getByLabelText("Restrict tools"));
  expect(onStageChange).toHaveBeenLastCalledWith(expect.objectContaining({ tool_allow: [] }));
  expect(screen.queryByLabelText("Stage tool channel_send")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("Stage tool read_skill_reference")).not.toBeInTheDocument();

  await userEvent.click(screen.getByLabelText("Stage tool google_workspace_docs_append"));
  await userEvent.click(screen.getByLabelText("Stage tool read_file"));
  expect(onStageChange).toHaveBeenLastCalledWith(
    expect.objectContaining({ tool_allow: ["google_workspace_docs_append", "read_file"] })
  );

  await userEvent.selectOptions(screen.getByLabelText("Agent"), "comms-agent");
  expect(onStageChange).toHaveBeenLastCalledWith(
    expect.objectContaining({ agent_id: "comms-agent", tool_allow: ["read_file"] })
  );

  await userEvent.click(screen.getByLabelText("Restrict tools"));
  const last = onStageChange.mock.calls.at(-1)?.[0] as WorkflowStage;
  expect("tool_allow" in last).toBe(false);
  expect(last.agent_id).toBe("comms-agent");
});
