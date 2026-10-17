import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { SystemPage } from "./SystemPage";
import { McpPage } from "./McpPage";

function mcpPayload() {
  return {
    status: "empty",
    definition: { definition_type: "mcp_settings", definition_version: "1.0.0", servers: {} },
    draft: {
      id: "draft-1",
      revision: "rev-1",
      definition: { definition_type: "mcp_settings", definition_version: "1.0.0", servers: {} },
    },
    merged: {
      atlassian: {
        transport: "stdio",
        command: "mcporter",
        enabled: true,
        env: { ATLASSIAN_TOKEN: "***" },
      },
    },
    sources: { atlassian: "inherited" },
    catalog: [
      {
        id: "atlassian",
        display_name: "Atlassian",
        transport: "stdio",
        command: "mcporter",
        args: [],
        env_names: [],
      },
    ],
  };
}

function installDialogMethods() {
  HTMLDialogElement.prototype.showModal = vi.fn(function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  });
  HTMLDialogElement.prototype.close = vi.fn(function close(this: HTMLDialogElement) {
    this.removeAttribute("open");
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

beforeEach(() => {
  installDialogMethods();
});

describe("System MCP tab", () => {
  it("selects the MCP tab for #/system/mcp", async () => {
    mockFetch((url) => {
      if (url === "/admin/mcp-settings") return jsonResponse(mcpPayload());
      if (url === "/mcp/tools") return jsonResponse({ tools: [] });
      return jsonResponse({});
    });
    renderPage(<SystemPage section="mcp" />);
    const tab = await screen.findByRole("tab", { name: "MCP" });
    expect(tab.getAttribute("aria-selected")).toBe("true");
    expect(await screen.findByRole("link", { name: "atlassian" })).toBeInTheDocument();
  });

  it("does not show raw env secret refs", async () => {
    mockFetch((url) => {
      if (url === "/admin/mcp-settings") return jsonResponse(mcpPayload());
      if (url === "/mcp/tools") return jsonResponse({ tools: [] });
      return jsonResponse({});
    });
    renderPage(<McpPage />);
    await screen.findByRole("link", { name: "atlassian" });
    expect(screen.queryByText("secret:ATLASSIAN_TOKEN")).not.toBeInTheDocument();
    expect(screen.queryByText("sk-live")).not.toBeInTheDocument();
    expect(screen.queryByText("***")).not.toBeInTheDocument();
  });

  it("keeps add forms closed until Add MCP and Cancel dismisses them", async () => {
    const user = userEvent.setup();
    mockFetch((url) => {
      if (url === "/admin/mcp-settings") return jsonResponse(mcpPayload());
      if (url === "/mcp/tools") return jsonResponse({ tools: [] });
      return jsonResponse({});
    });
    renderPage(<McpPage />);
    await screen.findByRole("link", { name: "atlassian" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("HTTP server name")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add MCP" }));
    expect(await screen.findByRole("dialog", { name: "Add MCP server" })).toBeInTheDocument();
    expect(screen.getByLabelText("HTTP server name")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(screen.queryByLabelText("HTTP server name")).not.toBeInTheDocument();
  });

  it("adds HTTP by PUTting the draft", async () => {
    const user = userEvent.setup();
    const puts: unknown[] = [];
    mockFetch((url, init) => {
      if (url === "/admin/mcp-settings") return jsonResponse(mcpPayload());
      if (url === "/mcp/tools") return jsonResponse({ tools: [] });
      if (url === "/admin/mcp-settings/drafts/draft-1" && init?.method === "PUT") {
        puts.push(JSON.parse(String(init.body || "{}")));
        return jsonResponse({
          draft: {
            id: "draft-1",
            revision: "rev-2",
            definition: JSON.parse(String(init.body || "{}")).definition,
          },
        });
      }
      return jsonResponse({});
    });
    renderPage(<McpPage />);
    await screen.findByRole("link", { name: "atlassian" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add MCP" }));
    expect(await screen.findByRole("dialog", { name: "Add MCP server" })).toBeInTheDocument();
    await user.type(screen.getByLabelText("HTTP server name"), "figma");
    await user.type(screen.getByLabelText("HTTP server URL"), "https://mcp.figma.com/mcp");
    await user.type(screen.getByLabelText("Header secret name"), "FIGMA_MCP_TOKEN");
    await user.click(screen.getByRole("button", { name: "Add HTTP" }));
    expect(puts).toHaveLength(1);
    expect((puts[0] as { definition: { servers: { figma: { url: string } } } }).definition.servers.figma.url).toBe(
      "https://mcp.figma.com/mcp"
    );
  });

  it("install copies catalog command into the draft", async () => {
    const user = userEvent.setup();
    const puts: Array<{ definition: { servers: Record<string, { command?: string }> } }> = [];
    mockFetch((url, init) => {
      if (url === "/admin/mcp-settings") return jsonResponse(mcpPayload());
      if (url === "/mcp/tools") return jsonResponse({ tools: [] });
      if (url === "/admin/mcp-settings/drafts/draft-1" && init?.method === "PUT") {
        const body = JSON.parse(String(init.body || "{}"));
        puts.push(body);
        return jsonResponse({
          draft: { id: "draft-1", revision: "rev-2", definition: body.definition },
        });
      }
      return jsonResponse({});
    });
    renderPage(<McpPage />);
    await user.click(await screen.findByRole("button", { name: "Add MCP" }));
    await user.click(screen.getByRole("tab", { name: "Catalog" }));
    await user.selectOptions(await screen.findByLabelText("Catalog entry"), "atlassian");
    await user.click(screen.getByRole("button", { name: "Install" }));
    expect(puts[0]?.definition.servers.atlassian.command).toBe("mcporter");
  });

  it("shows Remove for overlay-only servers and lists discovered tools", async () => {
    mockFetch((url) => {
      if (url === "/admin/mcp-settings") {
        return jsonResponse({
          ...mcpPayload(),
          yaml: {},
          merged: {
            figma: {
              transport: "streamable_http",
              url: "https://mcp.figma.com/mcp",
              enabled: true,
            },
          },
          sources: { figma: "overlay" },
          draft: {
            id: "draft-1",
            revision: "rev-1",
            definition: {
              definition_type: "mcp_settings",
              definition_version: "1.0.0",
              servers: {
                figma: {
                  transport: "streamable_http",
                  url: "https://mcp.figma.com/mcp",
                  enabled: true,
                },
              },
            },
          },
        });
      }
      if (url === "/mcp/tools") {
        return jsonResponse({ tools: [{ name: "mcp-figma-get_file", server: "figma" }] });
      }
      return jsonResponse({});
    });
    renderPage(<McpPage selectedName="figma" />);
    expect(await screen.findByRole("button", { name: "Remove" })).toBeInTheDocument();
    expect(await screen.findByText("mcp-figma-get_file")).toBeInTheDocument();
  });

  it("pages servers ten at a time and opens the selected server page", async () => {
    const merged = Object.fromEntries(
      Array.from({ length: 12 }, (_, index) => [
        `server-${String(index + 1).padStart(2, "0")}`,
        { transport: "stdio", command: "mcporter", enabled: true },
      ])
    );
    mockFetch((url) => {
      if (url === "/admin/mcp-settings") {
        return jsonResponse({
          ...mcpPayload(),
          merged,
          sources: Object.fromEntries(Object.keys(merged).map((name) => [name, "inherited"])),
        });
      }
      if (url === "/mcp/tools") return jsonResponse({ tools: [] });
      return jsonResponse({});
    });
    renderPage(<McpPage selectedName="server-12" />);
    expect(await screen.findByRole("link", { name: "server-12" })).toBeInTheDocument();
    expect(screen.getByText("Showing 11–12 of 12 servers")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "server-01" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Previous" }));
    expect(await screen.findByRole("link", { name: "server-01" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "server-12" })).not.toBeInTheDocument();
  });

  it("calls Activate", async () => {
    const user = userEvent.setup();
    const activates: unknown[] = [];
    mockFetch((url, init) => {
      if (url === "/admin/mcp-settings") return jsonResponse(mcpPayload());
      if (url === "/mcp/tools") return jsonResponse({ tools: [] });
      if (url === "/admin/mcp-settings/activate" && init?.method === "POST") {
        activates.push(JSON.parse(String(init.body || "{}")));
        return jsonResponse({ degraded: [], reload_ok: true });
      }
      return jsonResponse({});
    });
    renderPage(<McpPage />);
    await screen.findByRole("link", { name: "atlassian" });
    await user.click(screen.getByRole("button", { name: "Activate" }));
    expect(activates).toEqual([{ draft_id: "draft-1", expected_revision: "rev-1" }]);
  });
});
