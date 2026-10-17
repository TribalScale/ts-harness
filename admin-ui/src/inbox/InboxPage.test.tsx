import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, expect, it, vi } from "vitest";
import type { InboxItem } from "../api/types";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { setPermissions } from "../auth/session";
import { InboxPage } from "./InboxPage";

const inboxItems: InboxItem[] = [
  {
    type: "skill_draft",
    id: "skill-1",
    title: "Skill draft",
    subtitle: "Prompt injection review",
    created_at: "2026-08-07T10:00:00Z",
    meta: { requester: "Maya", status: "pending" },
  },
  {
    type: "tool_approval",
    id: "approval-1",
    title: "Tool approval",
    subtitle: "Run deployment script",
    created_at: "2026-08-07T10:05:00Z",
    meta: { command: "npm run deploy", requester: "Ravi", status: "pending" },
  },
  {
    type: "workflow_gate",
    id: "gate-1",
    title: "Workflow gate",
    subtitle: "Release checkpoint",
    created_at: "2026-08-07T10:10:00Z",
    meta: {
      task_id: "task-7",
      step_name: "Approve release",
      program_id: "program-1",
      status: "blocked",
    },
  },
];

function installMediaQuery(desktop = true) {
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

function StatefulInbox({
  initialType,
  initialId,
}: {
  initialType?: InboxItem["type"];
  initialId?: string;
}) {
  const [selected, setSelected] = useState({ type: initialType, id: initialId });
  return (
    <InboxPage
      selectedType={selected.type}
      selectedId={selected.id}
      onSelect={(type, id) => setSelected({ type, id })}
    />
  );
}

beforeEach(() => {
  localStorage.clear();
  setPermissions([
    "view",
    "start_program",
    "approve_gate",
    "confirm_delegation",
    "edit_config",
    "edit_models",
    "edit_allowlists",
  ]);
  installMediaQuery();
});

it("renders counted filters and compact rows for every inbox type", async () => {
  mockFetch((url) => {
    if (url === "/admin/inbox") return jsonResponse({ items: inboxItems });
    if (url === "/admin/skills/drafts/skill-1") {
      return jsonResponse({
        title: "Skill draft",
        content: "Draft body",
        requester: "Maya",
        created_at: "2026-08-07T10:00:00Z",
      });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<StatefulInbox />);

  expect(await screen.findByRole("button", { name: /All for me 3/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Skills 1/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Approvals 1/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Gates 1/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Skill draft/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Tool approval/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Workflow gate/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Skill draft/ })).toHaveAttribute("title", "Skill draft");
  expect(screen.getByRole("button", { name: /All for me 3/ })).toHaveAttribute(
    "aria-pressed",
    "true"
  );
  expect(screen.getByRole("button", { name: /Approvals 1/ })).toHaveAttribute(
    "aria-pressed",
    "false"
  );
});

it("never presents a pending skill draft as a success status", async () => {
  mockFetch((url) => {
    if (url === "/admin/inbox") return jsonResponse({ items: inboxItems });
    if (url === "/admin/skills/drafts/skill-1") return jsonResponse({ title: "Skill draft" });
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<StatefulInbox />);

  const row = await screen.findByRole("button", { name: /Skill draft/ });
  expect(row.querySelector(".badge-skill")).toHaveTextContent("Skill draft");
  expect(row.querySelector(".status-success")).toBeNull();
  expect(within(row).getByText("Prompt injection review")).toBeInTheDocument();
});

it("derives inbox row status from item metadata instead of item type", async () => {
  mockFetch((url) => {
    if (url === "/admin/inbox") return jsonResponse({ items: inboxItems });
    if (url === "/admin/skills/drafts/skill-1") return jsonResponse({ title: "Skill draft" });
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<StatefulInbox />);

  const gate = await screen.findByRole("button", { name: /Workflow gate/ });
  expect(gate.querySelector(".status-warning")).toHaveTextContent(/blocked/i);
  const approval = screen.getByRole("button", { name: /Tool approval/ });
  expect(approval.querySelector(".status-success")).toBeNull();
});

it("leaves mobile detail closed after the operator closes it", async () => {
  installMediaQuery(false);
  mockFetch((url) => {
    if (url === "/admin/inbox") return jsonResponse({ items: inboxItems });
    if (url === "/admin/approvals/approval-1") {
      return jsonResponse({ requester: "Ravi", command: "npm run deploy" });
    }
    if (url === "/admin/skills/drafts/skill-1") return jsonResponse({ title: "Skill draft" });
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<StatefulInbox />);

  expect(await screen.findByRole("button", { name: /Tool approval/ })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Close" })).not.toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: /Tool approval/ }));
  expect(await screen.findByRole("button", { name: "Close" })).toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: "Close" }));

  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "Close" })).not.toBeInTheDocument()
  );
  await waitFor(() =>
    expect(screen.queryByRole("region", { name: /detail/i })).not.toBeInTheDocument()
  );
  expect(screen.getByRole("button", { name: /Tool approval/ })).toBeInTheDocument();
});

