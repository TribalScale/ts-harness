import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { findAlert } from "../test/alerts";
import { MemoryPage } from "./MemoryPage";

function installMediaQuery(desktop: boolean) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: query.includes("min-width: 1024px") ? desktop : !desktop,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
}

const NOTE = {
  id: "n1",
  tenant_id: "t1",
  scope: "project",
  project_id: "p1",
  project_key: "PROJ",
  kind: "avoid",
  title: "Run migrations before tests",
  body: "The api test suite needs the schema migrated first.",
  why: "Tests fail on a fresh database otherwise.",
  applies_to: ["org/api", "tests"],
  exception_to: null,
  status: "proposed",
  version: 2,
  evidence: [
    { type: "drafted_from", ref: "run wf-42", url: "https://github.com/org/api/pull/7", excerpt: "migrations missing" },
    { type: "pr_comment", ref: "org/api#7", url: "local-ref", excerpt: "please migrate first" },
  ],
  source: "drafted",
  proposal_action: "add",
  revision_of: null,
  base_version: null,
  promoted_from: null,
  proposed_by: "agent",
  approved_by: null,
  created_at: "2026-11-20T10:00:00Z",
  updated_at: "2026-11-21T10:00:00Z",
  last_used_at: null,
  use_count: 3,
  can_write: true,
};

const ORG_NOTE = {
  ...NOTE,
  id: "n2",
  scope: "org",
  project_id: null,
  project_key: null,
  kind: "howto",
  title: "PR titles",
  body: "Start with the Jira key.",
  status: "active",
  source: "manual",
  evidence: [],
  use_count: 9,
};

const PROJECTS = [
  { id: "p1", key: "PROJ", name: "Project" },
  { id: "p2", key: "WEB", name: "Web" },
];

function detailFor(note: Record<string, unknown>, canWrite = true) {
  return {
    note: { ...note, can_write: canWrite },
    versions: [
      { version: 1, snapshot: {}, changed_by: "agent", change_reason: "drafted from run wf-42", created_at: "2026-11-20T10:00:00Z" },
      { version: 2, snapshot: {}, changed_by: "lead@example.com", change_reason: "tightened wording", created_at: "2026-11-21T10:00:00Z" },
    ],
    project: note.project_id ? PROJECTS[0] : null,
    can_write: canWrite,
    is_admin: canWrite,
    drafted_from: (note.evidence as { type: string }[]).filter((item) => item.type === "drafted_from"),
  };
}

interface Call {
  url: string;
  method: string;
  body?: string;
}

function serve(opts: { mode?: string; canWrite?: boolean; statusResponse?: () => Response } = {}) {
  const calls: Call[] = [];
  mockFetch((url, init) => {
    const method = (init?.method || "GET").toUpperCase();
    calls.push({ url, method, body: init?.body ? String(init.body) : undefined });
    if (url.startsWith("/admin/knowledge/notes?")) {
      return jsonResponse({
        notes: [NOTE, ORG_NOTE].map((note) => ({ ...note, can_write: opts.canWrite ?? true })),
        mode: opts.mode || "hybrid",
        is_admin: true,
        projects: PROJECTS,
      });
    }
    if (url === "/admin/knowledge/notes/n1") return jsonResponse(detailFor(NOTE, opts.canWrite ?? true));
    if (url === "/admin/knowledge/notes/n2") return jsonResponse(detailFor(ORG_NOTE, opts.canWrite ?? true));
    if (url.endsWith("/status") && opts.statusResponse) return opts.statusResponse();
    return jsonResponse({ note: NOTE });
  });
  return calls;
}

const listCalls = (calls: Call[]) => calls.filter((call) => call.url.startsWith("/admin/knowledge/notes?"));
const lastListQuery = (calls: Call[]) => new URLSearchParams(listCalls(calls).at(-1)!.url.split("?")[1]);

beforeEach(() => installMediaQuery(true));
afterEach(() => vi.restoreAllMocks());

