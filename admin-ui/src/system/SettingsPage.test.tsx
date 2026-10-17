import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { SystemPage } from "./SystemPage";
import { SettingsPage } from "./SettingsPage";

function settingsPayload() {
  return {
    status: "empty",
    definition: { definition_type: "runtime_settings", definition_version: "1.0.0" },
    draft: null,
    yaml: {},
    merged: {
      program_defaults: {
        repo_picker: { repos: [{ id: "org/yaml", default_branch: "main" }] },
        jira_filing: { default_issue_type: "Story", issue_type_by_project: {} },
        ticket_picker: { statuses: ["To Do"] },
      },
      identity: {
        slack_user_emails: { U1: "yaml@example.com" },
        jira_account_ids: {},
      },
      policy: { budget_warn_usd: 40, budget_hard_stop_usd: 80 },
      sandbox: { timeout_seconds: 30, egress_policy: "deny", cpu_limit: 1, memory_mb: 512 },
      bindings: { slack: "orchestrator" },
      toolsets: { "core-read": ["read_file"] },
      connectors: { "google-workspace": { enabled: true, scopes: ["drive"] } },
    },
    sources: {
      "program_defaults.repo_picker": "inherited",
      "identity.slack_user_emails": "inherited",
    },
    redacted_yaml_draft: "gateway:\n  token: ***\n",
    redacted_yaml_live: "gateway:\n  token: ***\n",
  };
}

