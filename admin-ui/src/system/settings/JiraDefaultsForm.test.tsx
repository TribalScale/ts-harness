import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { renderPage } from "../../test/render";
import { JiraDefaultsForm } from "./JiraDefaultsForm";
import type { RuntimeSettingsDefinition } from "./types";

const merged = {
  program_defaults: {
    repo_picker: { repos: [{ id: "org/yaml", default_branch: "main" }] },
    jira_filing: { default_issue_type: "Story", issue_type_by_project: { ENG: "Bug" } },
    ticket_picker: { statuses: ["To Do"] },
  },
};

const emptyDefinition: RuntimeSettingsDefinition = {
  definition_type: "runtime_settings",
  definition_version: "1.0.0",
};

describe("JiraDefaultsForm", () => {
  it("shows overlay Remove after adding to inherited lists", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const { rerender } = renderPage(
      <JiraDefaultsForm
        merged={merged}
        sources={{
          "program_defaults.repo_picker": "inherited",
          "program_defaults.jira_filing": "inherited",
          "program_defaults.ticket_picker": "inherited",
        }}
        definition={emptyDefinition}
        onChange={onChange}
      />
    );
    expect(screen.getAllByText("Source: inherited")).toHaveLength(3);
    expect(screen.queryByRole("button", { name: "Remove" })).not.toBeInTheDocument();

    await user.type(screen.getByLabelText("Repo id"), "org/checkout-service");
    await user.click(screen.getByRole("button", { name: "Add repo" }));
    const afterRepo = onChange.mock.calls.at(-1)?.[0] as RuntimeSettingsDefinition;
    rerender(
      <JiraDefaultsForm
        merged={merged}
        sources={{
          "program_defaults.repo_picker": "inherited",
          "program_defaults.jira_filing": "inherited",
          "program_defaults.ticket_picker": "inherited",
        }}
        definition={afterRepo}
        onChange={onChange}
      />
    );
    expect(screen.getByText("Source: overlay")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Remove" }).length).toBeGreaterThan(0);

    await user.type(screen.getByLabelText("Filing project"), "OPS");
    await user.type(screen.getByLabelText("Filing type"), "Task");
    await user.click(screen.getByRole("button", { name: "Add project type" }));
    const afterFiling = onChange.mock.calls.at(-1)?.[0] as RuntimeSettingsDefinition;
    rerender(
      <JiraDefaultsForm
        merged={merged}
        sources={{
          "program_defaults.repo_picker": "inherited",
          "program_defaults.jira_filing": "inherited",
          "program_defaults.ticket_picker": "inherited",
        }}
        definition={afterFiling}
        onChange={onChange}
      />
    );
    expect(screen.getByText("OPS")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Remove project OPS" }));
    const removedFiling = onChange.mock.calls.at(-1)?.[0] as RuntimeSettingsDefinition;
    expect(removedFiling.program_defaults?.jira_filing?.issue_type_by_project?.OPS).toBeUndefined();

    await user.type(screen.getByLabelText("Add status"), "Blocked");
    await user.click(screen.getByRole("button", { name: "Add status" }));
    const afterStatus = onChange.mock.calls.at(-1)?.[0] as RuntimeSettingsDefinition;
    rerender(
      <JiraDefaultsForm
        merged={merged}
        sources={{
          "program_defaults.repo_picker": "inherited",
          "program_defaults.jira_filing": "inherited",
          "program_defaults.ticket_picker": "inherited",
        }}
        definition={afterStatus}
        onChange={onChange}
      />
    );
    expect(screen.getByText("Blocked")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Remove status Blocked" }));
    const removedStatus = onChange.mock.calls.at(-1)?.[0] as RuntimeSettingsDefinition;
    expect(removedStatus.program_defaults?.ticket_picker?.statuses).not.toContain("Blocked");
  });
});
