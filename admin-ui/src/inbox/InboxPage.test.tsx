import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, expect, it, vi } from "vitest";
import type { InboxItem } from "../api/types";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { setPermissions } from "../auth/session";
import { InboxPage } from "./InboxPage";
import { findAlert } from "../test/alerts";

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

const stuckItem: InboxItem = {
  type: "stuck_step",
  id: "finalize-4",
  title: "WO-003 · finalize_code_review",
  subtitle: "Revision limit reached · waiting for your decision",
  created_at: "2026-10-22T17:34:10Z",
  meta: {
    workflow_id: "wf-d",
    task_id: "finalize-4",
    step_name: "finalize_code_review",
    work_order: "WO-003",
    decisions: ["revise", "accept", "stop"],
    feedback: "Blocking findings (fix every one):\n- clearing the portfolio keeps the old balance",
    pr_url: "https://github.com/org/app/pull/18",
    program_id: "program-1",
    status: "blocked",
  },
};

it("decides a stuck step: revise again with guidance", async () => {
  const posts: { url: string; body: Record<string, unknown> }[] = [];
  mockFetch((url, init) => {
    if (url === "/admin/inbox") return jsonResponse({ items: posts.length ? [] : [stuckItem] });
    if (url === "/admin/workflows/decide") {
      posts.push({ url, body: JSON.parse(String(init?.body)) });
      return jsonResponse({ status: "revising", revision_number: 1, max_revisions: 3 });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<StatefulInbox initialType="stuck_step" initialId="finalize-4" />);

  expect(await screen.findByRole("button", { name: /Stuck 1/ })).toBeInTheDocument();
  const detail = await screen.findByRole("region", { name: /Stuck step detail/i });
  expect(within(detail).getByText(/clearing the portfolio keeps the old balance/, { selector: "p" })).toBeInTheDocument();
  expect(within(detail).getByRole("link", { name: /pull\/18/ })).toHaveAttribute(
    "href",
    "https://github.com/org/app/pull/18"
  );
  expect(within(detail).getByRole("button", { name: "Accept as is" })).toBeInTheDocument();
  expect(within(detail).getByRole("button", { name: "Stop work order" })).toBeInTheDocument();

  await userEvent.type(within(detail).getByLabelText(/Guidance for OpenCode/), "Persist the empty state");
  await userEvent.click(within(detail).getByRole("button", { name: "Revise again" }));

  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0].body).toEqual({
    workflow_id: "wf-d",
    task_id: "finalize-4",
    action: "revise",
    guidance: "Persist the empty state",
  });
});

it("shows the arbiter's hand-off and applies its recommendation in one click", async () => {
  const item: InboxItem = {
    ...stuckItem,
    subtitle: "Arbiter handed off · waiting for your decision",
    meta: {
      ...stuckItem.meta,
      arbiter: {
        label: "Arbiter handed off",
        current: true,
        why: "the arbiter already acted 2 times on this step",
        reason: "The revision redesigned the review panel the ticket leaves alone.",
        actions: [
          { action: "revise", reason: "Scope: undo the panel redesign" },
          { action: "revise", reason: "Same finding again" },
        ],
        recommendation: { action: "revise", guidance: "Keep only the dashed Draft badge." },
      },
    },
  };
  const posts: Record<string, unknown>[] = [];
  mockFetch((url, init) => {
    if (url === "/admin/inbox") return jsonResponse({ items: posts.length ? [] : [item] });
    if (url === "/admin/workflows/decide") {
      posts.push(JSON.parse(String(init?.body)));
      return jsonResponse({ status: "revising" });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<StatefulInbox initialType="stuck_step" initialId="finalize-4" />);

  const detail = await screen.findByRole("region", { name: /Stuck step detail/i });
  const arbiter = within(detail).getByRole("region", { name: "Arbiter" });
  expect(within(arbiter).getByText("Arbiter handed off")).toBeInTheDocument();
  expect(within(arbiter).getByText(/already acted 2 times/)).toBeInTheDocument();
  expect(within(arbiter).getAllByRole("listitem")).toHaveLength(2);

  await userEvent.click(within(arbiter).getByRole("button", { name: "Apply recommendation" }));
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0]).toEqual({ workflow_id: "wf-d", task_id: "finalize-4", action: "revise", guidance: "Keep only the dashed Draft badge." });
});

