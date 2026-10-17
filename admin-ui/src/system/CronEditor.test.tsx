import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { CronEditor } from "./CronEditor";

function installDialogMethods() {
  HTMLDialogElement.prototype.showModal = vi.fn(function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  });
  HTMLDialogElement.prototype.close = vi.fn(function close(this: HTMLDialogElement) {
    this.removeAttribute("open");
  });
}

beforeEach(() => {
  installDialogMethods();
});

it("opens create cron job in a dialog above the table", async () => {
  mockFetch(() =>
    jsonResponse({
      jobs: [
        {
          name: "daily-standup",
          agent_id: "comms-agent",
          cron: "0 9 * * *",
          prompt: "standup",
          next_run: "2026-08-08T13:00:00+00:00",
        },
      ],
    })
  );

  renderPage(<CronEditor />);
  expect(await screen.findByText("daily-standup")).toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: /new job/i }));

  const dialog = await screen.findByRole("dialog", { name: /create cron job/i });
  expect(within(dialog).getByLabelText("Name")).toBeInTheDocument();
  expect(screen.getByText("daily-standup")).toBeInTheDocument();
});

it("opens edit cron job in a dialog and closes on cancel", async () => {
  mockFetch(() =>
    jsonResponse({
      jobs: [
        {
          name: "daily-standup",
          agent_id: "comms-agent",
          cron: "0 9 * * *",
          prompt: "standup",
          timezone: "UTC",
          skill_id: "",
          active_start: "00:00",
          active_end: "23:59",
        },
      ],
    })
  );

  renderPage(<CronEditor />);
  expect(await screen.findByText("daily-standup")).toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: /^edit$/i }));

  const dialog = await screen.findByRole("dialog", { name: /edit cron job/i });
  expect(within(dialog).getByLabelText("Name")).toHaveValue("daily-standup");

  await userEvent.click(within(dialog).getByRole("button", { name: /cancel/i }));
  await waitFor(() => {
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

it("opens an existing job with attachSkill set", async () => {
  mockFetch(() =>
    jsonResponse({
      jobs: [
        {
          name: "daily-standup",
          agent_id: "comms-agent",
          cron: "0 9 * * *",
          prompt: "standup",
          timezone: "UTC",
          skill_id: "",
          active_start: "00:00",
          active_end: "23:59",
        },
      ],
    })
  );
  window.location.hash = "#/system/cron?attachSkill=hello-skill&job=daily-standup";
  renderPage(<CronEditor attachSkill="hello-skill" attachJob="daily-standup" />);
  const dialog = await screen.findByRole("dialog", { name: /edit cron job/i });
  expect(within(dialog).getByLabelText("Skill id")).toHaveValue("hello-skill");
  expect(window.location.hash).toBe("#/system/cron");
});

it("opens a used-by cron job from the job query alone", async () => {
  mockFetch(() =>
    jsonResponse({
      jobs: [
        {
          name: "daily-standup",
          agent_id: "comms-agent",
          cron: "0 9 * * *",
          prompt: "standup",
          timezone: "UTC",
          skill_id: "ops",
          active_start: "00:00",
          active_end: "23:59",
        },
      ],
    })
  );
  window.location.hash = "#/system/cron?job=daily-standup";
  renderPage(<CronEditor attachJob="daily-standup" />);
  const dialog = await screen.findByRole("dialog", { name: /edit cron job/i });
  expect(within(dialog).getByLabelText("Name")).toHaveValue("daily-standup");
  expect(within(dialog).getByLabelText("Skill id")).toHaveValue("ops");
});

function manyJobs(count: number) {
  return Array.from({ length: count }, (_, index) => ({
    name: `job-${String(index + 1).padStart(2, "0")}`,
    agent_id: "ops-agent",
    cron: "0 10 * * 1",
    prompt: "work",
    timezone: "UTC",
    skill_id: "",
    active_start: "00:00",
    active_end: "23:59",
  }));
}

it("pages jobs ten at a time", async () => {
  mockFetch(() => jsonResponse({ jobs: manyJobs(12) }));
  renderPage(<CronEditor />);
  expect(await screen.findByText("job-01")).toBeInTheDocument();
  expect(screen.getByText("Showing 1–10 of 12 jobs")).toBeInTheDocument();
  expect(screen.queryByText("job-11")).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Next" }));
  expect(await screen.findByText("job-11")).toBeInTheDocument();
  expect(screen.getByText("job-12")).toBeInTheDocument();
  expect(screen.queryByText("job-01")).not.toBeInTheDocument();
});

it("opens an attached job on its page", async () => {
  mockFetch(() => jsonResponse({ jobs: manyJobs(12) }));
  renderPage(<CronEditor attachJob="job-12" />);
  expect(await screen.findByText("job-12")).toBeInTheDocument();
  expect(screen.getByText("Showing 11–12 of 12 jobs")).toBeInTheDocument();
  expect(screen.queryByText("job-01")).not.toBeInTheDocument();
  expect(await screen.findByRole("dialog", { name: /edit cron job/i })).toBeInTheDocument();
});

it("shows a banner for an unknown attach job", async () => {
  mockFetch(() => jsonResponse({ jobs: [] }));
  renderPage(<CronEditor attachSkill="hello-skill" attachJob="missing-job" />);
  expect(await screen.findByText(/Unknown cron job: missing-job/i)).toBeInTheDocument();
});

function programFetch(saved: Array<{ url: string; method: string; body: any }>, jobs: unknown[] = []) {
  return mockFetch((url, init) => {
    if (url === "/admin/program-definitions") {
      return jsonResponse({ definitions: [{ program_type: "account_digest" }] });
    }
    if (url.startsWith("/admin/cron/jobs") && init?.method && init.method !== "GET") {
      saved.push({ url, method: init.method, body: JSON.parse(String(init.body || "{}")) });
      return jsonResponse({ job: {} });
    }
    return jsonResponse({ jobs });
  });
}

it("creates a Start program job with program config and Slack notify", async () => {
  const saved: Array<{ url: string; method: string; body: any }> = [];
  programFetch(saved);
  renderPage(<CronEditor />);
  await userEvent.click(await screen.findByRole("button", { name: /new job/i }));
  const dialog = await screen.findByRole("dialog", { name: /create cron job/i });

  await userEvent.selectOptions(within(dialog).getByLabelText("Job type"), "program");
  expect(within(dialog).queryByLabelText("Prompt")).not.toBeInTheDocument();
  await userEvent.type(within(dialog).getByLabelText("Name"), "weekly-digest");
  await screen.findByRole("option", { name: "account_digest" });
  await userEvent.selectOptions(within(dialog).getByLabelText("Program type"), "account_digest");
  await userEvent.type(within(dialog).getByLabelText("Run summary"), "Weekly digest");
  const artifacts = within(dialog).getByLabelText("Artifacts (JSON)");
  await userEvent.clear(artifacts);
  await userEvent.type(artifacts, '{{"region": "emea"}');
  await userEvent.type(within(dialog).getByLabelText("Post to Slack channel"), "C9");
  await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));

  await waitFor(() => expect(saved).toHaveLength(1));
  expect(saved[0].method).toBe("POST");
  expect(saved[0].body).toMatchObject({
    name: "weekly-digest",
    kind: "system",
    handler: "start_program",
    agent_id: "ops-agent",
    program: {
      program_type: "account_digest",
      summary: "Weekly digest",
      artifacts: { region: "emea" },
      notify: { channel: "C9" },
    },
  });
  expect(saved[0].body).not.toHaveProperty("prompt");
});

