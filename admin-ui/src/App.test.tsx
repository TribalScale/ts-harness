import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import App, { parseHash } from "./App";
import { apiFetch } from "./api/client";
import { jsonResponse, mockFetch, renderPage } from "./test/render";

vi.mock("./inbox/InboxPage", () => ({ InboxPage: () => <div>Inbox page</div> }));
vi.mock("./chat/ChatPage", () => ({ ChatPage: () => <div>Chat page</div> }));
vi.mock("./programs/ProgramsPage", () => ({ ProgramsPage: () => <div>Programs page</div> }));
vi.mock("./system/SystemPage", async () => {
  const React = await import("react");
  return {
    SystemPage: ({
      onSettingsDirtyChange,
    }: {
      onSettingsDirtyChange?: (dirty: boolean) => void;
    }) => {
      React.useEffect(() => {
        onSettingsDirtyChange?.(true);
        return () => onSettingsDirtyChange?.(false);
      }, [onSettingsDirtyChange]);
      return <div>System page</div>;
    },
  };
});
vi.mock("./program-definitions/ProgramDefinitionStudio", async () => {
  const React = await import("react");
  const { Button } = await import("./components/Button");
  const typeLabel = (route: { kind: string; draftId?: string; workflowType?: string }) => {
    if (route.kind === "draft") return `draft:${route.draftId}`;
    if (route.kind === "workflow") return `workflow:${route.workflowType}`;
    return route.kind;
  };
  return {
    ProgramDefinitionStudio: ({
      tab,
      workflowRoute,
      programRoute,
      onDirtyChange,
      onNavigateWorkflow,
      allowDirtyNavigation,
    }: {
      tab: string;
      workflowRoute: { kind: string; draftId?: string; workflowType?: string };
      programRoute: { kind: string; draftId?: string; programType?: string };
      onDirtyChange?: (dirty: boolean) => void;
      onNavigateWorkflow: (route: { kind: string; draftId?: string; workflowType?: string }) => void;
      allowDirtyNavigation?: () => void;
    }) => {
      React.useEffect(() => {
        if (tab === "workflows") onDirtyChange?.(true);
        return () => onDirtyChange?.(false);
      }, [tab, onDirtyChange]);
      const programLabel =
        programRoute.kind === "program" && programRoute.programType
          ? `program:${programRoute.programType}`
          : `program:${programRoute.kind}`;
      return (
        <div>
          {`Studio tab:${tab} ${programLabel} workflow:${typeLabel(workflowRoute)}`}
          <Button onClick={() => onNavigateWorkflow({ kind: "draft", draftId: "draft-2" })}>
            Open draft two
          </Button>
          <Button
            onClick={() => {
              allowDirtyNavigation?.();
              onNavigateWorkflow({ kind: "draft", draftId: "created-1" });
            }}
          >
            Save-created draft
          </Button>
        </div>
      );
    },
  };
});

async function flushStudioDirtyFlag() {
  await act(async () => {
    await Promise.resolve();
  });
}

beforeEach(() => {
  vi.restoreAllMocks();
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: query.includes("min-width: 1024px"),
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
  window.location.hash = "#/workflows/new";
});

