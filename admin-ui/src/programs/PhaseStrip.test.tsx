import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { renderPage } from "../test/render";
import { PhaseStrip } from "./PhaseStrip";

const branchedTopology = {
  program_type: "custom_delivery",
  definition_version: "2.0.0",
  nodes: [
    { id: "intake", default_workflow_type: "prd_request" },
    { id: "plan_a", default_workflow_type: "jira_from_prd" },
    { id: "plan_b", default_workflow_type: "work_orders_from_prd" },
    { id: "build", default_workflow_type: "implement_delivery", terminal: true },
  ],
  edges: [
    { source: "intake", target: "plan_a", mode: "manual" },
    { source: "intake", target: "plan_b", mode: "manual" },
    { source: "plan_a", target: "build", mode: "manual" },
    { source: "plan_b", target: "build", mode: "automatic" },
  ],
};

const phases = {
  intake: { state: "succeeded", workflow_type: "prd_request" },
  plan_a: { state: "running", workflow_type: "jira_from_prd", active_step: "Create epic" },
  plan_b: { state: "not_started" },
  build: { state: "not_started" },
};

const resolved = {
  intake: { workflow_type: "prd_request", definition_version: "1.0.0" },
  plan_a: { workflow_type: "jira_from_prd", definition_version: "1.0.0" },
  plan_b: { workflow_type: "work_orders_from_prd", definition_version: "1.1.0" },
  build: { workflow_type: "implement_delivery", definition_version: "2.0.0" },
};

it("renders the selected branch from phase_order without hardcoded jira/work_orders logic", () => {
  renderPage(
    <PhaseStrip
      phases={phases}
      phaseOrder={["intake", "plan_a", "build"]}
      topology={branchedTopology}
      resolvedWorkflowVersions={resolved}
      currentPhase="plan_a"
      onSelect={() => undefined}
    />
  );

  const strip = screen.getByRole("list", { name: /program phases/i });
  expect(strip).toHaveTextContent(/intake/i);
  expect(strip).toHaveTextContent(/plan a/i);
  expect(strip).toHaveTextContent(/build/i);
  expect(strip).not.toHaveTextContent(/plan b/i);
  expect(screen.queryByRole("button", { name: /^jira$/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /work orders/i })).not.toBeInTheDocument();
  expect(screen.getByText("prd_request@1.0.0")).toBeInTheDocument();
  expect(screen.getByText("jira_from_prd@1.0.0")).toBeInTheDocument();
});

it("exposes alternate nodes and edges in an expandable topology view", async () => {
  const onSelect = vi.fn();
  renderPage(
    <PhaseStrip
      phases={phases}
      phaseOrder={["intake", "plan_a", "build"]}
      topology={branchedTopology}
      resolvedWorkflowVersions={resolved}
      currentPhase="plan_a"
      onSelect={onSelect}
    />
  );

  const expander = screen.getByText(/full topology/i).closest("details");
  expect(expander).not.toBeNull();
  expect(expander).not.toHaveAttribute("open");
  await userEvent.click(screen.getByText(/full topology/i));
  expect(expander).toHaveAttribute("open");

  const topology = screen.getByLabelText(/full program topology/i);
  expect(topology).toHaveTextContent(/plan b/i);
  expect(topology).toHaveTextContent(/work_orders_from_prd@1.1.0/);
  expect(topology).toHaveTextContent("intake → plan_b");
  expect(topology).toHaveTextContent(/alternate/i);

  await userEvent.click(
    within(topology).getByRole("button", { name: /plan b/i })
  );
  expect(onSelect).toHaveBeenCalledWith("plan_b");
});

it("falls back to payload phase keys when phase_order is absent", () => {
  renderPage(
    <PhaseStrip
      phases={{
        alpha: { state: "succeeded" },
        beta: { state: "running" },
      }}
      currentPhase="beta"
      onSelect={() => undefined}
    />
  );

  expect(screen.getByRole("button", { name: /1alphasucceeded/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /2betarunning/i })).toBeInTheDocument();
});
