import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { ProjectNotesPanel } from "./ProjectNotesPanel";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ProjectNotesPanel", () => {
  it("shows org rules and a promote action for an active project note", async () => {
    mockFetch((url) => {
      if (url === "/admin/notes") {
        return jsonResponse({
          notes: [{ id: "org", scope: "org", kind: "avoid", title: "No main", body: "Never commit to main.", status: "active", applies_to: [], use_count: 1, version: 1, why: "", evidence: [], source: "manual", proposal_action: "add", revision_of: null, base_version: null, promoted_from: null, project_id: null, exception_to: null }],
        });
      }
      return jsonResponse({
        notes: [{ id: "p", scope: "project", kind: "howto", title: "Routes", body: "Use asgi_app.py.", status: "active", applies_to: [], use_count: 0, version: 1, why: "", evidence: [], source: "manual", proposal_action: "add", revision_of: null, base_version: null, promoted_from: null, project_id: "proj", exception_to: null }],
        can_write: true,
      });
    });
    renderPage(<ProjectNotesPanel projectId="proj" canManage isAdmin={false} />);
    expect(await screen.findByText(/No main/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Promote Routes to org" })).toBeInTheDocument();
  });
});
