import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { ChatPage } from "./ChatPage";

function installMobileMediaQuery() {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
}

const THREADS = [
  { session_id: "session-alpha", agent_id: "alpha-agent" },
  { session_id: "session-beta", agent_id: "beta-agent" },
];

function StatefulChat({ initialSessionId = "" }: { initialSessionId?: string }) {
  const [sessionId, setSessionId] = useState(initialSessionId);
  return <ChatPage sessionId={sessionId} onSelectSession={setSessionId} />;
}

beforeEach(() => {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: true,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
});

it("sends text and renders output.summary", async () => {
  let transcriptReloaded = false;
  const fetchMock = mockFetch((url, init) => {
    if (url.includes("/messages")) {
      transcriptReloaded = true;
      return jsonResponse({
        messages: [
          { role: "user", content: "Show blocked work" },
          { role: "assistant", content: "Two tasks are blocked." },
        ],
      });
    }
    if (url.startsWith("/admin/sessions")) return jsonResponse({ sessions: [] });
    if (url === "/session/message") {
      expect(JSON.parse(String(init?.body))).toMatchObject({
        text: "Show blocked work",
        channel: "web",
        source: "web",
      });
      return jsonResponse({
        status: "ok",
        session_id: "session-1",
        output: { summary: "Two tasks are blocked." },
      });
    }
    throw new Error(`Unexpected URL ${url}`);
  });
  renderPage(<ChatPage onSelectSession={() => undefined} />);
  await userEvent.type(screen.getByLabelText("Message"), "Show blocked work");
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
  expect(await screen.findByText("Two tasks are blocked.")).toBeInTheDocument();
  await waitFor(() => expect(transcriptReloaded).toBe(true));
  fetchMock.mockRestore();
});

it("shows an intentional start state", async () => {
  mockFetch(() => jsonResponse({ sessions: [] }));
  renderPage(<ChatPage onSelectSession={() => undefined} />);
  expect(await screen.findByText("Start a conversation")).toBeInTheDocument();
});

it("applies prefill from the Learn a skill button", async () => {
  mockFetch((url) => {
    if (url.startsWith("/admin/sessions")) return jsonResponse({ sessions: [] });
    throw new Error(`Unexpected URL ${url}`);
  });
  renderPage(<ChatPage prefill="/learn " onSelectSession={() => undefined} />);
  expect(screen.getByLabelText("Message")).toHaveValue("/learn ");
});

it("omits messages without displayable content", async () => {
  mockFetch((url) =>
    url.includes("/messages")
      ? jsonResponse({ messages: [{ role: "assistant", content: "" }] })
      : jsonResponse({ sessions: [] })
  );
  renderPage(<ChatPage sessionId="session-1" onSelectSession={() => undefined} />);
  await screen.findByLabelText("Message");
  expect(screen.queryByTestId("empty-message")).not.toBeInTheDocument();
});

it("restores the draft after send fails", async () => {
  mockFetch((url) =>
    url === "/session/message"
      ? jsonResponse({ error: "provider unavailable" }, 500)
      : jsonResponse({ sessions: [] })
  );
  renderPage(<ChatPage onSelectSession={() => undefined} />);
  await userEvent.type(screen.getByLabelText("Message"), "retry me");
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
  expect(await screen.findByDisplayValue("retry me")).toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent("provider unavailable");
});

