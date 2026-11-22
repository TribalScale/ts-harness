import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { ProjectSettingsPage } from "./ProjectSettingsPage";
import { RegistriesPanel } from "./RegistriesPanel";
import { getAlert } from "../test/alerts";

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

const NO_CHECKLIST = { project_id: "p1", items: [], ready: 0, total: 0, summary: "" };

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
        registries = [
          ...registries,
          {
            ...REGISTRY,
            id: "r2",
            type: body.type,
            url: body.url,
            last_test_ok: null,
          },
        ];
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
    await userEvent.selectOptions(
      screen.getByLabelText("Registry type"),
      "pypi",
    );
    await userEvent.type(
      screen.getByLabelText("Registry URL"),
      "https://pypi.acme.dev/simple",
    );
    await userEvent.type(screen.getByLabelText("Registry token"), "s3cret");
    await userEvent.click(
      screen.getByRole("button", { name: "Save registry" }),
    );

    await waitFor(() =>
      expect(
        screen.getByText("https://pypi.acme.dev/simple"),
      ).toBeInTheDocument(),
    );
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.url).toBe("/admin/projects/p1/registries");
    expect(JSON.parse(post.body!)).toMatchObject({
      type: "pypi",
      url: "https://pypi.acme.dev/simple",
      token: "s3cret",
    });
    expect(screen.queryByLabelText("Registry token")).not.toBeInTheDocument();
  });

  it("does not save a registry without a URL when Enter submits the form", async () => {
    const writes: string[] = [];
    mockFetch((url, init) => {
      if ((init?.method || "GET").toUpperCase() !== "GET") writes.push(url);
      return jsonResponse({ registries: [] });
    });
    renderPage(<RegistriesPanel projectId="p1" />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Add registry" }),
    );
    await userEvent.type(
      screen.getByLabelText("Registry token"),
      "s3cret{Enter}",
    );
    expect(writes).toEqual([]);
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
    const row = (await screen.findByText("https://npm.acme.dev")).closest(
      "tr",
    )!;
    await userEvent.click(within(row).getByRole("button", { name: "Edit" }));
    expect(
      screen.getByText(/leave blank to keep the current one/),
    ).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: "Save registry" }),
    );
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(JSON.parse(puts[0])).not.toHaveProperty("token");
  });
});

