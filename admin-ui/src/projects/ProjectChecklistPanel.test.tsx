import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ProjectChecklistItem } from "../api/types";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { ProjectChecklistPanel } from "./ProjectChecklistPanel";

function item(over: Partial<ProjectChecklistItem>): ProjectChecklistItem {
  return {
    id: "x",
    title: "Item",
    status: "ok",
    summary: "Fine.",
    details: [],
    fix: "",
    fix_link: "",
    ...over,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ProjectChecklistPanel", () => {
  it("shows the summary, each item and how to fix, and re-checks", async () => {
    let calls = 0;
    mockFetch((url) => {
      expect(url).toBe("/admin/projects/p1/checklist");
      calls += 1;
      const allowed = calls > 1;
      const items = [
        item({
          id: "jira_allowed",
          title: "Jira project allowed",
          status: allowed ? "ok" : "failing",
          summary: allowed
            ? "PROJ is in the Jira filing allowlist."
            : "PROJ is not in Allowed projects.",
          fix: allowed ? "" : "Add PROJ to Allowed projects.",
          fix_link: allowed ? "" : "#/system/settings",
        }),
        item({
          id: "ci_on_pull_requests",
          title: "CI on pull requests",
          status: "warning",
          summary: "0 of 1 repositories run CI on pull requests.",
          details: ["acme/api: no workflow runs on pull requests into dev"],
          fix: "Add a workflow.",
        }),
        item({ id: "project_settings", title: "Project settings" }),
      ];
      return jsonResponse({
        project_id: "p1",
        items,
        ready: items.filter((i) => i.status === "ok").length,
        total: 3,
        summary: "",
      });
    });
    renderPage(<ProjectChecklistPanel projectId="p1" />);

    expect(await screen.findByText("1 of 3 ready")).toBeInTheDocument();
    const jira = document.querySelector('[data-check="jira_allowed"]') as HTMLElement;
    expect(within(jira).getByText("Failing")).toBeInTheDocument();
    expect(within(jira).getByText("Add PROJ to Allowed projects.")).toBeInTheDocument();
    expect(within(jira).getByRole("link", { name: "Open" })).toHaveAttribute(
      "href",
      "#/system/settings",
    );
    const ci = document.querySelector('[data-check="ci_on_pull_requests"]') as HTMLElement;
    expect(within(ci).getByText("Warning")).toBeInTheDocument();
    expect(
      within(ci).getByText("acme/api: no workflow runs on pull requests into dev"),
    ).toBeInTheDocument();
    expect(within(ci).queryByRole("link")).toBeNull();
    const settings = document.querySelector('[data-check="project_settings"]') as HTMLElement;
    expect(within(settings).getByText("Ready")).toBeInTheDocument();
    expect(within(settings).queryByText(/How to fix/)).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Re-check" }));
    expect(await screen.findByText("2 of 3 ready")).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.getByText("PROJ is in the Jira filing allowlist."),
      ).toBeInTheDocument(),
    );
    expect(calls).toBe(2);
  });

  it("shows an error when the checklist cannot load", async () => {
    mockFetch(() => jsonResponse({ error: "not found" }, 404));
    renderPage(<ProjectChecklistPanel projectId="p1" />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });
});