it("keeps optimistic messages when the reloaded transcript is empty", async () => {
  let transcriptReloaded = false;
  mockFetch((url) => {
    if (url.includes("/messages")) {
      transcriptReloaded = true;
      return jsonResponse({ messages: [] });
    }
    if (url.startsWith("/admin/sessions")) return jsonResponse({ sessions: [] });
    if (url === "/session/message") {
      return jsonResponse({
        status: "ok",
        session_id: "session-1",
        output: { summary: "Two tasks are blocked." },
      });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  const { container } = renderPage(<ChatPage onSelectSession={() => undefined} />);
  await userEvent.type(screen.getByLabelText("Message"), "Check the release gate");
  await userEvent.click(screen.getByRole("button", { name: "Send" }));

  expect(await screen.findByText("Two tasks are blocked.")).toBeInTheDocument();
  await waitFor(() => expect(transcriptReloaded).toBe(true));
  await waitFor(() => expect(container.querySelectorAll(".bubble")).toHaveLength(2));
  expect(container.querySelector(".bubble.user")).toHaveTextContent("Check the release gate");
});

it("replaces the failed bubble instead of duplicating it when resending", async () => {
  let failSend = true;
  mockFetch((url) => {
    if (url === "/session/message") {
      return failSend
        ? jsonResponse({ error: "provider unavailable" }, 500)
        : jsonResponse({
            status: "ok",
            session_id: "session-1",
            output: { summary: "Recovered." },
          });
    }
    if (url.includes("/messages")) return jsonResponse({ messages: [] });
    return jsonResponse({ sessions: [] });
  });

  const { container } = renderPage(<ChatPage onSelectSession={() => undefined} />);
  await userEvent.type(screen.getByLabelText("Message"), "retry me");
  await userEvent.click(screen.getByRole("button", { name: "Send" }));

  expect(await screen.findByDisplayValue("retry me")).toBeInTheDocument();
  expect(container.querySelectorAll(".bubble.failed")).toHaveLength(1);

  failSend = false;
  await userEvent.click(screen.getByRole("button", { name: "Send" }));

  expect(await screen.findByText("Recovered.")).toBeInTheDocument();
  await waitFor(() => expect(container.querySelectorAll(".bubble.failed")).toHaveLength(0));
  expect(container.querySelectorAll(".bubble.user")).toHaveLength(1);
  expect(screen.queryByLabelText("Failed message")).not.toBeInTheDocument();
});

it("hides tool payloads and system events from the thread", async () => {
  mockFetch((url) =>
    url.includes("/messages")
      ? jsonResponse({
          messages: [
            { role: "assistant", content: "Here is the delivery status." },
            {
              role: "tool",
              name: "program_status",
              content: '{"resolved_config":{"big":true}}',
            },
            { role: "system", content: "session metadata" },
          ],
        })
      : jsonResponse({ sessions: [] })
  );

  const { container } = renderPage(
    <ChatPage sessionId="session-1" onSelectSession={() => undefined} />
  );

  expect(await screen.findByText("Here is the delivery status.")).toBeInTheDocument();
  expect(screen.queryByText(/resolved_config/)).not.toBeInTheDocument();
  expect(screen.queryByText("session metadata")).not.toBeInTheDocument();
  expect(container.querySelectorAll(".chat-event")).toHaveLength(0);
  expect(container.querySelectorAll(".bubble.assistant")).toHaveLength(1);
});

it("rejects a whitespace-only send", async () => {
  const fetchMock = mockFetch(() => jsonResponse({ sessions: [] }));
  renderPage(<ChatPage onSelectSession={() => undefined} />);
  const box = await screen.findByLabelText("Message");
  await userEvent.type(box, "   ");
  await userEvent.keyboard("{Enter}");
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Enter a message before sending."
  );
  expect(box).toHaveValue("   ");
  expect(
    fetchMock.mock.calls.some((call) => String(call[0]).includes("/session/message"))
  ).toBe(false);
});

it("keeps the start state outside the transcript live region", async () => {
  mockFetch(() => jsonResponse({ sessions: [] }));
  const { container } = renderPage(<ChatPage onSelectSession={() => undefined} />);

  const start = await screen.findByText("Start a conversation");
  expect(container.querySelector(".chat-thread")?.contains(start)).toBe(false);
});

it("clears the previous thread when switching to a session with an empty transcript", async () => {
  mockFetch((url) => {
    if (url.includes("/session-alpha/messages")) {
      return jsonResponse({
        messages: [
          { role: "user", content: "Older question" },
          { role: "assistant", content: "Older answer" },
        ],
      });
    }
    if (url.includes("/session-beta/messages")) return jsonResponse({ messages: [] });
    if (url.startsWith("/admin/sessions")) return jsonResponse({ sessions: THREADS });
    throw new Error(`Unexpected URL ${url}`);
  });

  const { container } = renderPage(<StatefulChat initialSessionId="session-alpha" />);

  expect(await screen.findByText("Older answer")).toBeInTheDocument();

  await userEvent.click(await screen.findByRole("button", { name: /beta-agent/ }));

  await waitFor(() => expect(container.querySelectorAll(".bubble")).toHaveLength(0));
  expect(screen.queryByText("Older answer")).not.toBeInTheDocument();
  expect(screen.queryByText("Older question")).not.toBeInTheDocument();
});

it("ignores a stale transcript response that resolves after the session changed", async () => {
  let resolveAlpha: (value: Response) => void = () => undefined;
  const alphaPending = new Promise<Response>((resolve) => {
    resolveAlpha = resolve;
  });
  mockFetch((url) => {
    if (url.includes("/session-alpha/messages")) return alphaPending;
    if (url.includes("/session-beta/messages")) {
      return jsonResponse({ messages: [{ role: "assistant", content: "Beta answer" }] });
    }
    if (url.startsWith("/admin/sessions")) return jsonResponse({ sessions: THREADS });
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<StatefulChat initialSessionId="session-alpha" />);

  await userEvent.click(await screen.findByRole("button", { name: /beta-agent/ }));
  expect(await screen.findByText("Beta answer")).toBeInTheDocument();

  await act(async () => {
    resolveAlpha(jsonResponse({ messages: [{ role: "assistant", content: "Alpha answer" }] }));
    await new Promise((resolve) => setTimeout(resolve, 0));
  });

  expect(screen.queryByText("Alpha answer")).not.toBeInTheDocument();
  expect(screen.getByText("Beta answer")).toBeInTheDocument();
});

it("keeps a typed draft when opening and closing the thread list", async () => {
  installMobileMediaQuery();
  mockFetch(() => jsonResponse({ sessions: [] }));
  renderPage(<ChatPage onSelectSession={() => undefined} />);

  await userEvent.type(screen.getByLabelText("Message"), "half written thought");
  await userEvent.click(screen.getByRole("button", { name: "History" }));
  expect(document.querySelector(".split-detail")?.hasAttribute("hidden")).toBe(true);
  expect(document.querySelector(".split-list")?.hasAttribute("hidden")).toBe(false);

  await userEvent.click(screen.getByRole("button", { name: "Back" }));
  expect(document.querySelector(".split-detail")?.hasAttribute("hidden")).toBe(false);
  expect(screen.getByLabelText("Message")).toHaveValue("half written thought");
});

it("keeps a sample prompt when opening and closing the thread list", async () => {
  installMobileMediaQuery();
  mockFetch(() => jsonResponse({ sessions: [] }));
  renderPage(<ChatPage onSelectSession={() => undefined} />);

  await userEvent.click(screen.getByRole("button", { name: "Show blocked work" }));
  expect(screen.getByLabelText("Message")).toHaveValue("Show blocked work");

  await userEvent.click(screen.getByRole("button", { name: "History" }));
  await userEvent.click(screen.getByRole("button", { name: "Back" }));
  expect(screen.getByLabelText("Message")).toHaveValue("Show blocked work");
});

it("opens the conversation on a narrow screen", () => {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
  mockFetch(() => jsonResponse({ sessions: [] }));
  renderPage(<ChatPage onSelectSession={() => undefined} />);
  expect(screen.getByLabelText("Message")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "History" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Back" })).not.toBeInTheDocument();
});

it("lists search hits and opens the existing thread", async () => {
  const onSelect = vi.fn();
  mockFetch((url) => {
    if (url.startsWith("/admin/sessions/search")) {
      return jsonResponse({
        hits: [
          { session_id: "session-alpha", snippet: "checkout PRD", role: "user", seq: 1 },
        ],
      });
    }
    if (url.includes("/messages")) return jsonResponse({ messages: [] });
    if (url.startsWith("/admin/sessions")) return jsonResponse({ sessions: THREADS });
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<ChatPage sessionId="" onSelectSession={onSelect} />);
  await userEvent.type(screen.getByLabelText("Search transcripts"), "checkout");
  await userEvent.click(screen.getByRole("button", { name: "Search" }));
  await userEvent.click(await screen.findByRole("button", { name: /checkout PRD/ }));

  expect(onSelect).toHaveBeenCalledWith("session-alpha");
});

it("shows a blocked slash-invoke summary without an assistant bubble", async () => {
  mockFetch((url) => {
    if (url === "/session/message") {
      return jsonResponse({
        status: "blocked",
        error: "skill_invoke_unresolved",
        output: { summary: "Cannot use /draft-prd: draft." },
      });
    }
    if (url.startsWith("/admin/skills")) return jsonResponse({ skills: [] });
    if (url.includes("/messages")) return jsonResponse({ messages: [] });
    if (url.startsWith("/admin/sessions")) return jsonResponse({ sessions: [] });
    throw new Error(`Unexpected URL ${url}`);
  });
  renderPage(<ChatPage onSelectSession={() => undefined} />);
  await userEvent.type(screen.getByLabelText("Message"), "/draft-prd now");
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Cannot use /draft-prd: draft.");
  expect(screen.queryByText("Cannot use /draft-prd: draft.", { selector: ".bubble.assistant" })).not.toBeInTheDocument();
  expect(screen.getByLabelText("Failed message")).toBeInTheDocument();
});

it("typeahead inserts a resolvable skill pin and hides other agents", async () => {
  mockFetch((url) => {
    if (url.startsWith("/admin/skills")) {
      return jsonResponse({
        skills: [
          { id: "jira-to-pr", agents: ["orchestrator"] },
          { id: "secret-ops", agents: ["ops-agent"] },
          { id: "google-workspace", agents: ["orchestrator"] },
          { id: "governed-delivery", agents: ["orchestrator"] },
        ],
      });
    }
    if (url.startsWith("/admin/sessions")) return jsonResponse({ sessions: [] });
    throw new Error(`Unexpected URL ${url}`);
  });
  renderPage(<ChatPage onSelectSession={() => undefined} />);
  await userEvent.type(screen.getByLabelText("Message"), "/g");
  expect(await screen.findByRole("option", { name: "google-workspace" })).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "governed-delivery" })).not.toBeInTheDocument();
  await userEvent.clear(screen.getByLabelText("Message"));
  await userEvent.type(screen.getByLabelText("Message"), "/j");
  expect(await screen.findByRole("option", { name: "jira-to-pr" })).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "secret-ops" })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("option", { name: "jira-to-pr" }));
  expect(screen.getByLabelText("Message")).toHaveValue("/jira-to-pr");
});

