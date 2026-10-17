import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { setPermissions, setRoles } from "../auth/session";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { formatAge, portfolioUrl, ProgramsPage } from "./ProgramsPage";

const programId = "57249723-checkers-program";
const LIST_URL = "/admin/programs?portfolio=1";

function installMediaQuery(matches = true) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
}

function installDialogMethods() {
  HTMLDialogElement.prototype.showModal = vi.fn(function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  });
  HTMLDialogElement.prototype.close = vi.fn(function close(this: HTMLDialogElement) {
    this.removeAttribute("open");
  });
}

function programList(status = "running") {
  return {
    programs: [
      {
        id: programId,
        display_name: "checkers",
        summary: "Build checkers",
        current_phase: "implement",
        status,
        project_id: "TFS",
      },
      {
        id: "57249723-checkers-followup",
        display_name: "checkers",
        summary: "Build checkers follow-up",
        current_phase: "deploy",
        status: "waiting",
      },
    ],
  };
}

function programStatus(status = "running") {
  return {
    program_id: programId,
    summary: "checkers",
    current_phase: "implement",
    status,
    program_type: "software_delivery",
    program_definition_version: "1.0.0",
    program_definition_revision: "rev-built-in",
    phase_order: ["prd", "jira", "implement", "deploy"],
    active_workflow_id: "workflow-implement",
    topology: {
      program_type: "software_delivery",
      definition_version: "1.0.0",
      nodes: [
        { id: "prd", default_workflow_type: "prd_request" },
        { id: "jira", default_workflow_type: "jira_from_prd" },
        { id: "work_orders", default_workflow_type: "work_orders_from_prd" },
        { id: "implement", default_workflow_type: "implement_delivery" },
        { id: "deploy", default_workflow_type: "deploy_delivery", terminal: true },
      ],
      edges: [
        { source: "prd", target: "jira", mode: "manual" },
        { source: "prd", target: "work_orders", mode: "manual" },
        { source: "jira", target: "implement", mode: "manual" },
        { source: "work_orders", target: "implement", mode: "manual" },
        { source: "implement", target: "deploy", mode: "manual" },
      ],
    },
    resolved_workflow_versions: {
      prd: { workflow_type: "prd_request", definition_version: "1.0.0" },
      jira: { workflow_type: "jira_from_prd", definition_version: "1.0.0" },
      work_orders: { workflow_type: "work_orders_from_prd", definition_version: "1.0.0" },
      implement: { workflow_type: "implement_delivery", definition_version: "1.0.0" },
      deploy: { workflow_type: "deploy_delivery", definition_version: "1.0.0" },
    },
    phases: {
      prd: { state: "succeeded", workflow_id: "workflow-prd", workflow_type: "prd_request" },
      jira: { state: "not_started" },
      work_orders: { state: "not_started" },
      implement: {
        state: "running",
        workflow_id: "workflow-implement",
        workflow_type: "implement_delivery",
        active_step: "write code",
      },
      deploy: { state: "not_started" },
    },
    artifacts: {
      pull_request_url: "https://github.com/example-org/project/pull/3",
      work_orders_markdown:
        "# Work orders\n\n- Build board\n- Verify release gates\n- Preserve active-phase kanban semantics\n- Capture approval handoffs for operators\n- Confirm post-mutation refresh behavior",
    },
    waiting_on: {
      kind: "workflow_gate",
      match: { task_id: "task-2", step_name: "Review implementation" },
    },
    recommended_action: "Approve the active implementation gate.",
    project_id: "TFS",
    project_id_source: "user",
    excluded_from_autoshare: false,
    outbound_link_count: 1,
    inbound_link_count: 0,
    links: { outbound: [{ to_program_id: "other-program" }], inbound: [] },
  };
}

function workflowTasks() {
  return {
    tasks: [
      { id: "task-1", step_name: "Implement board", status: "running" },
      {
        id: "task-2",
        step_name: "Review implementation",
        status: "blocked",
        approval_required: true,
        approval_status: "pending",
      },
    ],
  };
}

beforeEach(() => {
  installMediaQuery(true);
  installDialogMethods();
});