function draft(definition = settingsPayload().definition) {
  return { id: "draft-1", revision: "rev-1", definition };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Settings page", () => {
  it("shows policy, sandbox, channels, toolsets, and connector tabs", async () => {
    const user = userEvent.setup();
    const payload = settingsPayload();
    mockFetch((url, init) => {
      if (url === "/admin/runtime-settings") return jsonResponse(payload);
      if (url === "/admin/runtime-settings/drafts" && init?.method === "POST") {
        return jsonResponse({ draft: draft() });
      }
      return jsonResponse({});
    });
    renderPage(<SettingsPage section="policy" />);
    expect(await screen.findByRole("heading", { name: "Policy" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Sandbox" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Channels" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Toolsets" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Connector" })).toBeInTheDocument();
    const warn = screen.getByLabelText("Budget warn USD");
    expect(warn).toHaveValue(40);
    await user.clear(warn);
    await user.type(warn, "12");
    await user.click(screen.getByRole("button", { name: "Reset to inherited" }));
    expect(screen.getByLabelText("Budget warn USD")).toHaveValue(40);
  });

  it("shows the Settings tab on System", async () => {
    mockFetch(() => jsonResponse({ status: "ok", version: "1", components: {} }));
    renderPage(<SystemPage />);
    expect(await screen.findByRole("tab", { name: "Settings" })).toBeInTheDocument();
    expect(screen.getByText(/tenant settings/i)).toBeInTheDocument();
  });

  it("renders Identity heading for the identity section", async () => {
    mockFetch((url) => {
      if (url === "/admin/runtime-settings") return jsonResponse(settingsPayload());
      if (url === "/admin/runtime-settings/drafts") {
        return jsonResponse({ draft: draft() });
      }
      return jsonResponse({});
    });
    renderPage(<SystemPage section="settings" settingsSection="identity" />);
    expect(await screen.findByRole("heading", { name: "Identity" })).toBeInTheDocument();
  });

  it("falls back to Jira defaults for an unknown settings section", async () => {
    mockFetch((url) => {
      if (url === "/admin/runtime-settings") return jsonResponse(settingsPayload());
      if (url === "/admin/runtime-settings/drafts") {
        return jsonResponse({ draft: draft() });
      }
      return jsonResponse({});
    });
    renderPage(<SystemPage section="settings" settingsSection={"defaults"} />);
    expect(await screen.findByRole("heading", { name: "Jira defaults" })).toBeInTheDocument();
  });

  it("adds a repo and resets overlay key", async () => {
    const user = userEvent.setup();
    const definition = draft().definition;
    mockFetch((url, init) => {
      if (url === "/admin/runtime-settings") return jsonResponse(settingsPayload());
      if (url === "/admin/runtime-settings/drafts" && init?.method === "POST") {
        return jsonResponse({ draft: draft(definition) });
      }
      return jsonResponse({});
    });
    renderPage(<SettingsPage section="defaults" />);
    await screen.findByRole("heading", { name: "Jira defaults" });
    await user.type(screen.getByLabelText("Repo id"), "org/checkout-service");
    await user.click(screen.getByRole("button", { name: "Add repo" }));
    expect(screen.getByText("org/checkout-service")).toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: "Reset to inherited" })[0]);
    expect(screen.queryByText("org/checkout-service")).not.toBeInTheDocument();
    expect(screen.getByText("org/yaml")).toBeInTheDocument();
  });

  it("shows validate errors", async () => {
    const user = userEvent.setup();
    mockFetch((url, init) => {
      if (url === "/admin/runtime-settings") return jsonResponse(settingsPayload());
      if (url === "/admin/runtime-settings/drafts" && init?.method === "POST") {
        return jsonResponse({ draft: draft() });
      }
      if (url.includes("/validate")) {
        return jsonResponse({
          valid: false,
          issues: [{ severity: "error", message: "duplicate email: ada@example.com" }],
        });
      }
      if (url.includes("/drafts/") && init?.method === "PUT") {
        return jsonResponse({ draft: draft() });
      }
      return jsonResponse({});
    });
    renderPage(<SettingsPage section="identity" />);
    await screen.findByRole("heading", { name: "Identity" });
    await user.click(screen.getByRole("button", { name: "Validate" }));
    expect(await screen.findByText("duplicate email: ada@example.com")).toBeInTheDocument();
  });

  it("shows Remove after adding a repo to an inherited list", async () => {
    const user = userEvent.setup();
    mockFetch((url, init) => {
      if (url === "/admin/runtime-settings") return jsonResponse(settingsPayload());
      if (url === "/admin/runtime-settings/drafts" && init?.method === "POST") {
        return jsonResponse({ draft: draft() });
      }
      return jsonResponse({});
    });
    renderPage(<SettingsPage section="defaults" />);
    await screen.findByRole("heading", { name: "Jira defaults" });
    expect(screen.getAllByText("Source: inherited").length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Remove" })).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("Repo id"), "org/checkout-service");
    await user.click(screen.getByRole("button", { name: "Add repo" }));
    expect(screen.getByText("Source: overlay")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Remove" }).length).toBeGreaterThan(0);
  });

  it("reuses a routing store draft and does not recreate it when changing tabs", async () => {
    const user = userEvent.setup();
    const routingDefinition = {
      definition_type: "channel_routing",
      definition_version: "1.0.0",
      intents: [{ id: "governed_prd", signals: ["create a prd"], workflow_type: "prd_request" }],
    };
    const existingDraft = { id: "existing-r1", revision: "r-rev-1", definition: routingDefinition };
    const posts: string[] = [];
    mockFetch((url, init) => {
      if (url === "/admin/runtime-settings") return jsonResponse(settingsPayload());
      if (url === "/admin/runtime-settings/drafts" && init?.method === "POST") {
        return jsonResponse({ draft: draft() });
      }
      if (url === "/admin/routing-definitions") {
        return jsonResponse({
          status: "active",
          definition: routingDefinition,
          draft: existingDraft,
          yaml_intents: routingDefinition.intents,
          merged_intents: routingDefinition.intents,
        });
      }
      if (url === "/admin/routing-definitions/drafts" && init?.method === "POST") {
        posts.push(url);
        return jsonResponse({ draft: { id: "new-r1", revision: "1", definition: routingDefinition } });
      }
      return jsonResponse({});
    });
    const { rerender } = renderPage(<SettingsPage section="routing" />);
    await screen.findByRole("heading", { name: "Routing" });
    expect(screen.getByText("governed_prd")).toBeInTheDocument();
    expect(posts).toEqual([]);
    rerender(<SettingsPage section="advanced" />);
    await screen.findByRole("heading", { name: "Advanced" });
    rerender(<SettingsPage section="routing" />);
    await screen.findByRole("heading", { name: "Routing" });
    expect(posts).toEqual([]);
    await user.click(screen.getByRole("button", { name: "governed_prd" }));
    await user.clear(screen.getByLabelText("Intent signals"));
    await user.type(screen.getByLabelText("Intent signals"), "keep-this-signal");
    rerender(<SettingsPage section="advanced" />);
    rerender(<SettingsPage section="routing" />);
    expect(screen.getByLabelText("Intent signals")).toHaveValue("keep-this-signal");
  });

  it("activates only dirty stores through the page-level endpoint", async () => {
    const user = userEvent.setup();
    const routingDefinition = {
      definition_type: "channel_routing",
      definition_version: "1.0.0",
      intents: [],
    };
    const calls: Array<{ url: string; method?: string; body?: Record<string, unknown> }> = [];
    mockFetch((url, init) => {
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      calls.push({ url, method: init?.method, body });
      if (url === "/admin/runtime-settings") return jsonResponse(settingsPayload());
      if (url === "/admin/runtime-settings/drafts" && init?.method === "POST") {
        return jsonResponse({ draft: draft() });
      }
      if (url === "/admin/routing-definitions") {
        return jsonResponse({
          status: "active",
          definition: routingDefinition,
          yaml_intents: [],
          merged_intents: [],
        });
      }
      if (url === "/admin/routing-definitions/drafts" && init?.method === "POST") {
        return jsonResponse({ draft: { id: "r1", revision: "1", definition: routingDefinition } });
      }
      if (url.includes("/drafts/") && init?.method === "PUT") {
        if (url.includes("routing-definitions")) {
          return jsonResponse({ draft: { id: "r1", revision: "1", definition: routingDefinition } });
        }
        return jsonResponse({
          draft: draft({
            ...draft().definition,
            program_defaults: { repo_picker: { repos: [{ id: "org/checkout-service" }] } },
          }),
        });
      }
      if (url === "/admin/runtime-settings/activate") {
        return jsonResponse({ reload_ok: true, issues: [] });
      }
      return jsonResponse({});
    });
    const { rerender } = renderPage(<SettingsPage section="routing" />);
    await screen.findByRole("heading", { name: "Routing" });
    rerender(<SettingsPage section="defaults" />);
    await screen.findByRole("heading", { name: "Jira defaults" });
    await user.type(screen.getByLabelText("Repo id"), "org/checkout-service");
    await user.click(screen.getByRole("button", { name: "Add repo" }));
    await user.click(screen.getByRole("button", { name: "Activate" }));
    const activate = calls.find((item) => item.url === "/admin/runtime-settings/activate");
    expect(activate?.method).toBe("POST");
    expect(activate?.body).toEqual({
      runtime_settings_draft_id: "draft-1",
      runtime_settings_expected_revision: "rev-1",
    });
    expect(
      calls.some(
        (item) =>
          item.url.includes("/activate") &&
          item.url !== "/admin/runtime-settings/activate"
      )
    ).toBe(false);
  });

  it("skips the settings store when only routing is dirty", async () => {
    const user = userEvent.setup();
    const routingDefinition = {
      definition_type: "channel_routing",
      definition_version: "1.0.0",
      intents: [{ id: "governed_prd", signals: ["create a prd"], workflow_type: "prd_request" }],
    };
    const calls: Array<{ url: string; body?: Record<string, unknown> }> = [];
    mockFetch((url, init) => {
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      calls.push({ url, body });
      if (url === "/admin/runtime-settings") return jsonResponse(settingsPayload());
      if (url === "/admin/runtime-settings/drafts" && init?.method === "POST") {
        return jsonResponse({ draft: draft() });
      }
      if (url === "/admin/routing-definitions") {
        return jsonResponse({
          status: "active",
          definition: routingDefinition,
          yaml_intents: routingDefinition.intents,
          merged_intents: routingDefinition.intents,
        });
      }
      if (url === "/admin/routing-definitions/drafts" && init?.method === "POST") {
        return jsonResponse({ draft: { id: "r1", revision: "1", definition: routingDefinition } });
      }
      if (url.includes("/routing-definitions/") && init?.method === "PUT") {
        return jsonResponse({
          draft: {
            id: "r1",
            revision: "1",
            definition: {
              ...routingDefinition,
              intents: [{ id: "governed_prd", signals: ["create a prd", "extra"], workflow_type: "prd_request" }],
            },
          },
        });
      }
      if (url === "/admin/runtime-settings/activate") {
        return jsonResponse({ reload_ok: true, issues: [] });
      }
      return jsonResponse({});
    });
    renderPage(<SettingsPage section="routing" />);
    await screen.findByRole("heading", { name: "Routing" });
    await user.click(screen.getByRole("button", { name: "governed_prd" }));
    await user.type(screen.getByLabelText("Intent signals"), ", extra");
    await user.click(screen.getByRole("button", { name: "Activate" }));
    const activate = calls.find((item) => item.url === "/admin/runtime-settings/activate");
    expect(activate?.body).toEqual({
      routing_draft_id: "r1",
      routing_expected_revision: "1",
    });
  });

  it("disables Activate after Validate reports errors", async () => {
    const user = userEvent.setup();
    mockFetch((url, init) => {
      if (url === "/admin/runtime-settings") return jsonResponse(settingsPayload());
      if (url === "/admin/runtime-settings/drafts" && init?.method === "POST") {
        return jsonResponse({ draft: draft() });
      }
      if (url.includes("/validate")) {
        return jsonResponse({
          valid: false,
          issues: [{ severity: "error", message: "duplicate email: ada@example.com" }],
        });
      }
      if (url.includes("/drafts/") && init?.method === "PUT") {
        return jsonResponse({ draft: draft() });
      }
      return jsonResponse({});
    });
    renderPage(<SettingsPage section="identity" />);
    await screen.findByRole("heading", { name: "Identity" });
    await user.click(screen.getByRole("button", { name: "Validate" }));
    expect(await screen.findByText("duplicate email: ada@example.com")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Activate" })).toBeDisabled();
  });

  it("shows Validated after a successful validate and refreshes Advanced YAML", async () => {
    const user = userEvent.setup();
    let settingsGets = 0;
    mockFetch((url, init) => {
      if (url === "/admin/runtime-settings") {
        settingsGets += 1;
        return jsonResponse({
          ...settingsPayload(),
          redacted_yaml_draft:
            settingsGets === 1
              ? "gateway:\n  token: ***\n"
              : "gateway:\n  token: ***\nprogram_defaults:\n  overlay: true\n",
        });
      }
      if (url === "/admin/runtime-settings/drafts" && init?.method === "POST") {
        return jsonResponse({ draft: draft() });
      }
      if (url.includes("/validate")) {
        return jsonResponse({ valid: true, issues: [] });
      }
      if (url.includes("/drafts/") && init?.method === "PUT") {
        return jsonResponse({ draft: draft() });
      }
      return jsonResponse({});
    });
    const { rerender } = renderPage(<SettingsPage section="defaults" />);
    await screen.findByRole("heading", { name: "Jira defaults" });
    await user.click(screen.getByRole("button", { name: "Validate" }));
    expect(await screen.findByText("Validated")).toBeInTheDocument();
    rerender(<SettingsPage section="advanced" />);
    expect(await screen.findByLabelText("Redacted YAML")).toHaveValue(
      "gateway:\n  token: ***\nprogram_defaults:\n  overlay: true\n"
    );
  });

  it("shows draft and active revisions and uses ErrorBanner for persist errors", async () => {
    const user = userEvent.setup();
    mockFetch((url, init) => {
      if (url === "/admin/runtime-settings") {
        return jsonResponse({
          ...settingsPayload(),
          status: "active",
          definition: { definition_type: "runtime_settings", definition_version: "1.0.0" },
          draft: { id: "draft-1", revision: "c3d7bd12abcd", definition: draft().definition },
        });
      }
      if (url.includes("/drafts/") && init?.method === "PUT") {
        return jsonResponse({ error: "revision conflict" }, 409);
      }
      return jsonResponse({});
    });
    renderPage(<SettingsPage section="defaults" />);
    expect(await screen.findByText(/runtime-settings draft c3d7bd12/)).toBeInTheDocument();
    expect(screen.getByText(/active 1\.0\.0/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Validate" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("revision conflict");
    expect(screen.getByRole("alert")).toHaveClass("error");
  });

  it("reloads routing and clears dirty after Activate", async () => {
    const user = userEvent.setup();
    const routingDefinition = {
      definition_type: "channel_routing",
      definition_version: "1.0.0",
      intents: [{ id: "governed_prd", signals: ["create a prd"], workflow_type: "prd_request" }],
    };
    let routingGets = 0;
    mockFetch((url, init) => {
      if (url === "/admin/runtime-settings") return jsonResponse(settingsPayload());
      if (url === "/admin/runtime-settings/drafts" && init?.method === "POST") {
        return jsonResponse({ draft: draft() });
      }
      if (url === "/admin/routing-definitions") {
        routingGets += 1;
        return jsonResponse({
          status: "active",
          definition: routingDefinition,
          yaml_intents: routingDefinition.intents,
          merged_intents: routingDefinition.intents,
        });
      }
      if (url === "/admin/routing-definitions/drafts" && init?.method === "POST") {
        return jsonResponse({
          draft: {
            id: routingGets > 1 ? "r2" : "r1",
            revision: routingGets > 1 ? "2" : "1",
            definition: routingDefinition,
          },
        });
      }
      if (url.includes("/drafts/") && init?.method === "PUT") {
        if (url.includes("routing-definitions")) {
          return jsonResponse({
            draft: { id: "r1", revision: "1", definition: routingDefinition },
          });
        }
        return jsonResponse({ draft: draft() });
      }
      if (url === "/admin/runtime-settings/activate") {
        return jsonResponse({ reload_ok: true, issues: [] });
      }
      return jsonResponse({});
    });
    renderPage(<SettingsPage section="routing" />);
    await screen.findByRole("heading", { name: "Routing" });
    await user.click(screen.getByRole("button", { name: "governed_prd" }));
    await user.type(screen.getByLabelText("Intent signals"), ", extra");
    expect(screen.getByText(/Draft dirty/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Activate" }));
    expect(await screen.findByText(/Draft saved/)).toBeInTheDocument();
    expect(routingGets).toBeGreaterThan(1);
  });

  it("shows a routing collision warning from validate", async () => {
    const user = userEvent.setup();
    const routingDefinition = {
      definition_type: "channel_routing",
      definition_version: "1.0.0",
      intents: [],
    };
    let routingGets = 0;
    mockFetch((url, init) => {
      if (url === "/admin/runtime-settings") return jsonResponse(settingsPayload());
      if (url === "/admin/runtime-settings/drafts" && init?.method === "POST") {
        return jsonResponse({ draft: draft() });
      }
      if (url === "/admin/routing-definitions") {
        routingGets += 1;
        return jsonResponse({
          status: "active",
          definition: routingDefinition,
          yaml_intents: [],
          merged_intents: [],
        });
      }
      if (url === "/admin/routing-definitions/drafts" && init?.method === "POST") {
        return jsonResponse({ draft: { id: "r1", revision: "1", definition: routingDefinition } });
      }
      if (url.includes("/runtime-settings/") && url.includes("/validate")) {
        return jsonResponse({ valid: true, issues: [] });
      }
      if (url.includes("/routing-definitions/") && url.includes("/validate")) {
        return jsonResponse({
          issues: [{ severity: "warning", message: "signal collision: create a prd" }],
        });
      }
      if (url.includes("/drafts/") && init?.method === "PUT") {
        if (url.includes("routing-definitions")) {
          return jsonResponse({
            draft: { id: "r1", revision: "1", definition: routingDefinition },
          });
        }
        return jsonResponse({ draft: draft() });
      }
      return jsonResponse({});
    });
    renderPage(<SettingsPage section="routing" />);
    await screen.findByRole("heading", { name: "Routing" });
    await user.click(screen.getByRole("button", { name: "Validate" }));
    expect(await screen.findByText("signal collision: create a prd")).toBeInTheDocument();
    expect(routingGets).toBeGreaterThan(1);
  });
});
