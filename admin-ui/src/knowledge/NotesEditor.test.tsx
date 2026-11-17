import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { NotesEditor } from "./NotesEditor";

const NOTE = {
  id: "n1",
  scope: "org",
  project_id: null,
  kind: "avoid",
  title: "PR titles",
  body: "Start with the Jira key.",
  why: "Reviewers look for it.",
  applies_to: [],
  exception_to: null,
  status: "active",
  version: 1,
  evidence: [],
  source: "manual",
  proposal_action: "add",
  revision_of: null,
  base_version: null,
  promoted_from: null,
  use_count: 4,
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe("NotesEditor", () => {
  it("lists notes and reloads when a save conflicts", async () => {
    let calls = 0;
    mockFetch((url, init) => {
      const method = (init?.method || "GET").toUpperCase();
      if (method === "PATCH") return jsonResponse({ error: "version conflict" }, 409);
      calls += 1;
      return jsonResponse({ notes: [NOTE], can_write: true });
    });
    renderPage(<NotesEditor listUrl="/admin/notes" canWrite showUseCount />);
    expect(await screen.findByText("[avoid] PR titles")).toBeInTheDocument();
    expect(screen.getByText("Used by 4 runs")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Changed by someone else");
    await waitFor(() => expect(calls).toBeGreaterThan(1));
  });
});
