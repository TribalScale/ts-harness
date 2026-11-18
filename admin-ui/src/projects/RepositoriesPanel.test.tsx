import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { RepositoriesPanel } from "./RepositoriesPanel";

const API = {
  id: "r1",
  project_id: "p1",
  github_host: "github.com",
  owner: "acme",
  repo: "payments-api",
  full_name: "acme/payments-api",
  repo_url: "https://github.com/acme/payments-api",
  installation_id: 7,
  default_branch: null,
  role: "api",
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe("RepositoriesPanel", () => {
  it("lists repositories and adds one from the App installations", async () => {
    const posts: string[] = [];
    let repositories = [API];
    mockFetch((url, init) => {
      const method = (init?.method || "GET").toUpperCase();
      if (url === "/admin/github")
        return jsonResponse({
          installations: [
            {
              suspended: false,
              repositories: ["acme/payments-api", "acme/web"],
            },
          ],
        });
      if (method === "POST") {
        posts.push(String(init?.body));
        repositories = [
          ...repositories,
          { ...API, id: "r2", repo: "web", full_name: "acme/web", role: "web" },
        ];
        return jsonResponse(repositories[1]);
      }
      return jsonResponse({ repositories });
    });
    renderPage(<RepositoriesPanel projectId="p1" canManage />);

    expect(await screen.findByText("acme/payments-api")).toBeInTheDocument();
    expect(screen.getByText(/Project default/)).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: "Add repository" }),
    );
    await waitFor(() =>
      expect(document.querySelectorAll("datalist option")).toHaveLength(1),
    );
    expect(document.querySelector("datalist option")).toHaveAttribute(
      "value",
      "acme/web",
    );
    const add = screen.getByRole("button", { name: "Add" });
    expect(add).toBeDisabledControl();
    await userEvent.type(
      screen.getByLabelText("Repository (owner/name)"),
      "acme/web",
    );
    await userEvent.type(
      screen.getByLabelText("Role (optional, e.g. web or api)"),
      "web",
    );
    await userEvent.click(add);
    await waitFor(() =>
      expect(screen.getByText("acme/web")).toBeInTheDocument(),
    );
    expect(JSON.parse(posts[0])).toEqual({
      full_name: "acme/web",
      role: "web",
      default_branch: null,
    });
  });

  it("edits a repository's branch and role", async () => {
    const puts: { url: string; body: unknown }[] = [];
    let repositories = [API];
    mockFetch((url, init) => {
      if ((init?.method || "GET").toUpperCase() === "PUT") {
        const body = JSON.parse(String(init?.body));
        puts.push({ url, body });
        repositories = [{ ...API, ...body }];
        return jsonResponse(repositories[0]);
      }
      return jsonResponse({ repositories });
    });
    renderPage(
      <RepositoriesPanel projectId="p1" canManage projectDefaultBranch="dev" />,
    );
    expect(await screen.findByText(/Project default/)).toHaveTextContent(
      "Project default · dev",
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Edit acme/payments-api" }),
    );
    await userEvent.type(
      screen.getByLabelText("Base branch for acme/payments-api"),
      "fe-dev",
    );
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]).toEqual({
      url: "/admin/projects/p1/repositories/r1",
      body: { role: "api", default_branch: "fe-dev" },
    });
    expect(await screen.findByText("fe-dev")).toBeInTheDocument();
  });

  it("asks before removing a repository", async () => {
    const deletes: string[] = [];
    mockFetch((url, init) => {
      if ((init?.method || "GET").toUpperCase() === "DELETE") {
        deletes.push(url);
        return jsonResponse({});
      }
      return jsonResponse({ repositories: deletes.length ? [] : [API] });
    });
    renderPage(<RepositoriesPanel projectId="p1" canManage />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Remove acme/payments-api" }),
    );
    expect(deletes).toEqual([]);
    const dialog = screen.getByRole("dialog", { name: "Remove repository?" });
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Remove" }),
    );
    await waitFor(() =>
      expect(deletes).toEqual(["/admin/projects/p1/repositories/r1"]),
    );
    expect(await screen.findByText(/No repositories yet/)).toBeInTheDocument();
  });

  it("is read-only for people who cannot manage the project", async () => {
    mockFetch(() => jsonResponse({ repositories: [API] }));
    renderPage(<RepositoriesPanel projectId="p1" canManage={false} />);
    expect(await screen.findByText("acme/payments-api")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Add repository" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Remove" }),
    ).not.toBeInTheDocument();
  });
});