describe("Memory page", () => {
  it("lists notes with kind, scope, status, drafted marker and use count", async () => {
    serve();
    renderPage(<MemoryPage onSelect={() => undefined} />);
    const list = await screen.findByRole("list", { name: "Notes" });
    const rows = within(list).getAllByRole("button");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("Run migrations before tests");
    expect(rows[0]).toHaveTextContent("The api test suite needs the schema migrated first.");
    expect(rows[0]).toHaveTextContent("Avoid");
    expect(rows[0]).toHaveTextContent("Project PROJ");
    expect(rows[0]).toHaveTextContent("Proposed");
    expect(rows[0]).toHaveTextContent("Drafted from a run");
    expect(rows[0]).toHaveTextContent("Used by 3 runs");
    expect(rows[1]).toHaveTextContent("Org");
    expect(rows[1]).toHaveTextContent("Active");
    expect(rows[1]).not.toHaveTextContent("Drafted from a run");
    expect(screen.queryByText(/Semantic search is unavailable/)).not.toBeInTheDocument();
  });

  it("debounces the search into the q query parameter and sends filters", async () => {
    const calls = serve();
    const user = userEvent.setup();
    renderPage(<MemoryPage onSelect={() => undefined} />);
    await screen.findByRole("list", { name: "Notes" });
    expect(lastListQuery(calls).get("q")).toBeNull();

    await user.type(screen.getByLabelText("Search notes"), "migrations");
    await waitFor(() => expect(lastListQuery(calls).get("q")).toBe("migrations"));
    // One request for the finished word, not one per keystroke.
    expect(listCalls(calls).filter((call) => call.url.includes("q=")).length).toBe(1);

    await user.selectOptions(screen.getByLabelText("Scope filter"), "project");
    await user.selectOptions(screen.getByLabelText("Project filter"), "p1");
    await user.selectOptions(screen.getByLabelText("Status filter"), "proposed");
    await user.selectOptions(screen.getByLabelText("Kind filter"), "avoid");
    await waitFor(() => {
      const params = lastListQuery(calls);
      expect(params.get("q")).toBe("migrations");
      expect(params.get("scope")).toBe("project");
      expect(params.get("project_id")).toBe("p1");
      expect(params.get("status")).toBe("proposed");
      expect(params.get("kind")).toBe("avoid");
    });
  });

  it("says when results are full-text only", async () => {
    serve({ mode: "text" });
    renderPage(<MemoryPage onSelect={() => undefined} />);
    expect(await screen.findByText(/Semantic search is unavailable/)).toBeInTheDocument();
  });

  it("selects a note from the list", async () => {
    serve();
    const onSelect = vi.fn();
    renderPage(<MemoryPage onSelect={onSelect} />);
    const list = await screen.findByRole("list", { name: "Notes" });
    await userEvent.click(within(list).getByRole("button", { name: /PR titles/ }));
    expect(onSelect).toHaveBeenCalledWith("n2");
  });

  it("shows the note detail with drafted-from evidence and version history", async () => {
    serve();
    renderPage(<MemoryPage noteId="n1" onSelect={() => undefined} />);
    const detail = await screen.findByRole("region", { name: "Note detail" });
    expect(within(detail).getByRole("heading", { name: "Run migrations before tests" })).toBeInTheDocument();
    expect(detail).toHaveTextContent("Tests fail on a fresh database otherwise.");
    expect(detail).toHaveTextContent("PROJ · Project");
    expect(detail).toHaveTextContent("Proposed byagent");
    const chips = within(detail).getByRole("list", { name: "Applies to" });
    expect(within(chips).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["org/api", "tests"]);

    const drafted = within(detail).getByRole("region", { name: "Drafted from" });
    expect(drafted).toHaveTextContent("run wf-42");
    expect(within(drafted).getByRole("link", { name: "https://github.com/org/api/pull/7" })).toHaveAttribute(
      "href",
      "https://github.com/org/api/pull/7",
    );

    const evidence = within(detail).getByRole("region", { name: "Evidence" });
    expect(evidence).toHaveTextContent("pr_comment");
    expect(evidence).toHaveTextContent("please migrate first");
    // A non-http url is shown as text, not a link.
    expect(within(evidence).queryByRole("link", { name: "local-ref" })).not.toBeInTheDocument();
    expect(evidence).toHaveTextContent("local-ref");

    const history = within(detail).getByRole("region", { name: "Version history" });
    const versions = within(history).getAllByRole("listitem");
    expect(versions).toHaveLength(2);
    expect(versions[0]).toHaveTextContent("Version 1 · agent · drafted from run wf-42");
    expect(versions[1]).toHaveTextContent("Version 2 · lead@example.com · tightened wording");
  });

  it("approves a proposed note by posting its status and reloads", async () => {
    const calls = serve();
    renderPage(<MemoryPage noteId="n1" onSelect={() => undefined} />);
    await screen.findByRole("region", { name: "Note detail" });
    const before = calls.length;
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(calls.some((call) => call.method === "POST")).toBe(true));
    const post = calls.find((call) => call.method === "POST")!;
    expect(post.url).toBe("/admin/notes/n1/status");
    expect(JSON.parse(post.body!)).toEqual({ status: "active", expected_version: 2 });
    await waitFor(() => {
      const after = calls.slice(before);
      expect(after.some((call) => call.url.startsWith("/admin/knowledge/notes?"))).toBe(true);
      expect(after.some((call) => call.url === "/admin/knowledge/notes/n1" && call.method === "GET")).toBe(true);
    });
  });

  it("reloads and explains when an action hits a version conflict", async () => {
    serve({
      statusResponse: () => jsonResponse({ error: "version conflict", code: "version_conflict", note: NOTE }, 409),
    });
    renderPage(<MemoryPage noteId="n1" onSelect={() => undefined} />);
    await screen.findByRole("region", { name: "Note detail" });
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(await findAlert()).toHaveTextContent("Changed by someone else");
  });

  it("edits and archives an active note with compare-and-set", async () => {
    const calls = serve();
    renderPage(<MemoryPage noteId="n2" onSelect={() => undefined} />);
    await screen.findByRole("region", { name: "Note detail" });
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    const title = screen.getByLabelText("Title");
    await userEvent.clear(title);
    await userEvent.type(title, "PR titles start with the key");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(calls.some((call) => call.method === "PATCH")).toBe(true));
    const patch = calls.find((call) => call.method === "PATCH")!;
    expect(patch.url).toBe("/admin/notes/n2");
    expect(JSON.parse(patch.body!)).toEqual({
      expected_version: 2,
      title: "PR titles start with the key",
      body: "Start with the Jira key.",
      why: "Tests fail on a fresh database otherwise.",
      kind: "howto",
      applies_to: ["org/api", "tests"],
    });

    await userEvent.click(await screen.findByRole("button", { name: "Archive" }));
    await waitFor(() => expect(calls.some((call) => call.method === "POST")).toBe(true));
    const post = calls.find((call) => call.method === "POST")!;
    expect(JSON.parse(post.body!)).toEqual({ status: "retired", expected_version: 2 });
  });

  it("shows no write actions when the note cannot be written", async () => {
    serve({ canWrite: false });
    renderPage(<MemoryPage noteId="n1" onSelect={() => undefined} />);
    await screen.findByRole("region", { name: "Note detail" });
    for (const name of ["Approve", "Reject", "Edit", "Archive"]) {
      expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
    }
  });
});