it("renders compact distinguishable program rows and overview/tasks/artifacts tabs", async () => {
  mockFetch((url) => {
    if (url === LIST_URL) return jsonResponse(programList());
    if (url === `/admin/programs/${encodeURIComponent(programId)}/status`) {
      return jsonResponse(programStatus());
    }
    if (url === "/admin/workflows/workflow-implement") return jsonResponse(workflowTasks());
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<ProgramsPage programId={programId} onSelect={() => undefined} />);

  expect((await screen.findAllByText("checkers")).length).toBeGreaterThan(0);
  expect(screen.getAllByText(/implement/i).length).toBeGreaterThan(0);
  expect(screen.getAllByText(/57249723/)).toHaveLength(2);
  expect(screen.getByRole("tab", { name: /overview/i })).toBeInTheDocument();
  expect(await screen.findByRole("tab", { name: /tasks \(2\)/i })).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: /artifacts \(2\)/i })).toBeInTheDocument();

  await userEvent.click(screen.getByRole("tab", { name: /tasks/i }));
  expect(await screen.findByText("Implement board")).toBeInTheDocument();

  await userEvent.click(screen.getByRole("tab", { name: /artifacts/i }));
  expect(screen.getByRole("link", { name: /pull request/i })).toHaveAttribute(
    "href",
    "https://github.com/example-org/project/pull/3"
  );
  expect(screen.queryByText(/work_orders_markdown:/)).not.toBeInTheDocument();
  expect(screen.getByText(/work orders markdown/i)).toBeInTheDocument();
  expect(screen.getByText(/view value/i)).toBeInTheDocument();
});

it("shows project memory controls on overview", async () => {
  mockFetch((url) => {
    if (url === LIST_URL) return jsonResponse(programList());
    if (url === `/admin/programs/${encodeURIComponent(programId)}/status`) {
      return jsonResponse(programStatus());
    }
    if (url === "/admin/workflows/workflow-implement") return jsonResponse(workflowTasks());
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<ProgramsPage programId={programId} onSelect={() => undefined} />);
  expect(await screen.findByLabelText("Project memory")).toBeInTheDocument();
  expect(screen.getAllByText(/Project TFS/).length).toBeGreaterThan(0);
  expect(screen.getByRole("button", { name: /save project/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /exclude from auto-share/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /add link/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /unlink/i })).toBeInTheDocument();
});

it("saves project, exclude, link, and unlink through admin APIs", async () => {
  const calls: Array<{ url: string; method?: string; body?: Record<string, unknown> }> = [];
  mockFetch((url, init) => {
    const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : undefined;
    calls.push({ url, method: init?.method || "GET", body });
    if (url === LIST_URL) return jsonResponse(programList());
    if (url === `/admin/programs/${encodeURIComponent(programId)}/status`) {
      return jsonResponse(programStatus());
    }
    if (url === "/admin/workflows/workflow-implement") return jsonResponse(workflowTasks());
    if (url === `/admin/programs/${encodeURIComponent(programId)}` && init?.method === "PATCH") {
      return jsonResponse({ ok: true });
    }
    if (url === `/admin/programs/${encodeURIComponent(programId)}/links` && init?.method === "POST") {
      return jsonResponse({ ok: true });
    }
    if (
      url === `/admin/programs/${encodeURIComponent(programId)}/links/other-program` &&
      init?.method === "DELETE"
    ) {
      return jsonResponse({ ok: true });
    }
    throw new Error(`Unexpected URL ${url} ${init?.method}`);
  });

  renderPage(<ProgramsPage programId={programId} onSelect={() => undefined} />);
  expect(await screen.findByLabelText("Project memory")).toBeInTheDocument();

  const projectInput = screen.getByLabelText("Program project");
  await userEvent.clear(projectInput);
  await userEvent.type(projectInput, "RFP");
  await userEvent.click(screen.getByRole("button", { name: /save project/i }));
  await waitFor(() => {
    expect(calls.some((call) => call.method === "PATCH" && call.body?.project_id === "RFP")).toBe(
      true
    );
  });

  await userEvent.click(screen.getByRole("button", { name: /exclude from auto-share/i }));
  await waitFor(() => {
    expect(
      calls.some((call) => call.method === "PATCH" && call.body?.excluded_from_autoshare === true)
    ).toBe(true);
  });

  await userEvent.type(screen.getByLabelText("Link target program"), "linked-program");
  await userEvent.click(screen.getByRole("button", { name: /add link/i }));
  await waitFor(() => {
    expect(
      calls.some((call) => call.method === "POST" && call.body?.to_program_id === "linked-program")
    ).toBe(true);
  });

  await userEvent.click(screen.getByRole("button", { name: /unlink/i }));
  await waitFor(() => {
    expect(
      calls.some(
        (call) =>
          call.method === "DELETE" &&
          call.url.endsWith(`/admin/programs/${encodeURIComponent(programId)}/links/other-program`)
      )
    ).toBe(true);
  });
});

it("opens a confirmation dialog before canceling and refreshes selected state after confirm", async () => {
  const calls: Array<{ url: string; method: string }> = [];
  mockFetch((url, init) => {
    calls.push({ url, method: init?.method || "GET" });
    if (url === LIST_URL) return jsonResponse(programList());
    if (url === `/admin/programs/${encodeURIComponent(programId)}/status`) {
      return jsonResponse(programStatus());
    }
    if (url === "/admin/workflows/workflow-implement") return jsonResponse(workflowTasks());
    if (url === `/admin/programs/${encodeURIComponent(programId)}/cancel`) {
      return jsonResponse({ ok: true });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<ProgramsPage programId={programId} onSelect={() => undefined} />);
  await screen.findAllByText("checkers");
  await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

  expect(screen.getByRole("dialog", { name: /cancel program/i })).toBeInTheDocument();
  expect(calls).not.toContainEqual({
    url: `/admin/programs/${encodeURIComponent(programId)}/cancel`,
    method: "POST",
  });

  await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: /cancel program/i }));

  await waitFor(() =>
    expect(calls).toContainEqual({
      url: `/admin/programs/${encodeURIComponent(programId)}/cancel`,
      method: "POST",
    })
  );
  expect(
    calls.filter((call) => call.url === `/admin/programs/${encodeURIComponent(programId)}/status`)
  ).toHaveLength(2);
  expect(calls.filter((call) => call.url === LIST_URL)).toHaveLength(2);
  expect(screen.getAllByText("checkers").length).toBeGreaterThan(0);
});

