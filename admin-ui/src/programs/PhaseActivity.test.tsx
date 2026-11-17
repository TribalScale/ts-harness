import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it } from "vitest";
import type { PhaseActivity, WorkflowProgress, WorkflowStep } from "../api/types";
import { setPermissions } from "../auth/session";
import { jsonResponse, mockFetch } from "../test/render";
import { codingJobLabel, PhaseActivityPanel } from "./PhaseActivity";

function step(step_name: string, label: string, status: string, extra: Partial<WorkflowStep> = {}): WorkflowStep {
  return { task_id: step_name, step_name, label, agent_id: "orchestrator", status, approval_pending: false, error: "", updated_at: "", ...extra };
}

function delivery(label: string, extra: Partial<WorkflowProgress>): WorkflowProgress {
  return {
    workflow_id: `wf-${label}`,
    workflow_type: "deliver_work_order",
    label,
    workflow_status: "running",
    pr_url: "",
    current_step: "",
    state: "",
    kind: "running",
    revisions: 0,
    stuck_task_id: "",
    updated_at: "2026-10-22T18:28:34Z",
    steps: [],
    ...extra,
  };
}

const implement: PhaseActivity = {
  phase: "implement",
  state: "running",
  workflow_id: "wf-impl",
  workflow: {
    ...delivery("implement delivery", {}),
    workflow_id: "wf-impl",
    state: "Waiting for the workflows it started",
    kind: "waiting",
    steps: [step("fork_wave", "fork wave", "succeeded"), step("join_wave", "join wave", "blocked")],
  },
  children: [
    delivery("WO-001", { state: "Merged", kind: "done", pr_url: "https://github.com/o/r/pull/17" }),
    delivery("WO-002", {
      state: "Stuck at code review: waiting for your decision",
      kind: "stuck",
      stuck_task_id: "finalize-4",
      revisions: 3,
    }),
    delivery("WO-003", {
      state: "QA running",
      revisions: 1,
      pr_url: "https://github.com/o/r/pull/19",
      steps: [step("deliver_work_order", "OpenCode", "succeeded"), step("qa_gate", "QA", "running")],
    }),
  ],
};

beforeEach(() => {
  localStorage.clear();
  setPermissions(["view", "approve_gate"]);
});

it("shows the stage's own steps and the work orders it started", async () => {
  render(<PhaseActivityPanel phase="implement" activity={implement} />);

  const panel = screen.getByRole("region", { name: "implement activity" });
  expect(within(panel).getByText("Waiting for the workflows it started")).toBeInTheDocument();
  expect(within(screen.getByRole("list", { name: "implement steps" })).getAllByRole("listitem")).toHaveLength(2);
  expect(within(panel).getByRole("heading", { name: "Work orders (3)" })).toBeInTheDocument();

  const wo3 = screen.getAllByRole("listitem").find((row) => row.classList.contains("workflow-row") && within(row).queryByText("WO-003"))!;
  expect(within(wo3).getByText("QA running")).toBeInTheDocument();
  expect(within(wo3).getByText("1 revision")).toBeInTheDocument();
  expect(within(wo3).getByRole("link", { name: "PR #19" })).toHaveAttribute("href", "https://github.com/o/r/pull/19");
  expect(screen.getByRole("link", { name: "Decide in Inbox" })).toHaveAttribute("href", "#/inbox/stuck_step/finalize-4");

  const toggle = within(wo3).getByRole("button", { name: /WO-003/ });
  await userEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  expect(within(screen.getByRole("list", { name: "WO-003 steps" })).getByText("QA")).toBeInTheDocument();
});

it("approves a pending gate from the stage panel", async () => {
  const posts: string[] = [];
  mockFetch((url, init) => {
    posts.push(`${url} ${init?.body}`);
    return jsonResponse({ ok: true });
  });
  let changed = 0;
  const prd: PhaseActivity = {
    phase: "prd",
    state: "running",
    workflow_id: "wf-prd",
    workflow: {
      ...delivery("prd request", {}),
      state: "Waiting for approval: approve prd",
      kind: "waiting",
      steps: [step("draft_prd", "draft prd", "succeeded"), step("approve_prd", "approve prd", "blocked", { approval_pending: true })],
    },
    children: [],
  };

  render(<PhaseActivityPanel phase="prd" activity={prd} onChanged={() => (changed += 1)} />);

  expect(screen.getByText("waiting for approval")).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: /Work orders/ })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Approve" }));
  await waitFor(() => expect(posts).toEqual(['/admin/workflows/approve {"id":"approve_prd"}']));
  expect(changed).toBe(1);
});

it("hides gate actions without the approve permission or once the program is closed", () => {
  const prd: PhaseActivity = {
    ...implement,
    children: [],
    workflow: { ...implement.workflow!, steps: [step("approve_prd", "approve prd", "blocked", { approval_pending: true })] },
  };
  const { rerender } = render(<PhaseActivityPanel phase="prd" activity={prd} programClosed />);
  expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  setPermissions(["view"]);
  rerender(<PhaseActivityPanel phase="prd" activity={prd} />);
  expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
});

it("says when a stage has not started", () => {
  render(
    <PhaseActivityPanel
      phase="release"
      activity={{ phase: "release", state: "not_started", workflow_id: "", workflow: null, children: [] }}
    />
  );
  expect(screen.getByText("This stage has not started.")).toBeInTheDocument();
});

it("shows a running coding job's time and latest output", () => {
  const activity: PhaseActivity = {
    ...implement,
    children: [
      delivery("WO-004", {
        state: "OpenCode implementing",
        coding_job: { id: "job-1", status: "running", last_output: "npm test", started_at: new Date(Date.now() - 5 * 60000).toISOString(), created_at: "" },
      }),
    ],
  };
  render(<PhaseActivityPanel phase="implement" activity={activity} />);
  expect(screen.getByText("Running for 5 min")).toBeInTheDocument();
  expect(screen.getByText("npm test")).toBeInTheDocument();
});

it("labels a queued coding job", () => {
  expect(codingJobLabel({ id: "j", status: "queued", last_output: "", started_at: "", created_at: "" })).toBe("Waiting for a coding worker");
  expect(codingJobLabel({ id: "j", status: "starting", last_output: "", started_at: "", created_at: "" })).toBe("Coding worker starting");
});