it("keeps the list visible and retries only detail loading when detail fetch fails", async () => {
  const calls: string[] = [];
  mockFetch((url) => {
    calls.push(url);
    if (url === "/admin/inbox") return jsonResponse({ items: inboxItems });
    if (url === "/admin/skills/drafts/skill-1") {
      return jsonResponse({ error: "draft storage unavailable" }, 404);
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<StatefulInbox initialType="skill_draft" initialId="skill-1" />);

  expect(await screen.findByRole("button", { name: /Tool approval/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Workflow gate/ })).toBeInTheDocument();
  expect(await screen.findByRole("alert")).toHaveTextContent("draft storage unavailable");
  expect(screen.getByRole("button", { name: /Skill draft/ })).toBeInTheDocument();

  const detail = screen.getByRole("region", { name: /Skill draft detail/i });
  expect(within(detail).getByRole("button", { name: /try again/i })).toBeInTheDocument();

  await userEvent.click(within(detail).getByRole("button", { name: /try again/i }));

  await waitFor(() => {
    expect(calls.filter((url) => url === "/admin/skills/drafts/skill-1")).toHaveLength(2);
  });
  expect(calls.filter((url) => url === "/admin/inbox")).toHaveLength(1);
});

it("does not fetch or render skill draft detail for non-managers routed directly to a draft", async () => {
  localStorage.setItem("ec_admin_roles", JSON.stringify(["viewer"]));
  setPermissions(["view"]);
  const calls: string[] = [];
  mockFetch((url) => {
    calls.push(url);
    if (url === "/admin/inbox") return jsonResponse({ items: inboxItems });
    if (url === "/admin/skills/drafts/skill-1") {
      return jsonResponse({
        title: "Skill draft",
        content: "Protected draft body",
      });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<StatefulInbox initialType="skill_draft" initialId="skill-1" />);

  expect(await screen.findByRole("button", { name: /Tool approval/ })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Skill draft/ })).not.toBeInTheDocument();
  expect(calls).not.toContain("/admin/skills/drafts/skill-1");
  expect(screen.queryByText("Protected draft body")).not.toBeInTheDocument();
});

it("shows revision-of and a text diff for a revision draft", async () => {
  mockFetch((url) => {
    if (url === "/admin/inbox") return jsonResponse({ items: inboxItems });
    if (url === "/admin/skills/drafts/skill-1") {
      return jsonResponse({
        skill_id: "skill-1",
        content: "Updated body",
        revision_of: "jira-to-pr",
        revision_diff: "--- live\n+++ draft\n+Updated body\n",
        requester: "Maya",
        created_at: "2026-08-07T10:00:00Z",
      });
    }
    throw new Error(`Unexpected URL ${url}`);
  });
  renderPage(<StatefulInbox initialType="skill_draft" initialId="skill-1" />);
  const heading = await screen.findByText(/Revision of `?jira-to-pr`?/);
  expect(heading).toBeInTheDocument();
  const card = heading.closest("section") as HTMLElement;
  expect(within(card).getByText(/\+Updated body/)).toBeInTheDocument();
});

it("shows a folder draft's file tree, file viewer, per-file diffs, and created-by", async () => {
  mockFetch((url) => {
    if (url === "/admin/inbox") return jsonResponse({ items: inboxItems });
    if (url === "/admin/skills/drafts/skill-1") {
      return jsonResponse({
        skill_id: "skill-1",
        layout: "folder",
        revision_of: "skill-1",
        provenance: { created_by: "admin-1", source: "admin_upload" },
        files: [
          { path: "SKILL.md", size: 120, scan_findings: [] },
          { path: "stages/plan.md", size: 8, scan_findings: ["caution: shell"] },
        ],
        revision_diffs: [{ path: "stages/plan.md", status: "changed", diff: "-old\n+new\n" }],
      });
    }
    if (url === "/admin/skills/drafts/skill-1/files/stages/plan.md") {
      return jsonResponse({ path: "stages/plan.md", content: "# Plan body" });
    }
    throw new Error(`Unexpected URL ${url}`);
  });
  renderPage(<StatefulInbox initialType="skill_draft" initialId="skill-1" />);

  expect(await screen.findByText("admin-1 · Folder upload")).toBeInTheDocument();
  const tree = screen.getByRole("list", { name: "Draft files" });
  expect(within(tree).getByText(/caution: shell/)).toBeInTheDocument();
  expect(screen.getByText("stages/plan.md · changed")).toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: "Open stages/plan.md" }));
  expect(await screen.findByText("# Plan body")).toBeInTheDocument();
});