it("hides typeahead once the caret leaves the leading slash token", async () => {
  mockFetch((url) => {
    if (url.startsWith("/admin/skills")) {
      return jsonResponse({
        skills: [{ id: "jira-to-pr", agents: ["orchestrator"] }],
      });
    }
    if (url.startsWith("/admin/sessions")) return jsonResponse({ sessions: [] });
    throw new Error(`Unexpected URL ${url}`);
  });
  renderPage(<ChatPage onSelectSession={() => undefined} />);
  await userEvent.type(screen.getByLabelText("Message"), "/j");
  expect(await screen.findByRole("option", { name: "jira-to-pr" })).toBeInTheDocument();
  await userEvent.type(screen.getByLabelText("Message"), " leftover");
  expect(screen.queryByRole("option", { name: "jira-to-pr" })).not.toBeInTheDocument();
  expect(screen.getByLabelText("Message")).toHaveValue("/j leftover");
});

it("shows the start state on a narrow screen", async () => {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
  mockFetch(() => jsonResponse({ sessions: [] }));
  renderPage(<ChatPage onSelectSession={() => undefined} />);

  expect(await screen.findByText("Start a conversation")).toBeInTheDocument();
  expect(screen.getByLabelText("Message")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "New chat" }));
  expect(screen.getByLabelText("Message")).toBeInTheDocument();
});
