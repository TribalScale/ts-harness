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
    const posted: string[] = [];
    mockFetch((url, init) => {
      if (url === "/admin/workflows/decide") {
        posted.push(String(init?.body));
        return jsonResponse({ status: "retried" });
      }
      if (url === "/admin/live") {
        return jsonResponse({
          generated_at: "", capacity: { running: 2, queued: 1, limit: 3 },
          items: [item("PROJ-4", "you", { state: "Ready to merge", stuck_task_id: "t9", program_id: "p1" }),
                  item("PROJ-3", "agent", { program_id: "p1", coding_job: {
                    id: "j1", status: "running", last_output: "npm test", started_at: new Date(Date.now() - 400000).toISOString(),
                    created_at: "", model: "claude-opus-5-5",
                    progress: { phase: "review", phase_seconds: 700, detail: "fixing test failures (pass 1)", done: { setup: 5, coding: 320, verification: 75 },
                                steps: { review: [{ label: "reviewing (round 1)", seconds: 100 },
                                                  { label: "fixing 2 review findings", seconds: 540 },
                                                  { label: "fixing test failures (pass 1)", seconds: 60, active: true }] } },
                  } })],
        });
      }
      if (url.startsWith("/admin/programs/p1/tickets/")) return jsonResponse(STORY);
      return jsonResponse({});
    });
    renderPage(<LivePage />);
    expect(await screen.findByText("PROJ-4")).toBeInTheDocument();
    const capacity = screen.getByLabelText("Coding capacity");
    expect(capacity).toHaveTextContent("2 of 3 coding jobs running");
    expect(capacity).toHaveTextContent("1 waiting for you");
    expect(screen.getByText("Waiting for you")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Decide in Inbox" })).toHaveAttribute("href", "#/inbox/stuck_step/t9");
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(posted[0]).toContain('"action":"retry"');
    expect(posted[0]).toContain('"task_id":"t9"');
    // The running ticket's card breaks its coding run into phases.
    const run = screen.getByLabelText("Current coding run");
    expect(run).toHaveTextContent("on Opus");
    expect(run).toHaveTextContent("Coding5 m 20 s");
    expect(run).toHaveTextContent("Running tests1 m 15 s");
    // The pre-push phase lists its own timed steps instead of one 11-minute "Pre-push review".
    expect(run).toHaveTextContent("Pre-push review11 m 40 s");
    expect(run).not.toHaveTextContent("11 m 40 s · fixing");
    const steps = within(run).getAllByRole("listitem").filter((li) => li.className.includes("story-step"));
    expect(steps.map((li) => li.textContent)).toEqual([
      "reviewing (round 1)1 m 40 s",
      "fixing 2 review findings9 m 00 s",
      "fixing test failures (pass 1)1 m 00 s",
    ]);
    expect(steps[2].className).toContain("story-step-active");
    await user.click(screen.getAllByRole("button", { name: "Details" })[0]);
    const story = await screen.findByLabelText("What happened");
    expect(story).toHaveTextContent("Revision 4");
    expect(story).toHaveTextContent("Opus · 12 m 47 s · $2.19");
    expect(story).toHaveTextContent("Setup 50 s · Coding 5 m 50 s · Running tests 4 m 52 s · Pre-push review 1 m 04 s · Push 3 s");
    expect(story).toHaveTextContent("Pre-push review: found and fixed 1 blocking issue");
    expect(story).toHaveTextContent("Pre-push review: reviewing (round 1) 40 s · fixing 1 review finding 24 s");
    expect(story).toHaveTextContent("src/a.ts:4");
    expect(within(story).queryByText("Third finding")).not.toBeInTheDocument();
    await user.click(within(story).getByRole("button", { name: "Show 1 more" }));
    expect(within(story).getByText("Third finding")).toBeInTheDocument();
    expect(screen.getByLabelText("Where the time went")).toHaveTextContent("Waiting for you 42 m 17 s");
  });

  it("formats durations", () => {
    expect([formatDuration(40), formatDuration(720), formatDuration(3 * 3600 + 300), formatDuration(3 * 86400)]).toEqual(
      ["40s", "12m", "3h 05m", "3d 0h"]);
  });
});

