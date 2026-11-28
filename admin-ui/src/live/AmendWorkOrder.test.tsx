import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setPermissions } from "../auth/session";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { AmendWorkOrder } from "./AmendWorkOrder";

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("Amend work order", () => {
  it("appends an amendment and lists the earlier ones", async () => {
    const user = userEvent.setup();
    const posted: string[] = [];
    let amendments = [{ id: "a1", text: "Toolbar belongs to PROJ-13.", person: "@dana", source: "pr_comment", date: "2026-11-19", at: "" }];
    mockFetch((url, init) => {
      if (url === "/admin/workflows/amend") {
        posted.push(String(init?.body));
        amendments = [...amendments, { id: "a2", text: "Rows belong to PROJ-12.", person: "lee@example.com", source: "console", date: "2026-11-20", at: "" }];
        return jsonResponse({ status: "amended" });
      }
      if (url === "/admin/workflows/wf-1/amendments") {
        return jsonResponse({ workflow_id: "wf-1", original: "", current: "", amendments });
      }
      return jsonResponse({});
    });
    setPermissions(["view", "approve_gate"]);
    renderPage(<AmendWorkOrder workflowId="wf-1" label="PROJ-11" />);
    await user.click(screen.getByRole("button", { name: "Amend work order" }));
    expect(await screen.findByText("Toolbar belongs to PROJ-13.")).toBeInTheDocument();
    expect(screen.getByText(/@dana · 2026-11-19 · from a PR comment/)).toBeInTheDocument();
    const submit = screen.getByRole("button", { name: "Add amendment" });
    expect(submit).toBeDisabled();
    await user.type(screen.getByRole("textbox"), "Rows belong to PROJ-12.");
    await user.click(submit);
    expect(posted[0]).toBe(JSON.stringify({ workflow_id: "wf-1", text: "Rows belong to PROJ-12." }));
    expect(await screen.findByRole("status")).toHaveTextContent("Amended PROJ-11.");
    expect(await screen.findByText("Rows belong to PROJ-12.")).toBeInTheDocument();
  });

  it("shows the server's refusal", async () => {
    const user = userEvent.setup();
    mockFetch((url) => {
      if (url === "/admin/workflows/amend") return jsonResponse({ error: "only a running work order can be amended" }, 409);
      return jsonResponse({ workflow_id: "wf-1", original: "", current: "", amendments: [] });
    });
    setPermissions(["view", "approve_gate"]);
    renderPage(<AmendWorkOrder workflowId="wf-1" label="PROJ-11" />);
    await user.click(screen.getByRole("button", { name: "Amend work order" }));
    await user.type(screen.getByRole("textbox"), "x");
    await user.click(screen.getByRole("button", { name: "Add amendment" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("only a running work order can be amended");
  });

  it("is hidden without the decision permission", () => {
    setPermissions(["view"]);
    renderPage(<AmendWorkOrder workflowId="wf-1" label="PROJ-11" />);
    expect(screen.queryByRole("button", { name: "Amend work order" })).toBeNull();
  });
});