it("shows the resumed workflow stage and surfaces a resume failure", async () => {
  const workflowApproval: InboxItem = {
    type: "tool_approval",
    id: "approval-2",
    title: "google_workspace_docs_append",
    subtitle: "pm-agent · tool approval",
    created_at: "2026-08-07T10:06:00Z",
    meta: { status: "pending", workflow_id: "wf-1", task_id: "task-1" },
  };
  let decided = false;
  mockFetch((url, init) => {
    if (url === "/admin/inbox") return jsonResponse({ items: decided ? [] : [workflowApproval] });
    if (url === "/admin/approvals/approval-2") {
      return jsonResponse({
        tool_name: "google_workspace_docs_append",
        status: "pending",
        workflow_id: "wf-1",
        task_id: "task-1",
      });
    }
    if (url === "/admin/approvals/approval-2/decide" && init?.method === "POST") {
      decided = true;
      return jsonResponse({ status: "approved", resume: { status: "failed", error: "not awaiting" } });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<StatefulInbox initialType="tool_approval" initialId="approval-2" />);

  expect(await screen.findByText("Resumes workflow wf-1 when you decide.")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Approve" }));

  expect(
    await screen.findByText("Approved, but the workflow stage did not resume: not awaiting")
  ).toBeInTheDocument();
});

it("reloads with a notice when the approval was already decided in Slack", async () => {
  const item: InboxItem = {
    type: "tool_approval",
    id: "approval-3",
    title: "google_workspace_docs_append",
    subtitle: "marketing-agent · tool approval",
    created_at: "2026-08-07T10:06:00Z",
    meta: { status: "pending", workflow_id: "wf-1", task_id: "task-1" },
  };
  let decided = false;
  mockFetch((url, init) => {
    if (url === "/admin/inbox") return jsonResponse({ items: decided ? [] : [item] });
    if (url === "/admin/approvals/approval-3") {
      return jsonResponse({ tool_name: "google_workspace_docs_append", status: "pending", workflow_id: "wf-1" });
    }
    if (url === "/admin/approvals/approval-3/decide" && init?.method === "POST") {
      decided = true;
      return jsonResponse({ error: "approval already approved", status: "approved" }, 409);
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<StatefulInbox initialType="tool_approval" initialId="approval-3" />);

  await screen.findByText("Resumes workflow wf-1 when you decide.");
  await userEvent.click(screen.getByRole("button", { name: "Reject" }));

  expect(
    await screen.findByText("Someone already decided this request (approval already approved).")
  ).toBeInTheDocument();
  expect(screen.queryByText("Could not load item detail")).not.toBeInTheDocument();
});

it("keeps approval actions in a sticky detail footer", async () => {
  mockFetch((url) => {
    if (url === "/admin/inbox") return jsonResponse({ items: inboxItems });
    if (url === "/admin/approvals/approval-1") {
      return jsonResponse({
        requester: "Ravi",
        command: "npm run deploy",
        created_at: "2026-08-07T10:05:00Z",
        status: "pending",
      });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  const { container } = renderPage(
    <StatefulInbox initialType="tool_approval" initialId="approval-1" />
  );

  expect(await screen.findByText("npm run deploy")).toBeInTheDocument();
  fireEvent.scroll(screen.getByRole("region", { name: /Tool approval detail/i }), {
    target: { scrollTop: 1000 },
  });

  const footer = container.querySelector(".detail-actions");
  expect(footer).toBeInTheDocument();
  expect(footer).toHaveAttribute("aria-label", "Item actions");
  expect(within(footer as HTMLElement).getByRole("button", { name: "Reject" })).toBeInTheDocument();
  expect(within(footer as HTMLElement).getByRole("button", { name: "Approve" })).toBeInTheDocument();
});
