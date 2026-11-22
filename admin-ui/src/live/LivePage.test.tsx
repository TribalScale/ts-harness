import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { LivePage } from "./LivePage";
import { ProgramBoard } from "./ProgramBoard";
import { formatDuration } from "./Timeline";

const bar = (step: string, category: string, start: string, end: string, seconds: number, open = false) => ({
  task_id: step, step_name: step, label: step, category, status: "succeeded", run: 1, start, end, seconds, open,
});

const item = (key: string, waiting: string, extra: Record<string, unknown> = {}) => ({
  workflow_id: `wf-${key}`, workflow_type: "implement_jira_ticket", label: key, state: "Code review running", kind: "running",
  current_step: "code_review", revisions: 1, pr_url: `https://github.com/o/r/pull/${key.slice(-1)}`, stuck_task_id: "",
  coding_job: null, jira_issue_key: key, waiting_on: waiting, in_step_since: "", in_step_seconds: 1500, started_at: "",
  elapsed_seconds: 7200, stronger_model: false, program_id: "p1", program_label: "Alpha",
  timeline: [bar("deliver_work_order", "coding", "2026-01-02T10:00:00Z", "2026-01-02T10:30:00Z", 1800),
             bar("code_review", "review", "2026-01-02T10:30:00Z", "2026-01-02T11:00:00Z", 1800, true)],
  ...extra,
});

afterEach(() => vi.restoreAllMocks());