const finding = (message: string, line: number) => ({ severity: "major", category: "correctness", file: "src/a.ts", line, message });
const STORY = {
  workflow_id: "wf", generated_at: "", started_at: "", cost_usd: 2.19, current_run: null,
  totals: [{ category: "coding", label: "Coding", seconds: 767, share: 0.3 },
           { category: "you", label: "Waiting for you", seconds: 2537, share: 0.7 }],
  events: [
    { at: "2026-01-02T10:00:00Z", kind: "gate", title: "Code review blocked the PR", subtitle: "3 blocking findings", status: "blocked",
      findings: [finding("First finding", 4), finding("Second finding", 9), finding("Third finding", 12)] },
    { at: "2026-01-02T10:45:00Z", kind: "coding", title: "Revision 4", subtitle: "after /revise by ada@example.com", status: "ok",
      model: "Opus", seconds: 767, cost_usd: 2.19, outcome: "Pushed to the PR",
      phases: { setup: 49.8, coding: 349.8, verification: 291.8, review: 63.7, push: 3.1 },
      steps: { review: [{ label: "reviewing (round 1)", seconds: 40 }, { label: "fixing 1 review finding", seconds: 23.7 }] },
      review: { status: "pass", fixed: 1, reviews: 2, open_blocking: [] } },
  ],
};