it("rejects non-object artifacts JSON", async () => {
  const saved: Array<{ url: string; method: string; body: any }> = [];
  programFetch(saved);
  renderPage(<CronEditor attachProgram="account_digest" />);
  const dialog = await screen.findByRole("dialog", { name: /create cron job/i });
  const artifacts = within(dialog).getByLabelText("Artifacts (JSON)");
  await userEvent.clear(artifacts);
  await userEvent.type(artifacts, "[[1]");
  await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));
  expect((await screen.findAllByText("Artifacts must be a JSON object")).length).toBeGreaterThan(0);
  expect(saved).toHaveLength(0);
});

it("prefills a Start program job from attachProgram and round-trips an existing one", async () => {
  const saved: Array<{ url: string; method: string; body: any }> = [];
  programFetch(saved, [
    {
      name: "weekly-digest",
      kind: "system",
      handler: "start_program",
      agent_id: "ops-agent",
      cron: "0 9 * * 1",
      program: { program_type: "account_digest", summary: "Weekly", artifacts: { region: "emea" } },
    },
  ]);
  renderPage(<CronEditor attachProgram="account_digest" />);
  const dialog = await screen.findByRole("dialog", { name: /create cron job/i });
  expect(within(dialog).getByLabelText("Job type")).toHaveValue("program");
  expect(within(dialog).getByLabelText("Program type")).toHaveValue("account_digest");
  await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

  expect(screen.getByText("program: account_digest")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Edit" }));
  const edit = await screen.findByRole("dialog", { name: /edit cron job/i });
  expect(within(edit).getByLabelText("Artifacts (JSON)")).toHaveValue('{\n  "region": "emea"\n}');
  await userEvent.click(within(edit).getByRole("button", { name: "Save" }));
  await waitFor(() => expect(saved).toHaveLength(1));
  expect(saved[0].method).toBe("PUT");
  expect(saved[0].body.program).toEqual({
    program_type: "account_digest",
    summary: "Weekly",
    artifacts: { region: "emea" },
  });
});