describe("Live page", () => {
  it("shows capacity, each running ticket and its timeline", async () => {
    const user = userEvent.setup();
    mockFetch((url) => {
      if (url === "/admin/live") {
        return jsonResponse({
          generated_at: "", capacity: { running: 2, queued: 1, limit: 3 },
          items: [item("PROJ-4", "you", { state: "Ready to merge", stuck_task_id: "t9" }), item("PROJ-3", "agent")],
        });
      }
      return jsonResponse({});
    });
    renderPage(<LivePage />);
    expect(await screen.findByText("PROJ-4")).toBeInTheDocument();
    const capacity = screen.getByLabelText("Coding capacity");
    expect(capacity).toHaveTextContent("2 of 3 coding jobs running");
    expect(capacity).toHaveTextContent("1 waiting for you");
    expect(screen.getByText("Waiting for you")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Decide in Inbox" })).toHaveAttribute("href", "#/inbox/stuck_step/t9");
    await user.click(screen.getAllByRole("button", { name: "Timeline" })[0]);
    const timeline = screen.getByLabelText("PROJ-4 timeline");
    expect(within(timeline).getAllByText("30m")).toHaveLength(2);
  });

  it("formats durations", () => {
    expect([formatDuration(40), formatDuration(720), formatDuration(3 * 3600 + 300), formatDuration(3 * 86400)]).toEqual(
      ["40s", "12m", "3h 05m", "3d 0h"]);
  });
});

describe("Program board", () => {
  it("puts tickets in columns with their blockers and shows where the time went", async () => {
    const user = userEvent.setup();
    mockFetch((url) => {
      if (url === "/admin/programs/p1/board") {
        return jsonResponse({
          generated_at: "", program_id: "p1", parallel_limit: 2,
          columns: ["waiting_on_blockers", "ready", "coding", "in_review", "waiting_for_you", "done", "stopped"],
          items: [
            { key: "PROJ-2", title: "Foundations", column: "done", blocked_by: [], item: item("PROJ-2", "done") },
            { key: "PROJ-3", title: "Roles", column: "waiting_for_you", blocked_by: [], item: item("PROJ-3", "you") },
            { key: "PROJ-4", title: "Infra", column: "waiting_on_blockers", blocked_by: ["PROJ-3"], item: null },
          ],
          totals: [{ category: "merge", label: "Waiting for merge", seconds: 3600, share: 0.6 },
                   { category: "coding", label: "Coding", seconds: 2400, share: 0.4 }],
        });
      }
      return jsonResponse({});
    });
    renderPage(<ProgramBoard programId="p1" />);
    expect(await screen.findByText("PROJ-4")).toBeInTheDocument();
    expect(within(screen.getByRole("list", { name: "Waiting on blockers" })).getByText("blocked by PROJ-3")).toBeInTheDocument();
    expect(within(screen.getByRole("list", { name: "Done" })).getByText("PROJ-2")).toBeInTheDocument();
    expect(screen.getByText("· up to 2 at a time")).toBeInTheDocument();
    expect(screen.getByLabelText("Where the time went")).toHaveTextContent("Waiting for merge: 1h 00m (60%)");
    expect(screen.queryByRole("list", { name: "Stopped" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /PROJ-3/ }));
    expect(screen.getByLabelText("PROJ-3 timeline")).toBeInTheDocument();
  });
});

describe("History", () => {
  it("lists finished runs and filters and searches them", async () => {
    const user = userEvent.setup();
    const urls: string[] = [];
    mockFetch((url) => {
      urls.push(url);
      if (url.startsWith("/admin/history")) {
        return jsonResponse({
          generated_at: "", days: 14, total: 2,
          facets: { projects: ["PAY"], repos: ["o/api"], outcomes: ["merged", "cancelled"] },
          items: [
            { ...item("PROJ-3", "done"), outcome: "merged", decided_by: "dana", finished_at: "2026-01-02T10:00:00Z", project: "PAY", repo: "o/api" },
            { ...item("PROJ-4", "done"), outcome: "cancelled", decided_by: "", finished_at: "2026-01-01T10:00:00Z", project: "PAY", repo: "o/api" },
          ],
        });
      }
      return jsonResponse({ generated_at: "", items: [], capacity: { running: 0, queued: 0, limit: null } });
    });
    const onTab = vi.fn();
    const { rerender } = renderPage(<LivePage tab="now" onTab={onTab} />);
    await user.click(screen.getByRole("tab", { name: "History" }));
    expect(onTab).toHaveBeenCalledWith("history");
    rerender(<LivePage tab="history" onTab={onTab} />);
    expect(await screen.findByText("PROJ-3")).toBeInTheDocument();
    const runs = screen.getByRole("list", { name: "Finished runs" });
    expect(within(runs).getByText("Merged")).toBeInTheDocument();
    expect(within(runs).getByText("by dana")).toBeInTheDocument();
    expect(within(runs).getByText("Cancelled")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Outcome"), "cancelled");
    await user.type(screen.getByLabelText("Search history"), "proj-4");
    await vi.waitFor(() => expect(urls.some((u) => u.includes("outcome=cancelled") && u.includes("q=proj-4"))).toBe(true));
    expect(urls.some((u) => u.includes("days=14"))).toBe(true);
  });

  it("shows a ticket's earlier attempts on the board", async () => {
    const user = userEvent.setup();
    mockFetch((url) => {
      if (url === "/admin/programs/p1/board") {
        return jsonResponse({
          generated_at: "", program_id: "p1", parallel_limit: 1, columns: ["coding", "done"],
          items: [{ key: "PROJ-3", title: "Roles", column: "coding", blocked_by: [], item: item("PROJ-3", "agent"),
                    attempts: [{ ...item("PROJ-3", "done"), workflow_id: "old", outcome: "cancelled", decided_by: "dana" }] }],
          totals: [],
        });
      }
      return jsonResponse({});
    });
    renderPage(<ProgramBoard programId="p1" />);
    await user.click(await screen.findByRole("button", { name: /PROJ-3/ }));
    await user.click(screen.getByText("All attempts (2)"));
    const earlier = screen.getByLabelText("PROJ-3 earlier attempts");
    expect(earlier).toHaveTextContent("Attempt 1: Cancelled by dana");
    expect(within(earlier).getByLabelText("PROJ-3 attempt 1 timeline")).toBeInTheDocument();
  });
});
