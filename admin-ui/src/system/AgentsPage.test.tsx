import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it } from "vitest";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { AgentsPage } from "./AgentsPage";

function agentsPayload() {
  return {
    status: "empty",
    definition: { definition_type: "agents", definition_version: "1.0.0", agents: {} },
    draft: {
      id: "draft-1",
      revision: "rev-1",
      definition: { definition_type: "agents", definition_version: "1.0.0", agents: {} },
    },
    active_revision: null,
    roster: [
      {
        id: "orchestrator",
        source: "inherited",
        default: true,
        model: "openai/gpt-4o",
        skills: ["ops"],
        toolsets: ["core-read"],
        tools: { allow: [] as string[], deny: [] as string[] },
        connector_bindings: {} as Record<string, string>,
        soul: "",
      },
      {
        id: "legal-agent",
        source: "overlay",
        default: false,
        model: "openai/gpt-4o",
        skills: ["ops"],
        toolsets: ["core-read"],
        tools: { allow: [] as string[], deny: [] as string[] },
        connector_bindings: {} as Record<string, string>,
        soul: "",
      },
    ],
    toolsets: ["core-read", "core-write"],
    tool_catalog: {} as Record<string, string[]>,
    discovered_tools: {} as Record<string, string[]>,
    google_accounts: [] as string[],
    yaml_agent_ids: ["orchestrator"],
  };
}

beforeEach(() => {
  window.location.hash = "#/system/agents";
});

it("stacks an overlay agent's fields and shows its soul", async () => {
  const payload = agentsPayload();
  payload.draft.definition.agents = {
    "legal-agent": {
      display_name: "Legal",
      soul: "Cautious counsel.",
      model: "openai/gpt-4o",
      skills: ["ops"],
      toolsets: ["core-read"],
    },
  };
  mockFetch((url) => {
    if (url === "/admin/agents") return jsonResponse(payload);
    return jsonResponse({});
  });
  renderPage(<AgentsPage selectedId="legal-agent" />);
  const soul = await screen.findByLabelText("Soul");
  expect(soul).toHaveValue("Cautious counsel.");
  expect(screen.getByLabelText("Agent id").closest("label")).toHaveClass("form-field");
  expect(screen.getByLabelText("Display name").closest("label")).toHaveClass("form-field");
  expect(soul.closest("label")).toHaveClass("form-field");
});

it("shows inherited vs overlay badges from GET /admin/agents", async () => {
  mockFetch((url) => {
    if (url === "/admin/agents") return jsonResponse(agentsPayload());
    return jsonResponse({});
  });
  renderPage(<AgentsPage />);
  expect(await screen.findByText("orchestrator")).toBeInTheDocument();
  expect(screen.getByText("Inherited")).toBeInTheDocument();
  expect(screen.getByText("Overlay")).toBeInTheDocument();
});

it("pages the roster ten agents at a time", async () => {
  const payload = agentsPayload();
  payload.roster = Array.from({ length: 12 }, (_, index) => ({
    id: `agent-${String(index + 1).padStart(2, "0")}`,
    source: index % 2 ? "overlay" : "inherited",
    default: index === 0,
    model: "openai/gpt-4o",
    skills: ["ops"],
    toolsets: ["core-read"],
    tools: { allow: [] as string[], deny: [] as string[] },
    connector_bindings: {} as Record<string, string>,
    soul: "",
  }));
  mockFetch((url) => {
    if (url === "/admin/agents") return jsonResponse(payload);
    return jsonResponse({});
  });
  renderPage(<AgentsPage />);
  expect(await screen.findByRole("link", { name: "agent-01" })).toBeInTheDocument();
  expect(screen.getByText("Showing 1–10 of 12 agents")).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "agent-11" })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Next" }));
  expect(await screen.findByRole("link", { name: "agent-11" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "agent-12" })).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "agent-01" })).not.toBeInTheDocument();
});

