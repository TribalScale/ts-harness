import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { parseHash } from "../App";
import { setPermissions, setRoles } from "../auth/session";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { bumpPatch, FragmentBuilderPage } from "./FragmentBuilderPage";

const definition = {
  fragment_id: "review_pair",
  definition_version: "1.0.0",
  display_name: "Review pair",
  params: { build_stage: { kind: "stage_ref", required: true } },
  inputs: { repo_url: { required: true } },
  stages: [{ name: "review", kind: "task", agent_id: "qa-agent", depends_on: ["{{param.build_stage}}"] }],
};

const draft = { id: "frag-draft-1", fragment_id: "review_pair", status: "draft", revision: "rev-1", definition };

function installDialogMethods() {
  HTMLDialogElement.prototype.showModal = vi.fn(function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  });
  HTMLDialogElement.prototype.close = vi.fn(function close(this: HTMLDialogElement) {
    this.removeAttribute("open");
  });
}

const ADMIN_PERMISSIONS = [
  "view",
  "start_program",
  "approve_gate",
  "confirm_delegation",
  "edit_config",
  "edit_models",
  "edit_allowlists",
];

beforeEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
  setPermissions(ADMIN_PERMISSIONS);
  installDialogMethods();
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation(() => ({
      matches: true,
      media: "",
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
});

it("lists fragments by status", async () => {
  mockFetch((url) => {
    if (url === "/admin/workflow-fragments") {
      return jsonResponse({
        fragments: [
          { fragment_id: "verified_merge", definition_version: "1.0.0", status: "built_in", stage_names: ["a", "b", "c"] },
          { id: "frag-draft-1", fragment_id: "review_pair", status: "draft" },
        ],
      });
    }
    throw new Error(`Unexpected URL ${url}`);
  });
  renderPage(<FragmentBuilderPage route={{ kind: "library" }} onNavigate={() => undefined} />);
  await userEvent.click(await screen.findByRole("tab", { name: /built-ins/i }));
  const builtIns = screen.getByRole("tabpanel", { name: /built-ins/i });
  expect(within(builtIns).getAllByText("verified_merge").length).toBeGreaterThan(0);
  expect(within(builtIns).getByText("3 stages")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("tab", { name: /drafts/i }));
  expect(within(screen.getByRole("tabpanel", { name: /drafts/i })).getAllByText("review_pair").length).toBeGreaterThan(0);
  expect(screen.getByRole("button", { name: "New fragment" })).toBeInTheDocument();
});

it("saves, validates and activates a fragment draft", async () => {
  const calls: Array<{ url: string; method: string; body?: any }> = [];
  mockFetch((url, init) => {
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    const method = init?.method || "GET";
    calls.push({ url, method, body });
    if (url === "/admin/workflow-fragments") return jsonResponse({ fragments: [] });
    if (url === "/admin/workflow-fragments/drafts/frag-draft-1") {
      if (method === "PUT") return jsonResponse({ draft: { ...draft, revision: "rev-2", definition: body.definition } });
      return jsonResponse({ draft });
    }
    if (url === "/admin/workflow-fragments/drafts/frag-draft-1/validate") return jsonResponse({ valid: true, issues: [] });
    if (url === "/admin/workflow-fragments/drafts/frag-draft-1/activate") return jsonResponse({ active: {} });
    throw new Error(`Unexpected URL ${url}`);
  });
  const onNavigate = vi.fn();
  renderPage(<FragmentBuilderPage route={{ kind: "draft", draftId: "frag-draft-1" }} onNavigate={onNavigate} />);

  await screen.findByRole("heading", { name: "Review pair" });
  await userEvent.click(screen.getByLabelText("Input repo_url required"));
  await userEvent.click(screen.getByRole("button", { name: "Validate" }));
  await waitFor(() =>
    expect(calls).toContainEqual(
      expect.objectContaining({
        url: "/admin/workflow-fragments/drafts/frag-draft-1",
        method: "PUT",
        body: expect.objectContaining({
          expected_revision: "rev-1",
          definition: expect.objectContaining({ inputs: { repo_url: { required: false } } }),
        }),
      })
    )
  );
  expect(await screen.findByText("Definition is valid.")).toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: "Activate" }));
  await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Activate fragment" }));
  await waitFor(() => expect(onNavigate).toHaveBeenCalledWith({ kind: "fragment", fragmentId: "review_pair" }));
  expect(calls).toContainEqual(
    expect.objectContaining({
      url: "/admin/workflow-fragments/drafts/frag-draft-1/activate",
      body: { expected_revision: "rev-2" },
    })
  );
});

it("hides mutations for non-admin roles and shows built-ins read-only", async () => {
  setRoles(["viewer"]);
  setPermissions(["view"]);
  mockFetch((url) => {
    if (url === "/admin/workflow-fragments") return jsonResponse({ fragments: [] });
    if (url === "/admin/workflow-fragments/review_pair") {
      return jsonResponse({ status: "active", read_only: false, definition });
    }
    throw new Error(`Unexpected URL ${url}`);
  });
  renderPage(<FragmentBuilderPage route={{ kind: "fragment", fragmentId: "review_pair" }} onNavigate={() => undefined} />);
  await screen.findByRole("heading", { name: "Review pair" });
  expect(screen.queryByRole("button", { name: "Save draft" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "New version draft" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "New fragment" })).not.toBeInTheDocument();
  expect(screen.getByLabelText("Fragment id")).toBeDisabled();
});

it("hides the fragment editor when the server grants no permissions", async () => {
  setRoles([]);
  setPermissions([]);
  mockFetch((url) => {
    if (url === "/admin/workflow-fragments/review_pair") {
      return jsonResponse({ status: "active", read_only: false, definition });
    }
    throw new Error(`Unexpected URL ${url}`);
  });
  renderPage(<FragmentBuilderPage route={{ kind: "fragment", fragmentId: "review_pair" }} onNavigate={() => undefined} />);
  await screen.findByRole("heading", { name: "Review pair" });
  expect(screen.queryByRole("button", { name: "Save draft" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "New fragment" })).not.toBeInTheDocument();
});

it("routes fragment hashes and bumps versions", () => {
  expect(parseHash("#/workflows/fragments")).toMatchObject({ studioTab: "fragments", fragmentRoute: { kind: "library" } });
  expect(parseHash("#/workflows/fragments/new").fragmentRoute).toEqual({ kind: "new" });
  expect(parseHash("#/workflows/fragments/drafts/d%201").fragmentRoute).toEqual({ kind: "draft", draftId: "d 1" });
  expect(parseHash("#/workflows/fragments/verified_merge").fragmentRoute).toEqual({
    kind: "fragment",
    fragmentId: "verified_merge",
  });
  expect(bumpPatch("1.2.3")).toBe("1.2.4");
  expect(bumpPatch("draft")).toBe("draft.1");
});