it("a stuck planning step shows its error and retries with guidance", async () => {
  const planItem: InboxItem = {
    type: "stuck_step",
    id: "plan-9",
    title: "this work order · plan_work_orders",
    subtitle: "Blocked · waiting for your decision",
    created_at: "2026-11-26T02:34:49Z",
    meta: {
      workflow_id: "wf-p",
      task_id: "plan-9",
      step_name: "plan_work_orders",
      decisions: ["retry", "accept", "stop"],
      feedback: "What changed from the rejected output: the numbered list is now a table.",
      error: "WO-001 adds or changes endpoints but does not update the committed openapi.json",
      status: "blocked",
    },
  };
  const posts: Record<string, unknown>[] = [];
  mockFetch((url, init) => {
    if (url === "/admin/inbox") return jsonResponse({ items: posts.length ? [] : [planItem] });
    if (url === "/admin/workflows/decide") {
      posts.push(JSON.parse(String(init?.body)));
      return jsonResponse({ status: "retried" });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<StatefulInbox initialType="stuck_step" initialId="plan-9" />);

  const detail = await screen.findByRole("region", { name: /Stuck step detail/i });
  const why = within(detail).getByRole("heading", { name: "Why it is stuck" }).closest("section");
  expect(within(why as HTMLElement).getByText(/does not update the committed openapi.json/)).toBeInTheDocument();
  expect(within(detail).getByText(/the numbered list is now a table/, { selector: "p" })).toBeInTheDocument();
  expect(within(detail).queryByText(/gives OpenCode a fresh/)).not.toBeInTheDocument();
  expect(within(detail).getByText(/stopped after its automatic/)).toBeInTheDocument();

  await userEvent.type(within(detail).getByLabelText(/Guidance for the next attempt/), "WO-001 changes no endpoint.");
  await userEvent.click(within(detail).getByRole("button", { name: "Retry" }));

  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0]).toEqual({
    workflow_id: "wf-p",
    task_id: "plan-9",
    action: "retry",
    guidance: "WO-001 changes no endpoint.",
  });
});

it("shows what the automatic PRD checks found on a PRD gate", async () => {
  const prdGate: InboxItem = {
    type: "workflow_gate",
    id: "gate-prd",
    title: "approve_prd",
    subtitle: "Program Expense export",
    created_at: "2026-11-14T10:10:00Z",
    meta: {
      task_id: "gate-prd",
      step_name: "approve_prd",
      program_id: "program-2",
      prd_checks: ["No priority (Must, Should or Could) on FR-3.", "Definition of Done has 4 items; it needs 5 to 10."],
    },
  };
  mockFetch((url) => {
    if (url === "/admin/inbox") return jsonResponse({ items: [prdGate, inboxItems[2]] });
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<StatefulInbox initialType="workflow_gate" initialId="gate-prd" />);

  const checks = await screen.findByRole("region", { name: "Draft checks" });
  expect(within(checks).getByText(/found 2 issues/)).toBeInTheDocument();
  expect(within(checks).getByText("No priority (Must, Should or Could) on FR-3.")).toBeInTheDocument();
  expect(within(checks).getByText("Definition of Done has 4 items; it needs 5 to 10.")).toBeInTheDocument();
});

it("says a PRD draft passed and shows nothing on other gates", async () => {
  const passed: InboxItem = { ...inboxItems[2], id: "gate-ok", meta: { ...inboxItems[2].meta, prd_checks: [] } };
  mockFetch((url) => {
    if (url === "/admin/inbox") return jsonResponse({ items: [passed, inboxItems[2]] });
    throw new Error(`Unexpected URL ${url}`);
  });

  const { unmount } = renderPage(<StatefulInbox initialType="workflow_gate" initialId="gate-ok" />);
  const checks = await screen.findByRole("region", { name: "Draft checks" });
  expect(within(checks).getByText("The automatic PRD checks found no issues.")).toBeInTheDocument();
  unmount();

  renderPage(<StatefulInbox initialType="workflow_gate" initialId="gate-1" />);
  expect(await screen.findByText("Approve release")).toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "Draft checks" })).not.toBeInTheDocument();
});