describe("ProjectSettingsPage", () => {
  it("validates worker sizing before saving", async () => {
    mockFetch((url) => {
      if (url.endsWith("/registries")) return jsonResponse({ registries: [] });
      if (url.endsWith("/checklist")) return jsonResponse(NO_CHECKLIST);
      return jsonResponse({ projects: [PROJECT], is_admin: false });
    });
    renderPage(<ProjectSettingsPage selectedId="p1" />);
    const workers = await screen.findByRole("form", { name: "Coding workers" });
    await userEvent.selectOptions(
      within(workers).getByLabelText("Worker CPU"),
      "1024",
    );
    expect(getAlert()).toHaveTextContent("Set both CPU and memory");
    await userEvent.type(
      within(workers).getByLabelText("Worker memory"),
      "16384",
    );
    expect(getAlert()).toHaveTextContent("2048–8192");
    expect(
      within(workers).getByRole("button", { name: "Save changes" }),
    ).toBeDisabledControl();
    expect(screen.getByLabelText("Owning team")).toBeDisabledControl();
    expect(
      screen.queryByRole("button", { name: "New project" }),
    ).not.toBeInTheDocument();
  });

  // Astryx buttons are aria-disabled, so Enter in a field still submits the form: the handlers
  // must refuse what the disabled button refuses.
  it("does not save invalid worker sizing when Enter submits the form", async () => {
    const writes: string[] = [];
    mockFetch((url, init) => {
      if ((init?.method || "GET").toUpperCase() !== "GET") writes.push(url);
      if (url.endsWith("/registries")) return jsonResponse({ registries: [] });
      if (url.endsWith("/checklist")) return jsonResponse(NO_CHECKLIST);
      return jsonResponse({ projects: [PROJECT], is_admin: false });
    });
    renderPage(<ProjectSettingsPage selectedId="p1" />);
    const workers = await screen.findByRole("form", { name: "Coding workers" });
    await userEvent.selectOptions(
      within(workers).getByLabelText("Worker CPU"),
      "1024",
    );
    await userEvent.type(
      within(workers).getByLabelText("Worker memory"),
      "99{Enter}",
    );
    expect(getAlert()).toHaveTextContent("2048–8192");
    expect(writes).toEqual([]);
  });

  it("each card saves its own fields and keeps the rest as saved", async () => {
    const bodies: Record<string, unknown>[] = [];
    let project = { ...PROJECT, worker_cpu: 2048, worker_memory_mb: 8192 };
    mockFetch((url, init) => {
      if ((init?.method || "GET").toUpperCase() === "PUT") {
        const body = JSON.parse(String(init?.body));
        bodies.push(body);
        project = { ...project, ...body, name: body.name };
        return jsonResponse(project);
      }
      if (url.endsWith("/registries")) return jsonResponse({ registries: [] });
      if (url.includes("/repositories"))
        return jsonResponse({ repositories: [] });
      return jsonResponse({ projects: [project], is_admin: true });
    });
    renderPage(<ProjectSettingsPage selectedId="p1" />);
    const delivery = await screen.findByRole("form", {
      name: "Project settings",
    });
    const save = within(delivery).getByRole("button", { name: "Save changes" });
    expect(save).toBeDisabledControl();
    await userEvent.type(
      within(delivery).getByLabelText("Jira project key"),
      "thd",
    );
    await userEvent.clear(
      within(delivery).getByLabelText("Default base branch"),
    );
    await userEvent.type(
      within(delivery).getByLabelText("Default base branch"),
      "fe-dev",
    );
    await userEvent.click(save);
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toMatchObject({
      jira_project_key: "THD",
      default_base_branch: "fe-dev",
      worker_cpu: 2048,
      worker_memory_mb: 8192,
    });
    expect(await within(delivery).findByText("Saved")).toBeInTheDocument();
  });

  it("creates a project with its Jira link from the dialog", async () => {
    const posts: Record<string, unknown>[] = [];
    mockFetch((url, init) => {
      if ((init?.method || "GET").toUpperCase() === "POST") {
        posts.push(JSON.parse(String(init?.body)));
        return jsonResponse({ ...PROJECT, id: "p2", key: "PAY" });
      }
      return jsonResponse({ projects: [PROJECT], is_admin: true });
    });
    renderPage(<ProjectSettingsPage />);
    await userEvent.click(
      await screen.findByRole("button", { name: "New project" }),
    );
    const form = screen.getByRole("form", { name: "New project" });
    const create = within(form).getByRole("button", { name: "Create project" });
    expect(create).toBeDisabledControl();
    await userEvent.type(within(form).getByLabelText("New project key"), "pay");
    await userEvent.type(
      within(form).getByLabelText("New project Jira key"),
      "pay",
    );
    await userEvent.type(
      within(form).getByLabelText("New project base branch"),
      "main",
    );
    await userEvent.click(create);
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toEqual({
      key: "PAY",
      name: "PAY",
      team_slug: null,
      jira_project_key: "PAY",
      default_base_branch: "main",
    });
  });

  it("does not create a project with an empty key when Enter submits the form", async () => {
    const writes: string[] = [];
    mockFetch((url, init) => {
      if ((init?.method || "GET").toUpperCase() !== "GET") writes.push(url);
      return jsonResponse({ projects: [PROJECT], is_admin: true });
    });
    renderPage(<ProjectSettingsPage selectedId="p1" />);
    await userEvent.click(
      await screen.findByRole("button", { name: "New project" }),
    );
    await userEvent.type(
      screen.getByLabelText("New project key"),
      "   {Enter}",
    );
    expect(writes).toEqual([]);
  });

  it("hides registries from people who cannot manage the project", async () => {
    mockFetch(() =>
      jsonResponse({
        projects: [{ ...PROJECT, can_manage: false }],
        is_admin: false,
      }),
    );
    renderPage(<ProjectSettingsPage selectedId="p1" />);
    expect(
      await screen.findByText(/Only admins and managers/),
    ).toBeInTheDocument();
    expect(screen.getByText("Read only")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Save changes" }),
    ).not.toBeInTheDocument();
  });
});