it("parses program definition and workflow studio routes", () => {
  expect(parseHash("#/workflows/programs")).toEqual(
    expect.objectContaining({
      nav: "workflows",
      studioTab: "programs",
      programRoute: { kind: "library" },
    })
  );
  expect(parseHash("#/workflows/programs/software_delivery")).toEqual(
    expect.objectContaining({
      studioTab: "programs",
      programRoute: { kind: "program", programType: "software_delivery" },
    })
  );
  expect(parseHash("#/workflows/programs/drafts/draft-1")).toEqual(
    expect.objectContaining({
      programRoute: { kind: "draft", draftId: "draft-1" },
    })
  );
  expect(parseHash("#/workflows/programs/build")).toEqual(
    expect.objectContaining({
      studioTab: "programs",
      programRoute: { kind: "build", sessionId: undefined },
    })
  );
  expect(parseHash("#/workflows/programs/build/session-1")).toEqual(
    expect.objectContaining({
      studioTab: "programs",
      programRoute: { kind: "build", sessionId: "session-1" },
    })
  );
  expect(parseHash("#/workflows/new")).toEqual(
    expect.objectContaining({
      studioTab: "workflows",
      workflowRoute: { kind: "new" },
    })
  );
  expect(parseHash("#/programs/abc")).toEqual(
    expect.objectContaining({
      nav: "programs",
      programId: "abc",
    })
  );
  expect(parseHash("#/chat?prefill=%2Flearn%20")).toEqual({
    nav: "chat",
    sessionId: undefined,
    prefill: "/learn ",
  });
  expect(parseHash("#/system/settings")).toEqual(
    expect.objectContaining({
      nav: "system",
      systemSection: "settings",
      settingsSection: "defaults",
    })
  );
  expect(parseHash("#/system/settings/identity")).toEqual(
    expect.objectContaining({ settingsSection: "identity" })
  );
  expect(parseHash("#/system/settings/nope")).toEqual(
    expect.objectContaining({ settingsSection: "defaults" })
  );
  expect(parseHash("#/system/mcp")).toEqual(
    expect.objectContaining({
      nav: "system",
      systemSection: "mcp",
    })
  );
  expect(parseHash("#/system/mcp/figma")).toEqual(
    expect.objectContaining({ systemSection: "mcp", mcpName: "figma" })
  );
  expect(parseHash("#/system/agents")).toEqual(
    expect.objectContaining({ nav: "system", systemSection: "agents" })
  );
  expect(parseHash("#/system/agents/legal-agent")).toEqual(
    expect.objectContaining({ systemSection: "agents", agentId: "legal-agent" })
  );
  expect(parseHash("#/workflows/prd_request?attachSkill=hello-skill&stage=intake")).toEqual(
    expect.objectContaining({
      nav: "workflows",
      workflowRoute: { kind: "workflow", workflowType: "prd_request" },
      attachSkill: "hello-skill",
      attachStage: "intake",
    })
  );
  expect(parseHash("#/system/cron?attachSkill=hello-skill&job=nightly")).toEqual(
    expect.objectContaining({
      systemSection: "cron",
      attachSkill: "hello-skill",
      attachJob: "nightly",
    })
  );
  expect(parseHash("#/system/cron?attachProgram=account_digest")).toEqual(
    expect.objectContaining({ systemSection: "cron", attachProgram: "account_digest" })
  );
});

it("moves a leftover localStorage token into the session cookie", async () => {
  window.location.hash = "#/inbox";
  const bodies: string[] = [];
  localStorage.setItem("ec_admin_token", "legacy-token");
  mockFetch((url, init) => {
    if (String(url).includes("/admin/auth/session")) {
      bodies.push(String(init?.body || ""));
      return jsonResponse({ authenticated: true, roles: [], permissions: ["view"] });
    }
    if (String(url).includes("/admin/auth/config")) {
      return jsonResponse({ mode: "static", authenticated: true, permissions: ["view"] });
    }
    return jsonResponse({});
  });
  render(<App />);
  expect(await screen.findByText("Inbox page")).toBeInTheDocument();
  expect(localStorage.getItem("ec_admin_token")).toBeNull();
  expect(bodies[0]).toContain("legacy-token");
});

it("returns to the login page when an API call is unauthorized", async () => {
  window.location.hash = "#/inbox";
  mockFetch((url) => {
    if (String(url).includes("/admin/auth/config")) {
      return jsonResponse({ mode: "static", authenticated: true, permissions: ["view"] });
    }
    return jsonResponse({ error: "unauthorized" }, 401);
  });
  render(<App />);
  expect(await screen.findByText("Inbox page")).toBeInTheDocument();
  await expect(apiFetch("/admin/inbox")).rejects.toThrow(/unauthorized/i);
  expect(await screen.findByRole("heading", { name: "TS Harness" })).toBeInTheDocument();
});

it("opens Program definitions by default from Workflows navigation", async () => {
  window.location.hash = "#/inbox";
  renderPage(<App />);
  await userEvent.click(await screen.findByRole("button", { name: "Workflows" }));
  await waitFor(() => expect(window.location.hash).toBe("#/workflows/programs"));
  expect(await screen.findByText(/Studio tab:programs/)).toBeInTheDocument();
});

it("keeps top-level navigation on the workflow route when dirty changes are not discarded", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);

  renderPage(<App />);

  expect(await screen.findByText(/Studio tab:workflows/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Chat" }));

  expect(confirm).toHaveBeenCalledWith("Discard unsaved workflow changes?");
  expect(window.location.hash).toBe("#/workflows/new");
  await waitFor(() => expect(screen.getByText(/Studio tab:workflows/)).toBeInTheDocument());
});

it("uses hashchange as the single dirty gate for AppShell navigation", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);

  renderPage(<App />);

  expect(await screen.findByText(/Studio tab:workflows program:library workflow:new/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Chat" }));

  await waitFor(() => expect(screen.getByText("Chat page")).toBeInTheDocument());
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(window.location.hash).toBe("#/chat");
});

