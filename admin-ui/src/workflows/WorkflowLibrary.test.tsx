import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { WorkflowLibrary } from "./WorkflowLibrary";

it("loads draft, active, and built-in workflow definitions with actions", async () => {
  const onSelect = vi.fn();
  const onNew = vi.fn();
  const onClone = vi.fn();
  mockFetch((url) => {
    if (url === "/admin/workflow-definitions") {
      return jsonResponse({
        definitions: [
          {
            id: "draft-1",
            workflow_type: "ship_feature",
            definition_version: "1.0.0",
            display_name: "Ship feature draft",
            status: "draft",
            revision: "rev-1",
          },
          {
            workflow_type: "custom_release",
            definition_version: "2.0.0",
            display_name: "Custom release",
            status: "active",
            read_only: false,
          },
          {
            workflow_type: "jira_from_prd",
            definition_version: "1.1.0",
            display_name: "Jira from PRD",
            status: "built_in",
            read_only: true,
          },
        ],
      });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(
    <WorkflowLibrary
      selected={{ kind: "workflow", workflowType: "custom_release" }}
      onNew={onNew}
      onSelect={onSelect}
      onClone={onClone}
    />
  );

  expect(await screen.findByText("Custom release")).toBeInTheDocument();

  await userEvent.click(screen.getByRole("tab", { name: /drafts/i }));
  expect(screen.getByText("Ship feature draft")).toBeInTheDocument();

  await userEvent.click(screen.getByRole("tab", { name: /built-ins/i }));
  expect(screen.getByText("Jira from PRD")).toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: /new workflow/i }));
  expect(onNew).toHaveBeenCalled();

  await userEvent.click(screen.getByRole("tab", { name: /drafts/i }));
  await userEvent.click(screen.getByRole("button", { name: /^edit$/i }));
  expect(onSelect).toHaveBeenCalledWith({ kind: "draft", draftId: "draft-1" });

  await userEvent.click(screen.getByRole("tab", { name: /built-ins/i }));
  await userEvent.click(screen.getByRole("button", { name: /^clone jira_from_prd$/i }));
  expect(onClone).toHaveBeenCalledWith(
    expect.objectContaining({ workflow_type: "jira_from_prd", status: "built_in" })
  );
});

it("shows an empty state when no definitions exist", async () => {
  mockFetch((url) => {
    if (url === "/admin/workflow-definitions") return jsonResponse({ definitions: [] });
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(
    <WorkflowLibrary
      selected={{ kind: "library" }}
      onNew={() => undefined}
      onSelect={() => undefined}
      onClone={() => undefined}
    />
  );

  await waitFor(() => expect(screen.getByText(/no workflow definitions/i)).toBeInTheDocument());
});