it("only offers the decisions the step allows", async () => {
  const waitItem = { ...stuckItem, meta: { ...stuckItem.meta, decisions: ["revise", "stop"] } };
  mockFetch((url) => {
    if (url === "/admin/inbox") return jsonResponse({ items: [waitItem] });
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<StatefulInbox initialType="stuck_step" initialId="finalize-4" />);

  const detail = await screen.findByRole("region", { name: /Stuck step detail/i });
  expect(within(detail).getByRole("button", { name: "Revise again" })).toBeInTheDocument();
  expect(within(detail).queryByRole("button", { name: "Accept as is" })).not.toBeInTheDocument();
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
  expect(await findAlert()).toHaveTextContent("draft storage unavailable");
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

it("shows what a gate approves: the release a promote gate deploys", async () => {
  const gate: InboxItem = {
    type: "workflow_gate",
    id: "gate-staging",
    title: "approve_promote_staging",
    subtitle: "Program Discussion categories",
    created_at: "2026-11-17T16:00:00Z",
    meta: { task_id: "gate-staging", step_name: "approve_promote_staging", program_id: "program-3" },
  };
  mockFetch((url) => {
    if (url === "/admin/inbox") return jsonResponse({ items: [gate] });
    if (url === "/admin/workflows/gate-review?task_id=gate-staging") {
      return jsonResponse({
        task_id: "gate-staging",
        kind: "release",
        release: {
          environment: "staging",
          repo: "acme/web",
          sha: "a4d80707e51395b5",
          pr_urls: ["https://github.com/acme/web/pull/67", "https://github.com/acme/web/pull/68"],
          ci: { state: "success", checks: { e2e: "success", check: "success" }, failed: [] },
          rollback_sha: "22ebbfb1560c",
        },
      });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<StatefulInbox initialType="workflow_gate" initialId="gate-staging" />);

  const review = await screen.findByRole("region", { name: "What you are approving" });
  expect(await within(review).findByRole("link", { name: "PR #68" })).toBeInTheDocument();
  expect(within(review).getByText("staging")).toBeInTheDocument();
  expect(within(review).getByText(/e2e: success, check: success/)).toBeInTheDocument();
  expect(within(review).getByText("22ebbfb1560c")).toBeInTheDocument();
});

it("lists a pending chat proposal and cancels it", async () => {
  const posted: string[] = [];
  let cancelled = false;
  const proposal = {
    type: "pending_proposal",
    id: "s1:pending_program",
    title: "Start a program `jira_to_pr`: implement all PROJ",
    subtitle: "Waiting for a yes or no in slack",
    created_at: "2026-10-22T17:34:10Z",
    meta: { session_id: "s1", key: "pending_program", source: "slack", status: "pending" },
  };
  mockFetch((url, init) => {
    if (url === "/admin/inbox") return jsonResponse({ items: cancelled ? [] : [proposal] });
    if (url === "/admin/proposals/cancel") {
      posted.push(String(init?.body));
      cancelled = true;
      return jsonResponse({ status: "cancelled", cancelled: [] });
    }
    throw new Error(`Unexpected URL ${url}`);
  });
  renderPage(<StatefulInbox initialType="pending_proposal" initialId="s1:pending_program" />);
  expect(await screen.findByRole("button", { name: /Proposals 1/ })).toBeInTheDocument();
  await userEvent.click(await screen.findByRole("button", { name: "Cancel proposal" }));
  await waitFor(() => expect(posted).toEqual(['{"session_id":"s1"}']));
  expect(await screen.findByText("Proposal cancelled")).toBeInTheDocument();
});

it("shows an improvement proposal's diff, evidence and replay, and promotes it", async () => {
  const posted: { url: string; body: string }[] = [];
  let decided = false;
  const proposal = {
    id: 12,
    type: "skill_edit",
    status: "awaiting_approval",
    target: "pre_push_review",
    title: "Ask for a test per retry path",
    summary: "The reviewer misses untested retry branches.",
    pattern_key: "review:tests",
    metric: "autonomy",
    expected_effect: "fewer test blocks at the gate",
    risk: "more pre-push findings",
    change: { diff: "--- a/skills/x/SKILL.md\n+++ b/skills/x/SKILL.md\n@@ -1 +1,2 @@\n ## Deep checks\n+- retries\n" },
    evidence: {
      tickets: 4,
      pattern: "review:tests",
      examples: ["no unit test for the retry"],
      refs: [{ workflow_id: "wf-1", jira_issue_key: "PROJ-7", pr_url: "https://github.com/org/api/pull/3" }],
    },
    evaluation: {
      status: "passed",
      reason: "catch rate 80% vs 40%",
      cost_usd: 1.2,
      scores: {
        current: { blocks: 5, passes: 5, caught: 2, false_blocks: 0, catch_rate: 0.4, false_block_rate: 0, agreement: 0.7 },
        candidate: { blocks: 5, passes: 5, caught: 4, false_blocks: 0, catch_rate: 0.8, false_block_rate: 0, agreement: 0.9 },
      },
    },
    canary: {
      variant: "proposal-12",
      share: 0.5,
      started_at: "2026-11-01T00:00:00Z",
      latest: {
        checked_at: "2026-11-20T00:00:00Z",
        candidate: { tickets: 8, finished: 8, merged: 8, autonomy_rate: 0.75, problem_rate: 0, tickets_with_problems: 0 },
        control: { tickets: 8, finished: 8, merged: 8, autonomy_rate: 0.625, problem_rate: 0, tickets_with_problems: 0 },
        decision: { action: "approve", reason: "autonomy 75% vs control 62%" },
      },
    },
    cost_usd: 1.3,
  };
  const item: InboxItem = {
    type: "improvement_proposal",
    id: "12",
    title: proposal.title,
    subtitle: "skill edit · Canary passed: promote or reject",
    created_at: "2026-11-20T00:00:00Z",
    meta: { proposal, status: "awaiting_approval" },
  };
  mockFetch((url, init) => {
    if (url === "/admin/inbox") return jsonResponse({ items: decided ? [] : [item] });
    if (url === "/admin/improvements/12/decision") {
      posted.push({ url, body: String(init?.body) });
      decided = true;
      return jsonResponse({
        ...proposal,
        status: "promoted",
        promotion: { method: "pull_request", pr_url: "https://github.com/org/harness/pull/9" },
      });
    }
    throw new Error(`Unexpected URL ${url}`);
  });
  renderPage(<StatefulInbox initialType="improvement_proposal" initialId="12" />);
  expect(await screen.findByRole("button", { name: /Improvements 1/ })).toBeInTheDocument();
  expect(await screen.findByLabelText("Diff")).toHaveTextContent("+- retries");
  expect(screen.getByText("no unit test for the retry")).toBeInTheDocument();
  expect(screen.getByLabelText("Replay scores")).toHaveTextContent("candidate80% (4/5)");
  expect(screen.getByLabelText("Variant against control")).toHaveTextContent("control863%");
  await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
  await waitFor(() =>
    expect(posted).toEqual([{ url: "/admin/improvements/12/decision", body: '{"action":"approve","reason":""}' }]),
  );
  expect(await screen.findByText(/Pull request opened \(not merged\)/)).toBeInTheDocument();
});

it("an off-limits idea can only be dismissed", async () => {
  const item: InboxItem = {
    type: "improvement_proposal",
    id: "13",
    title: "Block on any missing test",
    subtitle: "needs design",
    meta: {
      status: "proposed",
      proposal: {
        id: 13,
        type: "needs_design",
        status: "proposed",
        target: "skills/review/governed-code-review/SKILL.md",
        title: "Block on any missing test",
        summary: "",
        pattern_key: "review:tests",
        metric: "",
        expected_effect: "",
        risk: "",
        change: {
          idea: "make missing tests blockers",
          off_limits: { match: "SKILL.md#Severity", why: "what blocks a PR", section: "Severity" },
        },
        evidence: { tickets: 3 },
        cost_usd: 0.1,
      },
    },
  };
  mockFetch((url) => {
    if (url === "/admin/inbox") return jsonResponse({ items: [item] });
    throw new Error(`Unexpected URL ${url}`);
  });
  renderPage(<StatefulInbox initialType="improvement_proposal" initialId="13" />);
  expect(await screen.findByText(/A person has to design this change/)).toBeInTheDocument();
  expect(await screen.findByRole("button", { name: "Dismiss" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
});

it("shows a paused ticket with who paused it and what waits, and resumes it", async () => {
  const posts: { url: string; body: Record<string, unknown> }[] = [];
  const pausedItem: InboxItem = {
    type: "paused",
    id: "ticket:wf-7",
    title: "Ticket PROJ-7 is paused",
    subtitle: "Paused by alice@example.com · Paused (revision waiting)",
    created_at: "2026-11-28T10:00:00Z",
    meta: {
      scope: "ticket",
      target_id: "wf-7",
      by: "alice@example.com",
      at: "2026-11-28T10:00:00Z",
      reason: "waiting on design",
      label: "PROJ-7",
      can_resume: true,
      held: [{ task_id: "t1", workflow_id: "wf-7", step_name: "deliver_work_order", hold: { kind: "revision" }, still_paused: true }],
    },
  };
  mockFetch((url, init) => {
    if (url === "/admin/inbox") return jsonResponse({ items: posts.length ? [] : [pausedItem] });
    if (url === "/admin/resume") {
      posts.push({ url, body: JSON.parse(String(init?.body)) });
      return jsonResponse({ scope: "ticket", target_id: "wf-7", status: "resumed", message: "Resumed PROJ-7." });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<StatefulInbox initialType="paused" initialId="ticket:wf-7" />);

  expect(await screen.findByRole("button", { name: /Paused 1/ })).toBeInTheDocument();
  const detail = await screen.findByRole("region", { name: /Paused detail/i });
  expect(within(detail).getAllByText(/Paused by alice@example.com/).length).toBeGreaterThan(0);
  expect(within(detail).getByText("Reason: waiting on design")).toBeInTheDocument();
  expect(within(detail).getByRole("listitem")).toHaveTextContent("Paused (revision waiting) · deliver_work_order");
  await userEvent.click(within(detail).getByRole("button", { name: "Resume PROJ-7" }));
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0].body).toMatchObject({ scope: "ticket", workflow_id: "wf-7" });
});