it("restores the previous workflow hash when dirty hash navigation is cancelled", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);

  renderPage(<App />);

  expect(await screen.findByText(/Studio tab:workflows/)).toBeInTheDocument();
  await flushStudioDirtyFlag();
  act(() => {
    window.location.hash = "#/chat";
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  });

  expect(confirm).toHaveBeenCalledWith("Discard unsaved workflow changes?");
  await waitFor(() => expect(window.location.hash).toBe("#/workflows/new"));
  expect(screen.getByText(/Studio tab:workflows/)).toBeInTheDocument();
  expect(screen.queryByText("Chat page")).not.toBeInTheDocument();
});

it("restores the previous workflow hash when dirty workflow-to-workflow navigation is cancelled", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);

  renderPage(<App />);

  expect(await screen.findByText(/Studio tab:workflows program:library workflow:new/)).toBeInTheDocument();
  await flushStudioDirtyFlag();
  act(() => {
    window.location.hash = "#/workflows/drafts/draft-2";
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  });

  expect(confirm).toHaveBeenCalledWith("Discard unsaved workflow changes?");
  expect(confirm).toHaveBeenCalledTimes(1);
  await waitFor(() => expect(window.location.hash).toBe("#/workflows/new"));
  expect(screen.getByText(/Studio tab:workflows program:library workflow:new/)).toBeInTheDocument();
});

it("applies dirty hash navigation when discard is confirmed", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);

  renderPage(<App />);

  expect(await screen.findByText(/Studio tab:workflows/)).toBeInTheDocument();
  await flushStudioDirtyFlag();
  act(() => {
    window.location.hash = "#/chat";
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  });

  expect(confirm).toHaveBeenCalledWith("Discard unsaved workflow changes?");
  await waitFor(() => expect(screen.getByText("Chat page")).toBeInTheDocument());
  expect(window.location.hash).toBe("#/chat");
});

it("confirms before leaving dirty Settings and stays when discarded", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  window.location.hash = "#/system/settings";

  renderPage(<App />);

  expect(await screen.findByText("System page")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Inbox" }));

  expect(confirm).toHaveBeenCalledWith("Discard unsaved settings changes?");
  await waitFor(() => expect(window.location.hash).toBe("#/system/settings"));
  expect(screen.getByText("System page")).toBeInTheDocument();
  expect(screen.queryByText("Inbox page")).not.toBeInTheDocument();
});

it("does not confirm when switching Settings inner tabs while dirty", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  window.location.hash = "#/system/settings";

  renderPage(<App />);

  expect(await screen.findByText("System page")).toBeInTheDocument();
  act(() => {
    window.location.hash = "#/system/settings/identity";
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  });

  expect(confirm).not.toHaveBeenCalled();
  await waitFor(() => expect(screen.getByText("System page")).toBeInTheDocument());
});

it("opens the inbox after static sign-in from the login hash", async () => {
  window.location.hash = "#/login";
  const sessionPosts: string[] = [];
  let signedIn = false;
  mockFetch((url, init) => {
    if (String(url).includes("/admin/auth/session")) {
      signedIn = true;
      sessionPosts.push(String(init?.body || ""));
      return jsonResponse({ authenticated: true, roles: [], permissions: ["view"] });
    }
    if (String(url).includes("/admin/auth/config")) {
      return jsonResponse({
        mode: "static",
        authenticated: signedIn,
        permissions: signedIn ? ["view"] : [],
      });
    }
    return jsonResponse({});
  });

  render(<App />);

  expect(await screen.findByRole("heading", { name: "TS Harness" })).toBeInTheDocument();
  await userEvent.type(await screen.findByPlaceholderText("GATEWAY_TOKEN"), "dev-token");
  await userEvent.click(screen.getByRole("button", { name: "Enter console" }));

  await waitFor(() => expect(window.location.hash).toBe("#/inbox"));
  expect(await screen.findByText("Inbox page")).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "TS Harness" })).not.toBeInTheDocument();
  expect(sessionPosts[0]).toContain("dev-token");
  expect(localStorage.getItem("ec_admin_token")).toBeNull();
});

it("skips the dirty confirm when allowDirtyNavigation is set before route change", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);

  renderPage(<App />);

  expect(await screen.findByText(/Studio tab:workflows program:library workflow:new/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Save-created draft" }));

  expect(confirm).not.toHaveBeenCalled();
  await waitFor(() =>
    expect(screen.getByText(/Studio tab:workflows program:library workflow:draft:created-1/)).toBeInTheDocument()
  );
  expect(window.location.hash).toBe("#/workflows/drafts/created-1");
});
