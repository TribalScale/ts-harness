import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { renderPage } from "../test/render";
import { ProgramInspector } from "./ProgramInspector";
import type { ProgramDefinition, ProgramDefinitionCatalog, ProgramNode } from "./types";

const catalog: ProgramDefinitionCatalog = {
  workflows: [{ workflow_type: "prd_request" }, { workflow_type: "deploy_release" }],
  adapters: [
    { id: "extract_prd_outputs", label: "extract_prd_outputs" },
    { id: "extract_deploy_outputs", label: "extract_deploy_outputs" },
  ],
  transition_modes: ["manual", "automatic"],
  templates: [],
};

const node: ProgramNode & { startable?: boolean } = {
  id: "prd",
  default_workflow_type: "prd_request",
  workflow_variants: [],
  required_artifacts: [],
  produces_artifacts: ["approved_prd"],
  input_bindings: [],
  completion_adapter: "extract_prd_outputs",
  terminal: false,
  startable: true,
};

const definition: ProgramDefinition = {
  program_type: "software_delivery",
  definition_version: "1.0.0",
  display_name: "Software delivery",
  startable_nodes: ["prd"],
  nodes: [node],
  edges: [],
};

it("updates program, node, and edge fields through callbacks", async () => {
  const onProgramChange = vi.fn();
  const onNodeChange = vi.fn();
  const onEdgeChange = vi.fn();

  renderPage(
    <ProgramInspector
      definition={definition}
      selectedNode={node}
      selectedEdge={{
        id: "prd->deploy:manual",
        source: "prd",
        target: "deploy",
        mode: "manual",
        signals: ["go"],
        artifact_patch: {},
      }}
      catalog={catalog}
      readOnly={false}
      onProgramChange={onProgramChange}
      onNodeChange={onNodeChange}
      onEdgeChange={onEdgeChange}
    />
  );

  await userEvent.clear(screen.getByLabelText("Display name"));
  await userEvent.type(screen.getByLabelText("Display name"), "Delivery map");
  expect(onProgramChange).toHaveBeenLastCalledWith(
    expect.objectContaining({ display_name: "Delivery map" })
  );

  await userEvent.click(screen.getByLabelText("Terminal"));
  expect(onNodeChange).toHaveBeenLastCalledWith(expect.objectContaining({ terminal: true }));

  await userEvent.selectOptions(screen.getByLabelText("Transition mode"), "automatic");
  expect(onEdgeChange).toHaveBeenLastCalledWith(expect.objectContaining({ mode: "automatic" }));

  expect((screen.getByLabelText("Current definition JSON") as HTMLTextAreaElement).value).toContain(
    "software_delivery"
  );
});

it("draft can switch repo section to custom and add a repo", async () => {
  const onProgramChange = vi.fn();
  renderPage(
    <ProgramInspector
      definition={definition}
      catalog={catalog}
      readOnly={false}
      onProgramChange={onProgramChange}
      onNodeChange={vi.fn()}
      onEdgeChange={vi.fn()}
    />
  );

  await userEvent.click(screen.getByLabelText("Repo allowlist custom"));
  expect(onProgramChange).toHaveBeenLastCalledWith(
    expect.objectContaining({
      config: expect.objectContaining({
        repo_picker: expect.objectContaining({ mode: "custom" }),
      }),
    })
  );

  await userEvent.type(screen.getByLabelText("Program repo id"), "org/checkout-service");
  await userEvent.click(screen.getByRole("button", { name: "Add repo" }));
  expect(onProgramChange).toHaveBeenLastCalledWith(
    expect.objectContaining({
      config: expect.objectContaining({
        repo_picker: expect.objectContaining({
          mode: "custom",
          repos: [expect.objectContaining({ id: "org/checkout-service" })],
        }),
      }),
    })
  );
});

it("can remove a custom repo row", async () => {
  const onProgramChange = vi.fn();
  renderPage(
    <ProgramInspector
      definition={{
        ...definition,
        config: { repo_picker: { mode: "custom", repos: [{ id: "org/custom" }] } },
      }}
      catalog={catalog}
      readOnly={false}
      onProgramChange={onProgramChange}
      onNodeChange={vi.fn()}
      onEdgeChange={vi.fn()}
    />
  );

  await userEvent.click(screen.getByRole("button", { name: "Remove" }));
  expect(onProgramChange).toHaveBeenLastCalledWith(
    expect.objectContaining({
      config: expect.objectContaining({
        repo_picker: expect.objectContaining({
          mode: "custom",
          repos: [],
        }),
      }),
    })
  );
});

it("writes mode inherit when switching a custom section back to tenant defaults", async () => {
  const onProgramChange = vi.fn();
  renderPage(
    <ProgramInspector
      definition={{
        ...definition,
        config: { repo_picker: { mode: "custom", repos: [{ id: "org/custom" }] } },
      }}
      catalog={catalog}
      readOnly={false}
      onProgramChange={onProgramChange}
      onNodeChange={vi.fn()}
      onEdgeChange={vi.fn()}
    />
  );

  await userEvent.click(screen.getByLabelText("Repo allowlist use tenant defaults"));
  expect(onProgramChange).toHaveBeenLastCalledWith(
    expect.objectContaining({
      config: expect.objectContaining({
        repo_picker: { mode: "inherit" },
      }),
    })
  );
});

it("does not let built-in programs change inherit toggles", async () => {
  renderPage(
    <ProgramInspector
      definition={definition}
      catalog={catalog}
      readOnly
      onProgramChange={vi.fn()}
      onNodeChange={vi.fn()}
      onEdgeChange={vi.fn()}
    />
  );

  expect(screen.getByLabelText("Repo allowlist custom")).toBeDisabled();
  expect(screen.getByRole("link", { name: "Edit tenant defaults in Settings" })).toHaveAttribute(
    "href",
    "#/system/settings"
  );
  expect(screen.getByRole("link", { name: "Edit in Settings → Routing" })).toHaveAttribute(
    "href",
    "#/system/settings/routing"
  );
  expect(screen.queryByLabelText("Program repo id")).not.toBeInTheDocument();
});

it("links to scheduling the program in System Cron", () => {
  renderPage(
    <ProgramInspector
      definition={{ ...definition, program_type: "account_digest" }}
      selectedNode={node}
      selectedEdge={null}
      catalog={catalog}
      readOnly
      onProgramChange={vi.fn()}
      onNodeChange={vi.fn()}
      onEdgeChange={vi.fn()}
    />
  );
  expect(screen.getByRole("link", { name: "Schedule this program" })).toHaveAttribute(
    "href",
    "#/system/cron?attachProgram=account_digest"
  );
});