it("saves a new agent draft and rewrites the hash to the id", async () => {
  mockFetch((url, init) => {
    if (url === "/admin/agents") return jsonResponse(agentsPayload());
    if (url === "/admin/agents/drafts/draft-1" && (init?.method || "GET") === "PUT") {
      return jsonResponse({
        draft: {
          id: "draft-1",
          revision: "rev-2",
          definition: {
            definition_type: "agents",
            definition_version: "1.0.0",
            agents: {
              "legal-agent": {
                workspace: "/workspace/legal-agent",
                model: "openai/gpt-4o",
                display_name: "Legal",
                soul: "Cautious counsel.",
                skills: ["ops"],
                toolsets: ["core-read"],
                default: false,
                delegatable: true,
              },
            },
          },
        },
      });
    }
    return jsonResponse({});
  });
  renderPage(<AgentsPage selectedId="new" />);
  await userEvent.clear(await screen.findByLabelText("Agent id"));
  await userEvent.type(screen.getByLabelText("Agent id"), "legal-agent");
  await userEvent.click(screen.getByRole("button", { name: "Save draft" }));
  expect(window.location.hash).toBe("#/system/agents/legal-agent");
});

it("prefills an inherited agent from the roster and resets the overlay object", async () => {
  let putBody: { definition?: { agents?: Record<string, unknown> } } = {};
  mockFetch((url, init) => {
    if (url === "/admin/agents") return jsonResponse(agentsPayload());
    if (url === "/admin/agents/drafts/draft-1" && (init?.method || "GET") === "PUT") {
      putBody = JSON.parse(String(init?.body || "{}"));
      return jsonResponse({ draft: agentsPayload().draft });
    }
    return jsonResponse({});
  });
  renderPage(<AgentsPage selectedId="orchestrator" />);
  expect(await screen.findByDisplayValue("openai/gpt-4o")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Reset" }));
  expect(putBody.definition?.agents).toEqual({});
});

it("activates the draft and shows routing and MCP links", async () => {
  let activateBody: { draft_id?: string; expected_revision?: string } = {};
  mockFetch((url, init) => {
    if (url === "/admin/agents") return jsonResponse(agentsPayload());
    if (url === "/admin/agents/activate" && (init?.method || "GET") === "POST") {
      activateBody = JSON.parse(String(init?.body || "{}"));
      return jsonResponse({ reload_ok: true, activated: {}, issues: [] });
    }
    return jsonResponse({});
  });
  renderPage(<AgentsPage selectedId="legal-agent" />);
  await userEvent.click(await screen.findByRole("button", { name: "Activate" }));
  expect(activateBody).toEqual({ draft_id: "draft-1", expected_revision: "rev-1" });
  expect(await screen.findByRole("link", { name: "Add a routing intent" })).toHaveAttribute(
    "href",
    "#/system/settings/routing"
  );
  expect(screen.getByRole("link", { name: "MCP" })).toHaveAttribute("href", "#/system/mcp");
});

it("keeps a seeded deny list and denies a toolset tool when unchecked", async () => {
  const payload = agentsPayload();
  payload.roster.push({
    id: "marketing-agent",
    source: "inherited",
    default: false,
    model: "openai/gpt-4o",
    skills: ["memory-api"],
    toolsets: ["google-workspace-docs"],
    tools: { allow: ["read_file"], deny: ["google_workspace_gmail_send"] },
    connector_bindings: { "google-workspace": "marketing" },
    soul: "Catalog voice.",
  });
  payload.tool_catalog = {
    "google-workspace-docs": ["google_workspace_docs_append"],
  };
  payload.google_accounts = ["marketing", "sales"];
  let putBody: {
    definition?: {
      agents?: Record<string, { tools?: { deny?: string[] }; connector_bindings?: Record<string, string> }>;
    };
  } = {};
  mockFetch((url, init) => {
    if (url === "/admin/agents") return jsonResponse(payload);
    if (url === "/admin/agents/drafts/draft-1" && (init?.method || "GET") === "PUT") {
      putBody = JSON.parse(String(init?.body || "{}"));
      return jsonResponse({ draft: payload.draft });
    }
    return jsonResponse({});
  });
  renderPage(<AgentsPage selectedId="marketing-agent" />);
  expect(await screen.findByLabelText("google_workspace_gmail_send")).not.toBeChecked();
  expect(screen.getByLabelText("google_workspace_docs_append")).toBeChecked();
  expect(screen.getByLabelText("Google account")).toHaveValue("marketing");
  await userEvent.click(screen.getByLabelText("google_workspace_docs_append"));
  expect(screen.getByLabelText("google_workspace_docs_append")).not.toBeChecked();
  await userEvent.click(screen.getByRole("button", { name: "Save draft" }));
  const saved = putBody.definition?.agents?.["marketing-agent"];
  expect(saved?.tools?.deny).toEqual(
    expect.arrayContaining(["google_workspace_gmail_send", "google_workspace_docs_append"])
  );
  expect(saved?.connector_bindings?.["google-workspace"]).toBe("marketing");
});
