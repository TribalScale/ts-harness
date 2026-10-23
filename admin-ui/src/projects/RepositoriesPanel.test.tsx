import { screen, waitFor } from "@testing-library/react";
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
            { suspended: false, repositories: ["acme/payments-api", "acme/web"] },
          ],
        });
      if (method === "POST") {
        posts.push(String(init?.body));
        repositories = [...repositories, { ...API, id: "r2", repo: "web", full_name: "acme/web", role: "web" }];
        return jsonResponse(repositories[1]);
      }
      return jsonResponse({ repositories });
    });
    renderPage(<RepositoriesPanel projectId="p1" canManage />);

    expect(await screen.findByText("acme/payments-api")).toBeInTheDocument();
    expect(screen.getByText("Project default")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Add repository" }));
    await waitFor(() =>
      expect(document.querySelectorAll("datalist option")).toHaveLength(1),
    );
    expect(document.querySelector("datalist option")).toHaveAttribute("value", "acme/web");
    const add = screen.getByRole("button", { name: "Add" });
    expect(add).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Repository (owner/name)"), "acme/web");
    await userEvent.type(screen.getByLabelText("Role (optional, e.g. web or api)"), "web");
    await userEvent.click(add);
    await waitFor(() => expect(screen.getByText("acme/web")).toBeInTheDocument());
    expect(JSON.parse(posts[0])).toEqual({ full_name: "acme/web", role: "web", default_branch: null });
  });

  it("is read-only for people who cannot manage the project", async () => {
    mockFetch(() => jsonResponse({ repositories: [API] }));
    renderPage(<RepositoriesPanel projectId="p1" canManage={false} />);
    expect(await screen.findByText("acme/payments-api")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add repository" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove" })).not.toBeInTheDocument();
  });
});
