import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { ProjectSettingsPage } from "./ProjectSettingsPage";
import { RegistriesPanel } from "./RegistriesPanel";

const REGISTRY = {
  id: "r1",
  project_id: "p1",
  type: "npm",
  url: "https://npm.acme.dev",
  scope: "@acme",
  username: null,
  has_credential: true,
  last_tested_at: null,
  last_test_ok: false,
  last_test_error: "HTTP 401",
};

const PROJECT = {
  id: "p1",
  tenant_id: "default",
  key: "APP",
  name: "App",
  team_slug: "web",
  jira_project_key: null,
  default_base_branch: null,
  worker_cpu: null,
  worker_memory_mb: null,
  can_manage: true,
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe("RegistriesPanel", () => {
  it("lists registries and sends the token only on save", async () => {
    const calls: { url: string; method: string; body?: string }[] = [];
    let registries = [REGISTRY];
    mockFetch((url, init) => {
      const method = (init?.method || "GET").toUpperCase();
      calls.push({ url, method, body: init?.body as string | undefined });
      if (method === "POST" && url.endsWith("/registries")) {
        const body = JSON.parse(String(init?.body));
        registries = [...registries, { ...REGISTRY, id: "r2", type: body.type, url: body.url, last_test_ok: null }];
        return jsonResponse(registries[1]);
      }
      if (url.endsWith("/registries")) return jsonResponse({ registries });
      return jsonResponse({});
    });
    renderPage(<RegistriesPanel projectId="p1" />);

    expect(await screen.findByText("https://npm.acme.dev")).toBeInTheDocument();
    expect(screen.getByText("HTTP 401")).toBeInTheDocument();
    expect(screen.queryByText(/arn:/)).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Add registry" }));
    await userEvent.selectOptions(screen.getByLabelText("Registry type"), "pypi");
    await userEvent.type(screen.getByLabelText("Registry URL"), "https://pypi.acme.dev/simple");
    await userEvent.type(screen.getByLabelText("Registry token"), "s3cret");
    await userEvent.click(screen.getByRole("button", { name: "Save registry" }));

    await waitFor(() => expect(screen.getByText("https://pypi.acme.dev/simple")).toBeInTheDocument());
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.url).toBe("/admin/projects/p1/registries");
    expect(JSON.parse(post.body!)).toMatchObject({ type: "pypi", url: "https://pypi.acme.dev/simple", token: "s3cret" });
    expect(screen.queryByLabelText("Registry token")).not.toBeInTheDocument();
  });

  it("editing without a new token keeps the stored credential", async () => {
    const puts: string[] = [];
    mockFetch((url, init) => {
      if (init?.method === "PUT") {
        puts.push(String(init.body));
        return jsonResponse(REGISTRY);
      }
      return jsonResponse({ registries: [REGISTRY] });
    });
    renderPage(<RegistriesPanel projectId="p1" />);
    const row = (await screen.findByText("https://npm.acme.dev")).closest("tr")!;
    await userEvent.click(within(row).getByRole("button", { name: "Edit" }));
    expect(screen.getByText(/leave blank to keep the current one/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Save registry" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(JSON.parse(puts[0])).not.toHaveProperty("token");
  });
});

describe("ProjectSettingsPage", () => {
  it("validates worker sizing before saving", async () => {
    mockFetch((url) => {
      if (url.endsWith("/registries")) return jsonResponse({ registries: [] });
      return jsonResponse({ projects: [PROJECT], is_admin: false });
    });
    renderPage(<ProjectSettingsPage selectedId="p1" />);
    await userEvent.selectOptions(await screen.findByLabelText("Worker CPU"), "1024");
    expect(screen.getByRole("alert")).toHaveTextContent("Set both CPU and memory");
    await userEvent.type(screen.getByLabelText("Worker memory"), "16384");
    expect(screen.getByRole("alert")).toHaveTextContent("2048–8192");
    expect(screen.getByRole("button", { name: "Save project" })).toBeDisabled();
    expect(screen.getByLabelText("Owning team")).toBeDisabled();
    expect(screen.queryByRole("form", { name: "New project" })).not.toBeInTheDocument();
  });

  it("hides registries from people who cannot manage the project", async () => {
    mockFetch(() => jsonResponse({ projects: [{ ...PROJECT, can_manage: false }], is_admin: false }));
    renderPage(<ProjectSettingsPage selectedId="p1" />);
    expect(await screen.findByText(/Only admins and managers/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save project" })).not.toBeInTheDocument();
  });
});