describe("Program board", () => {
  it("puts tickets in columns with their blockers and shows where the time went", async () => {
    const user = userEvent.setup();
    mockFetch((url) => {
      if (url.startsWith("/admin/programs/p1/tickets/")) return jsonResponse(STORY);
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
    expect(await screen.findByLabelText("What happened")).toHaveTextContent("Code review blocked the PR");
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

describe("Metrics", () => {
  const ticket = (key: string, extra: Record<string, unknown> = {}) => ({
    workflow_id: `wf-${key}`, program_id: "p1", program_label: "Alpha", jira_issue_key: key, label: key,
    pr_url: "", project: "PROJ", repo: "o/r", outcome: "merged", finished_at: "2026-01-12T10:00:00Z",
    revisions: 0, revisions_by_trigger: {}, first_try: true, time_to_merge_seconds: 6600, you_seconds: 1800,
    agent_seconds: 4000, coding_runs: 1, reviewed_runs: 1, review_fixed: 1, gate_compared: 1, gate_disagreed: 0,
    cost_usd: 2, strong_cost_usd: 0, lost_jobs: 0, ...extra,
  });

  it("shows the headline numbers, the weekly trend and the tickets by cost", async () => {
    const user = userEvent.setup();
    const urls: string[] = [];
    mockFetch((url) => {
      urls.push(url);
      if (url.startsWith("/admin/programs/p1/tickets/")) return jsonResponse(STORY);
      if (url.startsWith("/admin/metrics")) {
        return jsonResponse({
          generated_at: "", days: 14, include_running: false,
          summary: {
            tickets: 2, finished: 2, merged: 2, first_try: 1, first_try_rate: 0.5,
            revisions: { median: 1, mean: 1, max: 2, total: 2, by_trigger: { code_review: 1, ci: 1 } },
            time_to_merge_seconds: { median: 9240, mean: 9240, max: 11940 },
            you_seconds: 6300, agent_seconds: 9000, you_share: 0.41, coding_runs: 5, reviewed_runs: 3,
            review_catch_rate: 0.333, review_fixed: 1, gate_compared: 2, gate_disagreed: 1, gate_disagreement_rate: 0.5,
            cost_usd: { median: 4, total: 8 }, strong_cost_share: 0.625, lost_jobs: 1,
          },
          weekly: [{ week: "2026-01-05", tickets: 3, first_try_rate: 0.33, revisions_mean: 1.67 },
                   { week: "2026-01-12", tickets: 2, first_try_rate: 0.5, revisions_mean: 1 }],
          tickets: [ticket("PROJ-2", { revisions: 2, revisions_by_trigger: { code_review: 1, ci: 1 }, first_try: false, cost_usd: 6 }),
                    ticket("PROJ-1")],
          facets: { projects: ["PROJ"], repos: ["o/r"] },
        });
      }
      return jsonResponse({ generated_at: "", items: [], capacity: { running: 0, queued: 0, limit: null } });
    });
    const onTab = vi.fn();
    const { rerender } = renderPage(<LivePage tab="now" onTab={onTab} />);
    await user.click(screen.getByRole("tab", { name: "Metrics" }));
    expect(onTab).toHaveBeenCalledWith("metrics");
    rerender(<LivePage tab="metrics" onTab={onTab} />);
    const tiles = await screen.findByLabelText("Headline numbers");
    expect(tiles).toHaveTextContent("First-try pass50%1 of 2 finished merged with no revision");
    expect(tiles).toHaveTextContent("1 code review, 1 CI");
    expect(tiles).toHaveTextContent("Time to merge2h 34m");
    expect(tiles).toHaveTextContent("Pre-push review catch rate33%");
    expect(tiles).toHaveTextContent("code review blocked 1 of 2 runs the pre-push review passed");
    expect(tiles).toHaveTextContent("Cost per ticket$4.00median · $8.00 total · 63% on the stronger model");
    expect(tiles).toHaveTextContent("Lost coding jobs1of 5 coding runs");
    expect(screen.getByLabelText("Weekly trend")).toHaveTextContent("2026-01-05333%1.67");
    const table = screen.getByRole("table", { name: "Tickets by cost" });
    const rows = within(table).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("PROJ-2");
    expect(rows[1]).toHaveTextContent("$6.00");
    expect(rows[2]).toHaveTextContent("PROJ-1");
    await user.click(within(rows[1]).getByRole("button", { name: "Story" }));
    expect(await screen.findByLabelText("What happened")).toHaveTextContent("Revision 4");
    await user.click(screen.getByLabelText("Include running tickets"));
    await vi.waitFor(() => expect(urls.some((u) => u.includes("running=1") && u.includes("days=14"))).toBe(true));
  });

  it("shows autonomy, the post-merge guard with unknowns, the improvement budget and groups by a versions key", async () => {
    const user = userEvent.setup();
    const urls: string[] = [];
    const guard = {
      window_days: 14, merged: 2, tickets_with_problems: 1, problem_rate: 0.5, fully_checked: 1, window_open: 1,
      signals: {
        reverts: { count: 1, checked: 2, unknown: 0 },
        jira_bugs: { count: null, checked: 0, unknown: 2 },
        smoke_failures: { count: 0, checked: 1, unknown: 1 },
        arbiter_overruled: { count: 1, checked: 2, unknown: 0 },
      },
    };
    const summary = {
      tickets: 2, finished: 2, merged: 2, first_try: 1, first_try_rate: 0.5,
      revisions: { median: 1, mean: 1, max: 2, total: 2, by_trigger: {} },
      time_to_merge_seconds: { median: 9240, mean: 9240, max: 11940 },
      you_seconds: 0, agent_seconds: 0, you_share: null, coding_runs: 2, reviewed_runs: 0,
      review_catch_rate: null, review_fixed: 0, gate_compared: 0, gate_disagreed: 0, gate_disagreement_rate: null,
      cost_usd: { median: 4, total: 8 }, strong_cost_share: null, lost_jobs: 0,
      autonomy: { autonomous: 1, rate: 0.5, merged_share: 0.5, intervened: 1, by_kind: { revise_grant: 1 } },
      guard,
    };
    mockFetch((url) => {
      urls.push(url);
      if (url.startsWith("/admin/metrics")) {
        return jsonResponse({
          generated_at: "", days: 14, include_running: false, summary, weekly: [],
          tickets: [ticket("PROJ-2", { interventions: ["revise_grant"], autonomous: false }), ticket("PROJ-1", { interventions: [], autonomous: true })],
          facets: { projects: ["PROJ"], repos: ["o/r"], group_keys: ["models.coding", "variants.pre_push"] },
          ...(url.includes("group_by=variants.pre_push")
            ? { group_by: "variants.pre_push", groups: [{ value: "gate_inputs", tickets: 1, summary }, { value: "(none)", tickets: 1, summary: { ...summary, guard: { ...guard, signals: { ...guard.signals, reverts: { count: null, checked: 0, unknown: 1 }, smoke_failures: { count: null, checked: 0, unknown: 1 } } } } }] }
            : {}),
          improvement_budget: { month: "2026-01", cap_usd: 150, spent_usd: 150, remaining_usd: 0, paused: true, by_purpose: {}, available: true },
        });
      }
      return jsonResponse({ generated_at: "", items: [], capacity: { running: 0, queued: 0, limit: null } });
    });
    renderPage(<LivePage tab="metrics" onTab={vi.fn()} />);
    const tiles = await screen.findByLabelText("Headline numbers");
    expect(tiles).toHaveTextContent("Merged without a person50%1 of 2 finished · 1 needed a person (1 /revise)");
    expect(tiles).toHaveTextContent("Problems after merge50%1 of 2 merged within 14 days");
    expect(tiles).toHaveTextContent("reverts 1 · Jira bugs unknown · smoke failures 0 (1 unknown) · arbiter overruled 1");
    expect(tiles).toHaveTextContent("Improvement budget$150.00 of $150.00");
    expect(tiles).toHaveTextContent("cap reached: improvement work paused until next month");
    const table = screen.getByRole("table", { name: "Tickets by cost" });
    const rows = within(table).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("/revise");
    expect(rows[2]).toHaveTextContent("No");
    await user.selectOptions(screen.getByLabelText("Group by"), "variants.pre_push");
    await vi.waitFor(() => expect(urls.some((u) => u.includes("group_by=variants.pre_push"))).toBe(true));
    const groups = await screen.findByLabelText("By version");
    expect(groups).toHaveTextContent("By variants.pre_push");
    const groupRows = within(groups).getAllByRole("row");
    expect(groupRows[1]).toHaveTextContent("gate_inputs150%50%50%$4.00");
    expect(groupRows[2]).toHaveTextContent("(none)150%50%unknown");
  });

  it("shows unknown when the improvement budget cannot be read", async () => {
    mockFetch((url) => {
      if (url.startsWith("/admin/metrics")) {
        return jsonResponse({
          generated_at: "", days: 14, include_running: false,
          summary: {
            tickets: 0, finished: 0, merged: 0, first_try: 0, first_try_rate: null,
            revisions: { median: null, mean: null, max: null, total: 0, by_trigger: {} },
            time_to_merge_seconds: { median: null, mean: null, max: null },
            you_seconds: 0, agent_seconds: 0, you_share: null, coding_runs: 0, reviewed_runs: 0,
            review_catch_rate: null, review_fixed: 0, gate_compared: 0, gate_disagreed: 0, gate_disagreement_rate: null,
            cost_usd: { median: null, total: 0 }, strong_cost_share: null, lost_jobs: 0,
          },
          weekly: [], tickets: [], facets: { projects: [], repos: [] },
          improvement_budget: { month: "2026-01", cap_usd: 150, spent_usd: null, remaining_usd: null, paused: true, by_purpose: {}, available: false },
        });
      }
      return jsonResponse({ generated_at: "", items: [], capacity: { running: 0, queued: 0, limit: null } });
    });
    renderPage(<LivePage tab="metrics" onTab={vi.fn()} />);
    const tiles = await screen.findByLabelText("Headline numbers");
    expect(tiles).toHaveTextContent("Improvement budgetunknowncap $150.00 for 2026-01");
  });

  it("lists the harness issues and self-fix PRs, and what an admin must configure", async () => {
    mockFetch((url) => {
      if (url.startsWith("/admin/metrics")) {
        return jsonResponse({
          generated_at: "", days: 14, include_running: false,
          summary: {
            tickets: 0, finished: 0, merged: 0, first_try: 0, first_try_rate: null,
            revisions: { median: null, mean: null, max: null, total: 0, by_trigger: {} },
            time_to_merge_seconds: { median: null, mean: null, max: null },
            you_seconds: 0, agent_seconds: 0, you_share: null, coding_runs: 0, reviewed_runs: 0,
            review_catch_rate: null, review_fixed: 0, gate_compared: 0, gate_disagreed: 0, gate_disagreement_rate: null,
            cost_usd: { median: null, total: 0 }, strong_cost_share: null, lost_jobs: 0,
          },
          weekly: [], tickets: [], facets: { projects: [], repos: [] },
          harness_loop: {
            settings: { repo: "org/ts-harness", branch: "dev", file_issues: true, self_fix: true, problems: [] },
            available: true,
            problems: ["Self-fixes are waiting: no GitHub credentials for org/ts-harness."],
            limits: { open_fixes: 1, max_open: 2, fixes_last_7_days: 1, max_per_week: 3 },
            issues: [
              {
                id: "i1", fingerprint: "abcdef0123456789", source: "arbiter", kind: "code",
                component: "runtime/ts_harness/workflows/engine.py", title: "KeyError: 'pr_url'", occurrences: 3,
                first_seen: "", last_seen: "", issue_number: 12, issue_url: "https://github.com/org/ts-harness/issues/12",
                issue_state: "open", fix_status: "pr_open", fix_reason: "", fix_pr_url: "https://github.com/org/ts-harness/pull/13",
                fix_started_at: "", fix_charged_usd: 1.5, last_error: "",
              },
            ],
          },
        });
      }
      return jsonResponse({ generated_at: "", items: [], capacity: { running: 0, queued: 0, limit: null } });
    });
    renderPage(<LivePage tab="metrics" onTab={vi.fn()} />);
    const section = await screen.findByLabelText("Harness issues");
    expect(section).toHaveTextContent("org/ts-harness · filing on · self-fix on into dev · 1 of 2 self-fix PRs open · 1 of 3 this week");
    expect(section).toHaveTextContent("no GitHub credentials");
    expect(within(section).getByRole("link", { name: "#12" })).toHaveAttribute("href", "https://github.com/org/ts-harness/issues/12");
    expect(within(section).getByRole("link", { name: "PR" })).toHaveAttribute("href", "https://github.com/org/ts-harness/pull/13");
    expect(section).toHaveTextContent("Harness issue");
    expect(section).toHaveTextContent("draft PR open");
  });

  it("lists improvement proposals with their canary numbers and the month's spend", async () => {
    const user = userEvent.setup();
    mockFetch((url) => {
      if (url.startsWith("/admin/improvements")) {
        return jsonResponse({
          available: true,
          budget: { month: "2026-11", cap_usd: 150, spent_usd: 12.5, remaining_usd: 137.5, paused: false, by_purpose: {}, available: true },
          rules: {
            replay: { cases: 10, min_each: 3, catch_margin: 0.1 },
            canary: { share: 0.5, min_tickets: 8, autonomy_margin: 0.125, max_days: 45, enabled: true },
          },
          proposals: [
            {
              id: 4, type: "skill_edit", status: "canary", target: "pre_push_review", title: "Ask for retry tests",
              summary: "s", pattern_key: "review:tests", metric: "autonomy", expected_effect: "", risk: "",
              change: {}, evidence: { tickets: 3 }, evaluation: { status: "passed" }, cost_usd: 2.25,
              canary: {
                variant: "proposal-4", share: 0.5, started_at: "2026-11-01T00:00:00Z",
                latest: {
                  checked_at: "2026-11-20T00:00:00Z",
                  candidate: { tickets: 3, finished: 3, merged: 3, autonomy_rate: 1, problem_rate: 0, tickets_with_problems: 0 },
                  control: { tickets: 4, finished: 4, merged: 4, autonomy_rate: 0.5, problem_rate: 0, tickets_with_problems: 0 },
                  decision: { action: "wait", reason: "3/8 candidate and 4/8 control finished" },
                },
              },
            },
          ],
        });
      }
      if (url.startsWith("/admin/metrics")) {
        return jsonResponse({
          generated_at: "", days: 14, include_running: false,
          summary: {
            tickets: 0, finished: 0, merged: 0, first_try: 0, first_try_rate: null,
            revisions: { median: null, mean: null, max: null, total: 0, by_trigger: {} },
            time_to_merge_seconds: { median: null, mean: null, max: null },
            you_seconds: 0, agent_seconds: 0, you_share: null, coding_runs: 0, reviewed_runs: 0,
            review_catch_rate: null, review_fixed: 0, gate_compared: 0, gate_disagreed: 0, gate_disagreement_rate: null,
            cost_usd: { median: null, total: 0 }, strong_cost_share: null, lost_jobs: 0,
          },
          weekly: [], tickets: [], facets: { projects: [], repos: [] },
        });
      }
      return jsonResponse({ generated_at: "", items: [], capacity: { running: 0, queued: 0, limit: null } });
    });
    renderPage(<LivePage tab="metrics" onTab={vi.fn()} />);
    const section = await screen.findByLabelText("Improvements");
    expect(section).toHaveTextContent("This month $12.50 of $150.00 improvement budget · 1 live canary");
    const table = within(section).getByRole("table", { name: "Improvement proposals" });
    expect(within(table).getAllByRole("row")[1]).toHaveTextContent("#4 Ask for retry testsskill editcanary runningpassed100% vs 50% (3/4)$2.25");
    await user.click(within(table).getByRole("button", { name: "Details" }));
    expect(await within(section).findByText("3/8 candidate and 4/8 control finished")).toBeInTheDocument();
  });
});
