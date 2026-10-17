import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { ProgramBuilderAgentPage, type BuilderSessionState } from "./ProgramBuilderAgentPage";

afterEach(() => {
  vi.restoreAllMocks();
});

function readyState(overrides: Partial<BuilderSessionState> = {}): BuilderSessionState {
  return {
    session_id: "session-1",
    workflow: {
      status: "draft",
      draft_id: "wf-draft",
      revision: "rev-wf",
      issues: [],
      valid: true,
      workflow_type: "team_digest",
    },
    program: {
      status: "draft",
      draft_id: "prog-draft",
      revision: "rev-prog",
      issues: [],
      valid: true,
      program_type: "team_digest",
    },
    routing: {
      status: "draft",
      draft_id: "route-draft",
      revision: "rev-route",
      issues: [],
      valid: true,
      intent_id: "team_digest",
      signals: ["send the team digest"],
    },
    skills: [],
    problems: [],
    ready: true,
    ...overrides,
  };
}

it("sends the intake with the first message and opens the new session", async () => {
  const onNavigate = vi.fn();
  let sent: Record<string, unknown> | null = null;
  mockFetch((url, init) => {
    if (url === "/admin/program-builder/sessions") return jsonResponse({ sessions: [] });
    if (url === "/admin/program-builder/message") {
      sent = JSON.parse(String(init?.body));
      return jsonResponse({ status: "succeeded", session_id: "session-1", output: { summary: "Drafting now." } });
    }
    if (url.includes("/messages")) return jsonResponse({ messages: [] });
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<ProgramBuilderAgentPage onNavigate={onNavigate} />);
  await userEvent.type(screen.getByLabelText("Program name"), "Team digest");
  await userEvent.type(screen.getByLabelText("Channels"), "#team, #leads");
  await userEvent.type(screen.getByLabelText("Message"), "Build it");
  await userEvent.click(screen.getByRole("button", { name: "Send" }));

  expect(await screen.findByText("Drafting now.")).toBeInTheDocument();
  expect(sent).toMatchObject({
    text: "Build it",
    intake: { name: "Team digest", channels: ["#team", "#leads"] },
  });
  expect(sent).not.toHaveProperty("channel");
  expect(onNavigate).toHaveBeenCalledWith({ kind: "build", sessionId: "session-1" });
});

it("hides the intake and omits it once a session exists", async () => {
  let sent: Record<string, unknown> | null = null;
  mockFetch((url, init) => {
    if (url === "/admin/program-builder/sessions") return jsonResponse({ sessions: [] });
    if (url === "/admin/program-builder/sessions/session-1") return jsonResponse(readyState());
    if (url === "/admin/program-builder/message") {
      sent = JSON.parse(String(init?.body));
      return jsonResponse({ status: "succeeded", session_id: "session-1", output: { summary: "Updated." } });
    }
    if (url.includes("/messages")) return jsonResponse({ messages: [] });
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<ProgramBuilderAgentPage sessionId="session-1" onNavigate={() => undefined} />);
  expect(screen.queryByLabelText("Program name")).not.toBeInTheDocument();
  await userEvent.type(screen.getByLabelText("Message"), "Rename it");
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(sent).toEqual({ text: "Rename it", session_id: "session-1" }));
});

it("renders draft cards with issues, links, and discarded drafts", async () => {
  mockFetch((url) => {
    if (url === "/admin/program-builder/sessions") return jsonResponse({ sessions: [] });
    if (url === "/admin/program-builder/sessions/session-1") {
      return jsonResponse(
        readyState({
          workflow: {
            status: "draft",
            draft_id: "wf-draft",
            revision: "rev-wf",
            valid: false,
            issues: [
              {
                severity: "error",
                code: "ungated_publish_stage",
                path: "stages[2].tool_allow",
                message: "stage send can publish without an approval gate",
              },
            ],
          },
          program: { status: "missing", draft_id: "prog-draft", revision: null, issues: [], valid: true },
          skills: [{ skill_id: "digest-writer", status: "draft" }],
          problems: ["program draft is missing", "workflow draft has errors", "skill digest-writer is draft"],
          ready: false,
        })
      );
    }
    if (url.includes("/messages")) return jsonResponse({ messages: [] });
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<ProgramBuilderAgentPage sessionId="session-1" onNavigate={() => undefined} />);

  const workflow = await screen.findByRole("region", { name: "Workflow draft" });
  expect(within(workflow).getByText(/1 error, 0 warnings/)).toBeInTheDocument();
  expect(within(workflow).getByText(/without an approval gate/)).toBeInTheDocument();
  expect(within(workflow).getByRole("link", { name: "Open in builder" })).toHaveAttribute(
    "href",
    "#/workflows/drafts/wf-draft"
  );
  const program = screen.getByRole("region", { name: "Program draft" });
  expect(within(program).getByText("Discarded. Ask the agent to recreate it.")).toBeInTheDocument();
  const routing = screen.getByRole("region", { name: "Routing intent draft" });
  expect(within(routing).getByRole("link", { name: "Open in builder" })).toHaveAttribute(
    "href",
    "#/system/settings/routing"
  );
  const skills = screen.getByRole("region", { name: "Proposed skills" });
  expect(within(skills).getByText("digest-writer")).toBeInTheDocument();
  expect(within(skills).getByRole("link", { name: "Review in Inbox" })).toHaveAttribute(
    "href",
    "#/inbox/skill_draft"
  );
  expect(screen.getByRole("button", { name: "Activate all" })).toBeDisabled();
});

it("activates with the displayed revisions and offers retry after a partial failure", async () => {
  const activations: Array<Record<string, unknown>> = [];
  mockFetch((url, init) => {
    if (url === "/admin/program-builder/sessions") return jsonResponse({ sessions: [] });
    if (url === "/admin/program-builder/sessions/session-1") return jsonResponse(readyState());
    if (url === "/admin/program-builder/sessions/session-1/activate") {
      activations.push(JSON.parse(String(init?.body)));
      if (activations.length === 1) {
        return jsonResponse({
          ok: false,
          steps: [
            { kind: "workflow", status: "activated" },
            { kind: "program", status: "failed", error: "draft changed since the page last loaded" },
            { kind: "routing", status: "not_run" },
          ],
          state: readyState({
            workflow: { status: "activated", draft_id: null, revision: null, issues: [], valid: true },
          }),
        });
      }
      return jsonResponse({
        ok: true,
        steps: [
          { kind: "workflow", status: "already_active" },
          { kind: "program", status: "activated" },
          { kind: "routing", status: "activated" },
        ],
        state: readyState({
          workflow: { status: "activated", issues: [], valid: true },
          program: { status: "activated", issues: [], valid: true, program_type: "team_digest" },
          routing: { status: "activated", issues: [], valid: true, signals: ["send the team digest"] },
        }),
      });
    }
    if (url.includes("/messages")) return jsonResponse({ messages: [] });
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<ProgramBuilderAgentPage sessionId="session-1" onNavigate={() => undefined} />);
  const activate = await screen.findByRole("button", { name: "Activate all" });
  await waitFor(() => expect(activate).toBeEnabled());
  await userEvent.click(activate);

  const steps = await screen.findByRole("list", { name: "Activation steps" });
  expect(within(steps).getByText(/draft changed since the page last loaded/)).toBeInTheDocument();
  expect(activations[0]).toEqual({
    expected_revisions: { workflow: "rev-wf", program: "rev-prog", routing: "rev-route" },
  });

  await userEvent.click(screen.getByRole("button", { name: "Retry remaining" }));
  expect(await screen.findByText("Program is live.", { exact: false })).toBeInTheDocument();
  expect(activations[1]).toEqual({
    expected_revisions: { program: "rev-prog", routing: "rev-route" },
  });
  expect(screen.getByRole("link", { name: "Open the program" })).toHaveAttribute(
    "href",
    "#/workflows/programs/team_digest"
  );
  expect(screen.getByText(/"send the team digest"/)).toBeInTheDocument();
});

it("lists earlier builds in the session picker", async () => {
  const onNavigate = vi.fn();
  mockFetch((url) => {
    if (url === "/admin/program-builder/sessions") {
      return jsonResponse({
        sessions: [{ session_id: "session-9abcdef", program_type: "team_digest" }],
      });
    }
    throw new Error(`Unexpected URL ${url}`);
  });
  renderPage(<ProgramBuilderAgentPage onNavigate={onNavigate} />);
  const picker = await screen.findByLabelText("Earlier builds");
  await screen.findByRole("option", { name: /team_digest/ });
  await userEvent.selectOptions(picker, "session-9abcdef");
  expect(onNavigate).toHaveBeenCalledWith({ kind: "build", sessionId: "session-9abcdef" });
});