it("keeps Tasks on the current phase workflow after inspecting another phase", async () => {
  const workflowUrls: string[] = [];
  mockFetch((url) => {
    if (url === LIST_URL) return jsonResponse(programList());
    if (url === `/admin/programs/${encodeURIComponent(programId)}/status`) {
      return jsonResponse(programStatus());
    }
    if (url === "/admin/workflows/workflow-implement") {
      workflowUrls.push(url);
      return jsonResponse(workflowTasks());
    }
    if (url === "/admin/workflows/workflow-prd") {
      workflowUrls.push(url);
      return jsonResponse({ tasks: [{ id: "prd-task", step_name: "Review PRD", status: "succeeded" }] });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<ProgramsPage programId={programId} onSelect={() => undefined} />);
  expect(await screen.findByRole("tab", { name: /tasks \(2\)/i })).toBeInTheDocument();

  const phases = screen.getByRole("list", { name: /program phases/i });
  await userEvent.click(within(phases).getByRole("button", { name: /^1\s*prd/i }));
  await userEvent.click(screen.getByRole("tab", { name: /tasks/i }));

  expect(await screen.findByText("Implement board")).toBeInTheDocument();
  expect(screen.queryByText("Review PRD")).not.toBeInTheDocument();
  expect(workflowUrls).not.toContain("/admin/workflows/workflow-prd");
});

it("keeps the program selected and surfaces the error when cancel fails", async () => {
  const onSelect = vi.fn();
  mockFetch((url) => {
    if (url === LIST_URL) return jsonResponse(programList());
    if (url === `/admin/programs/${encodeURIComponent(programId)}/status`) {
      return jsonResponse(programStatus());
    }
    if (url === "/admin/workflows/workflow-implement") return jsonResponse(workflowTasks());
    if (url === `/admin/programs/${encodeURIComponent(programId)}/cancel`) {
      return jsonResponse({ error: "program already cancelled" }, 409);
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<ProgramsPage programId={programId} onSelect={onSelect} />);
  await screen.findAllByText("checkers");
  await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
  await userEvent.click(
    within(screen.getByRole("dialog")).getByRole("button", { name: /cancel program/i })
  );

  expect(await screen.findByRole("alert")).toHaveTextContent("program already cancelled");
  expect(onSelect).not.toHaveBeenCalled();
  expect(screen.getByRole("group", { name: "Program actions" })).toBeInTheDocument();
});

it("shows loading instead of a load error before the first status response", () => {
  let resolveStatus: (value: Response) => void = () => undefined;
  const pendingStatus = new Promise<Response>((resolve) => {
    resolveStatus = resolve;
  });
  mockFetch((url) => {
    if (url === LIST_URL) return jsonResponse(programList());
    if (url === `/admin/programs/${encodeURIComponent(programId)}/status`) {
      return pendingStatus;
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<ProgramsPage programId={programId} onSelect={() => undefined} />);

  expect(screen.getByText("Loading program")).toBeInTheDocument();
  expect(screen.queryByText("Could not load program")).not.toBeInTheDocument();
  resolveStatus(jsonResponse(programStatus()));
});

it("refetches program status when retrying after a status error", async () => {
  const statusUrl = `/admin/programs/${encodeURIComponent(programId)}/status`;
  let failStatus = true;
  let statusCalls = 0;
  mockFetch((url) => {
    if (url === LIST_URL) return jsonResponse(programList());
    if (url === statusUrl) {
      statusCalls += 1;
      return failStatus
        ? jsonResponse({ error: "status unavailable" }, 400)
        : jsonResponse(programStatus());
    }
    if (url === "/admin/workflows/workflow-implement") return jsonResponse(workflowTasks());
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<ProgramsPage programId={programId} onSelect={() => undefined} />);

  expect(await screen.findByText("status unavailable")).toBeInTheDocument();
  const callsBeforeRetry = statusCalls;

  failStatus = false;
  await userEvent.click(screen.getByRole("button", { name: /try again/i }));

  await waitFor(() => expect(statusCalls).toBeGreaterThan(callsBeforeRetry));
  expect(await screen.findByRole("tab", { name: /overview/i })).toBeInTheDocument();
});

it("keeps program status and actions visible above every tab", async () => {
  mockFetch((url) => {
    if (url === LIST_URL) return jsonResponse(programList());
    if (url === `/admin/programs/${encodeURIComponent(programId)}/status`) {
      return jsonResponse(programStatus());
    }
    if (url === "/admin/workflows/workflow-implement") return jsonResponse(workflowTasks());
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<ProgramsPage programId={programId} onSelect={() => undefined} />);
  await screen.findAllByText("checkers");

  const header = screen.getByRole("region", { name: "Program detail header" });
  expect(header.querySelector(".program-waiting")).toHaveTextContent(/waiting on/i);
  expect(header.querySelector(".status-pill")).toHaveTextContent(/running/i);
  expect(within(header).getByRole("group", { name: "Program actions" })).toBeInTheDocument();

  for (const tabName of [/tasks/i, /artifacts/i, /overview/i]) {
    await userEvent.click(screen.getByRole("tab", { name: tabName }));
    const actions = screen.getByRole("group", { name: "Program actions" });
    expect(within(actions).getByRole("button", { name: "Advance" })).toBeInTheDocument();
    expect(within(actions).getByRole("button", { name: "Cancel" })).toBeInTheDocument();
  }

  expect(screen.getAllByRole("button", { name: "Advance" })).toHaveLength(1);
  expect(screen.getAllByRole("button", { name: "Cancel" })).toHaveLength(1);
});

it("connects each program tab to its rendered panel", async () => {
  mockFetch((url) => {
    if (url === LIST_URL) return jsonResponse(programList());
    if (url === `/admin/programs/${encodeURIComponent(programId)}/status`) {
      return jsonResponse(programStatus());
    }
    if (url === "/admin/workflows/workflow-implement") return jsonResponse(workflowTasks());
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<ProgramsPage programId={programId} onSelect={() => undefined} />);

  const tab = await screen.findByRole("tab", { name: /overview/i });
  const panel = screen.getByRole("tabpanel");
  expect(tab.getAttribute("aria-controls")).toBe(panel.id);
  expect(panel.getAttribute("aria-labelledby")).toBe(tab.id);
  const phases = screen.getByRole("list", { name: /program phases/i });
  expect(within(phases).getByRole("button", { name: /^1\s*prd/i })).toHaveAttribute(
    "aria-pressed",
    "false"
  );
});

it("shows pinned program/workflow definition links from the status payload", async () => {
  mockFetch((url) => {
    if (url === LIST_URL) return jsonResponse(programList());
    if (url === `/admin/programs/${encodeURIComponent(programId)}/status`) {
      return jsonResponse(programStatus());
    }
    if (url === "/admin/workflows/workflow-implement") return jsonResponse(workflowTasks());
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<ProgramsPage programId={programId} onSelect={() => undefined} />);
  await screen.findAllByText("checkers");

  expect(screen.getByLabelText(/pinned program definition/i)).toHaveTextContent(
    /software_delivery@1\.0\.0/
  );
  expect(screen.getByRole("link", { name: /software_delivery@1\.0\.0/i })).toHaveAttribute(
    "href",
    "#/workflows/programs/software_delivery"
  );
  expect(
    screen.getByRole("link", { name: /implement: implement_delivery@1\.0\.0/i })
  ).toHaveAttribute("href", "#/workflows/implement_delivery");
  expect(screen.getByText(/full topology/i)).toBeInTheDocument();
});

it("surfaces a list error instead of a false empty state", async () => {
  mockFetch((url) => {
    if (url === LIST_URL) {
      return jsonResponse({ error: "password authentication failed for user \"ts_harness\"" }, 502);
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<ProgramsPage onSelect={() => undefined} />);

  expect(await screen.findByRole("alert")).toHaveTextContent("Unable to load programs");
  expect(screen.getByText(/password authentication failed/i)).toBeInTheDocument();
  expect(screen.queryByText("No delivery programs")).not.toBeInTheDocument();
});

it("shows a loading state instead of a false empty state while programs load", async () => {
  let resolveList: (value: Response) => void = () => undefined;
  const pending = new Promise<Response>((resolve) => {
    resolveList = resolve;
  });
  mockFetch((url) => {
    if (url === LIST_URL) return pending;
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<ProgramsPage onSelect={() => undefined} />);

  expect(await screen.findByText("Loading programs")).toBeInTheDocument();
  expect(screen.queryByText("No delivery programs")).not.toBeInTheDocument();

  resolveList(jsonResponse(programList()));
  expect(await screen.findAllByText("checkers")).not.toHaveLength(0);
});

it("opens a confirmation dialog before advancing and refreshes selected state after confirm", async () => {
  const calls: Array<{ url: string; method: string }> = [];
  mockFetch((url, init) => {
    calls.push({ url, method: init?.method || "GET" });
    if (url === LIST_URL) return jsonResponse(programList());
    if (url === `/admin/programs/${encodeURIComponent(programId)}/status`) {
      return jsonResponse(programStatus());
    }
    if (url === "/admin/workflows/workflow-implement") return jsonResponse(workflowTasks());
    if (url === `/admin/programs/${encodeURIComponent(programId)}/advance`) {
      return jsonResponse({ ok: true });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<ProgramsPage programId={programId} onSelect={() => undefined} />);
  await screen.findAllByText("checkers");
  await userEvent.click(screen.getByRole("button", { name: "Advance" }));

  expect(screen.getByRole("dialog", { name: /advance program/i })).toBeInTheDocument();
  expect(calls).not.toContainEqual({
    url: `/admin/programs/${encodeURIComponent(programId)}/advance`,
    method: "POST",
  });

  await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: /advance program/i }));

  await waitFor(() =>
    expect(calls).toContainEqual({
      url: `/admin/programs/${encodeURIComponent(programId)}/advance`,
      method: "POST",
    })
  );
  expect(
    calls.filter((call) => call.url === `/admin/programs/${encodeURIComponent(programId)}/status`)
  ).toHaveLength(2);
  expect(calls.filter((call) => call.url === LIST_URL)).toHaveLength(2);
});

it("shows portfolio columns for root programs", async () => {
  mockFetch((url) => {
    if (url === LIST_URL) {
      return jsonResponse({
        programs: [
          {
            ...programList().programs[0],
            owner: "alice@example.com",
            child_count: 3,
            waiting_on_kind: "external",
            wait_age_seconds: 7200,
            escalated: true,
            recommended_action: "review_and_merge_or_cancel_pr",
          },
        ],
      });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<ProgramsPage onSelect={() => undefined} />);

  expect(await screen.findByText("Owner alice@example.com")).toBeInTheDocument();
  expect(screen.getByText("3 child programs")).toBeInTheDocument();
  expect(screen.getByText("Waiting on external for 2h")).toBeInTheDocument();
  expect(screen.getByText("Escalated")).toBeInTheDocument();
  expect(screen.getByText("Next: review and merge or cancel pr")).toBeInTheDocument();
});

it("applies portfolio filters to the list request", async () => {
  const urls: string[] = [];
  mockFetch((url) => {
    urls.push(url);
    if (url.startsWith("/admin/programs?portfolio=1")) return jsonResponse(programList());
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<ProgramsPage onSelect={() => undefined} />);
  await screen.findAllByText("checkers");

  await userEvent.selectOptions(screen.getByLabelText("Filter by status"), "blocked");
  await userEvent.type(screen.getByLabelText("Filter by phase"), "implement");
  await userEvent.type(screen.getByLabelText("Filter by owner"), "alice");
  await userEvent.type(screen.getByLabelText("Waiting more than hours"), "24");
  await userEvent.click(screen.getByRole("button", { name: /apply filters/i }));

  await waitFor(() =>
    expect(urls).toContain(
      "/admin/programs?portfolio=1&status=blocked&phase=implement&owner=alice&waiting_hours=24"
    )
  );
});

it("shows cancel cleanup failures on the program detail", async () => {
  mockFetch((url) => {
    if (url === LIST_URL) return jsonResponse(programList("cancelled"));
    if (url === `/admin/programs/${encodeURIComponent(programId)}/status`) {
      return jsonResponse({
        ...programStatus("cancelled"),
        waiting_on: null,
        artifacts: {
          compensation: {
            status: "partial",
            failures: [{ kind: "pull_request", ref: "https://github.com/o/r/pull/9", detail: "403 forbidden" }],
          },
        },
      });
    }
    if (url === "/admin/workflows/workflow-implement") return jsonResponse(workflowTasks());
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<ProgramsPage programId={programId} onSelect={() => undefined} />);

  const cleanup = await screen.findByLabelText("Cancel cleanup");
  expect(cleanup).toHaveTextContent("Cancel cleanup: partial");
  expect(cleanup).toHaveTextContent("pull request https://github.com/o/r/pull/9: 403 forbidden");
});

function mockProgramDetail(extra?: (url: string) => Response | undefined) {
  return mockFetch((url) => {
    const handled = extra?.(url);
    if (handled) return handled;
    if (url === LIST_URL) return jsonResponse(programList());
    if (url === `/admin/programs/${encodeURIComponent(programId)}/status`) {
      return jsonResponse(programStatus());
    }
    if (url === "/admin/workflows/workflow-implement") return jsonResponse(workflowTasks());
    throw new Error(`Unexpected URL ${url}`);
  });
}

it("hides the evidence download from non-admin roles", async () => {
  setRoles(["member"]);
  setPermissions(["view"]);
  mockProgramDetail();

  renderPage(<ProgramsPage programId={programId} onSelect={() => undefined} />);

  expect(await screen.findByRole("button", { name: /advance/i })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /download evidence/i })).not.toBeInTheDocument();
});

it("downloads signed program evidence and shows the short hash", async () => {
  setRoles(["admin"]);
  setPermissions(["view", "edit_config"]);
  const digest = "ab12cd34ef56".padEnd(64, "0");
  const evidenceUrl = `/admin/programs/${encodeURIComponent(programId)}/evidence`;
  const fetchSpy = mockProgramDetail((url) =>
    url === evidenceUrl
      ? new Response(JSON.stringify({ sha256: digest }), {
          status: 200,
          headers: { "Content-Type": "application/json", "X-Evidence-SHA256": digest },
        })
      : undefined
  );
  const createObjectURL = vi.fn(() => "blob:evidence");
  const revokeObjectURL = vi.fn();
  Object.assign(URL, { createObjectURL, revokeObjectURL });
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);

  renderPage(<ProgramsPage programId={programId} onSelect={() => undefined} />);
  await userEvent.click(await screen.findByRole("button", { name: /download evidence/i }));

  expect(await screen.findByLabelText("Evidence hash")).toHaveTextContent("sha256 ab12cd34ef56");
  expect(fetchSpy.mock.calls.filter(([input]) => String(input) === evidenceUrl)).toHaveLength(1);
  expect(createObjectURL).toHaveBeenCalledTimes(1);
  expect(click).toHaveBeenCalledTimes(1);
  expect(revokeObjectURL).toHaveBeenCalledWith("blob:evidence");
  click.mockRestore();
});

it("builds portfolio URLs and formats wait ages", () => {
  expect(portfolioUrl({ status: "", phase: " ", owner: "", waitingHours: "0" })).toBe(LIST_URL);
  expect(formatAge(undefined)).toBeUndefined();
  expect(formatAge(90)).toBe("2m");
  expect(formatAge(3 * 86400)).toBe("3d");
});
