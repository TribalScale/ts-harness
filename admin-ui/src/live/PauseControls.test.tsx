import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { CodingPauseCard } from "../system/CodingPauseCard";
import { LivePage } from "./LivePage";
import { ProgramBoard } from "./ProgramBoard";

const item = (key: string, extra: Record<string, unknown> = {}) => ({
  workflow_id: `wf-${key}`, workflow_type: "implement_jira_ticket", label: key, state: "Code review running", kind: "running",
  current_step: "code_review", revisions: 1, pr_url: "", stuck_task_id: "", coding_job: null, jira_issue_key: key,
  waiting_on: "agent", in_step_since: "", in_step_seconds: 60, started_at: "", elapsed_seconds: 600, stronger_model: false,
  program_id: "p1", program_label: "Alpha", timeline: [], paused: null, held: "",
  ...extra,
});

const PAUSED_BY_ALICE = { scope: "ticket", target_id: "wf-PROJ-7", by: "alice@example.com", at: "2026-11-28T10:00:00Z", reason: "waiting on design" };

afterEach(() => vi.restoreAllMocks());

describe("Pause and resume", () => {
  it("shows a held revision on Live with who paused it, and resumes it", async () => {
    const user = userEvent.setup();
    const posted: { url: string; body: string }[] = [];
    mockFetch((url, init) => {
      if (url === "/admin/resume" || url === "/admin/pause") {
        posted.push({ url, body: String(init?.body) });
        return jsonResponse({ scope: "ticket", target_id: "wf-PROJ-7", status: "resumed", message: "Resumed PROJ-7. 1 held step picked up where it stopped." });
      }
      if (url === "/admin/live") {
        return jsonResponse({
          generated_at: "", capacity: { running: 0, queued: 0, limit: 2 }, paused: null,
          items: [
            item("PROJ-7", { state: "Paused (revision waiting)", kind: "paused", waiting_on: "paused", held: "revision", paused: PAUSED_BY_ALICE }),
            item("PROJ-8"),
          ],
        });
      }
      return jsonResponse({});
    });
    renderPage(<LivePage />);
    expect(await screen.findByText("Paused (revision waiting)")).toBeInTheDocument();
    expect(screen.getByText(/Paused by alice@example.com/)).toBeInTheDocument();
    expect(screen.getByText(/waiting on design/)).toBeInTheDocument();
    expect(screen.getByLabelText("Coding capacity")).toHaveTextContent("1 paused");
    await user.click(screen.getByRole("button", { name: "Resume PROJ-7" }));
    expect(posted[0].url).toBe("/admin/resume");
    expect(posted[0].body).toContain('"workflow_id":"wf-PROJ-7"');
    expect(posted[0].body).toContain('"scope":"ticket"');
    expect(await screen.findByText(/picked up where it stopped/)).toBeInTheDocument();
    // The other ticket can be paused, with an optional reason.
    await user.click(screen.getByRole("button", { name: "Pause PROJ-8" }));
    await user.type(screen.getByLabelText("Reason for pausing PROJ-8 (optional)"), "freeze");
    await user.click(screen.getByRole("button", { name: "Confirm pause" }));
    expect(posted[1].url).toBe("/admin/pause");
    expect(posted[1].body).toContain('"reason":"freeze"');
  });

  it("shows the all-coding banner on Live when an admin paused everything", async () => {
    mockFetch((url) => {
      if (url === "/admin/live") {
        return jsonResponse({
          generated_at: "", capacity: { running: 1, queued: 3, limit: 2 }, items: [],
          paused: { scope: "global", by: "admin@example.com", at: "2026-11-28T09:00:00Z", reason: "maintenance" },
        });
      }
      return jsonResponse({});
    });
    renderPage(<LivePage />);
    expect(await screen.findByText("All coding is paused.")).toBeInTheDocument();
    expect(screen.getByText(/Paused by admin@example.com/)).toBeInTheDocument();
  });

  it("puts a held ticket in the Paused column and pauses the program from its board", async () => {
    const user = userEvent.setup();
    const posted: string[] = [];
    mockFetch((url, init) => {
      if (url === "/admin/pause") {
        posted.push(String(init?.body));
        return jsonResponse({ scope: "program", target_id: "p1", status: "paused", message: "Paused Program Alpha." });
      }
      if (url === "/admin/programs/p1/board") {
        return jsonResponse({
          generated_at: "", program_id: "p1", parallel_limit: 2, paused: null, totals: [],
          columns: ["waiting_on_blockers", "ready", "coding", "in_review", "waiting_for_you", "paused", "done", "stopped"],
          items: [{ key: "PROJ-7", title: "Checkout", column: "paused", blocked_by: [],
                    item: item("PROJ-7", { state: "Paused (revision waiting)", kind: "paused", waiting_on: "paused", paused: PAUSED_BY_ALICE }) }],
        });
      }
      return jsonResponse({});
    });
    renderPage(<ProgramBoard programId="p1" />);
    expect(await screen.findByRole("list", { name: "Paused" })).toHaveTextContent("PROJ-7");
    expect(screen.getByText("paused by alice@example.com")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Pause this program" }));
    await user.click(screen.getByRole("button", { name: "Confirm pause" }));
    expect(posted[0]).toContain('"scope":"program"');
    expect(posted[0]).toContain('"program_id":"p1"');
  });

  it("lets an admin pause all coding from the System page and lists what is paused", async () => {
    const user = userEvent.setup();
    const posted: string[] = [];
    mockFetch((url, init) => {
      if (url === "/admin/pause" && init?.method === "POST") {
        posted.push(String(init?.body));
        return jsonResponse({ scope: "global", target_id: "", status: "paused", message: "Paused all coding." });
      }
      if (url === "/admin/pause") {
        return jsonResponse({
          global: null, can_pause_global: true, held: [],
          programs: [{ program_id: "p1", label: "Alpha", by: "bob@example.com", at: "2026-11-28T08:00:00Z" }],
          workflows: [{ workflow_id: "wf-PROJ-7", label: "PROJ-7", kind: "ticket", by: "alice@example.com", at: "2026-11-28T10:00:00Z",
                        held: [{ task_id: "t1", workflow_id: "wf-PROJ-7", step_name: "deliver_work_order", hold: { kind: "revision" }, still_paused: true }] }],
        });
      }
      return jsonResponse({});
    });
    renderPage(<CodingPauseCard />);
    expect(await screen.findByRole("link", { name: "Program Alpha" })).toHaveAttribute("href", "#/programs/p1");
    expect(screen.getByText(/revision waiting/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume PROJ-7" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Pause all coding" }));
    await user.click(screen.getByRole("button", { name: "Confirm pause" }));
    expect(posted[0]).toContain('"scope":"global"');
  });

  it("tells a non-admin that only an admin pauses all coding", async () => {
    mockFetch((url) =>
      url === "/admin/pause"
        ? jsonResponse({ global: null, can_pause_global: false, held: [], programs: [], workflows: [] })
        : jsonResponse({}),
    );
    renderPage(<CodingPauseCard />);
    expect(await screen.findByText("Only an admin can pause or resume all coding.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pause all coding" })).not.toBeInTheDocument();
  });
});
