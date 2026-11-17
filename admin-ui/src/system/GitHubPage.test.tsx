import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { GitHubPage } from "./GitHubPage";

function overview(canSync: boolean) {
  return {
    apps: [
      {
        host: "github.com",
        configured: true,
        app_id: "42",
        app_slug: "ts-harness-acme",
        source: "environment",
        webhook_secret: true,
        install_url: "https://github.com/apps/ts-harness-acme/installations/new",
      },
    ],
    installations: [
      {
        host: "github.com",
        installation_id: 7,
        account_login: "acme",
        account_type: "Organization",
        repository_selection: "selected",
        suspended: false,
        repositories: ["acme/web", "acme/api"],
        synced_at: "2026-10-01T00:00:00Z",
      },
    ],
    webhooks: [{ host: "github.com", last_received_at: "2026-10-01T00:00:00Z", last_day: 3 }],
    rate_limits: [{ host: "github.com", remaining: 4990, limit: 5000, reset: 0, observed_at: 0 }],
    pat_fallback: true,
    can_sync: canSync,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("GitHubPage", () => {
  it("shows the app, installations and rate limit, and syncs installations", async () => {
    const calls: { url: string; method: string; body?: string }[] = [];
    mockFetch((url, init) => {
      const method = (init?.method || "GET").toUpperCase();
      calls.push({ url, method, body: init?.body as string | undefined });
      if (url.endsWith("/admin/github/sync")) return jsonResponse({ host: "github.com", installations: 1, removed: 0 });
      return jsonResponse(overview(true));
    });
    renderPage(<GitHubPage />);

    expect(await screen.findByText("ts-harness-acme (42)")).toBeInTheDocument();
    expect(screen.getByText("acme/web, acme/api")).toBeInTheDocument();
    expect(screen.getByText("4990 / 5000")).toBeInTheDocument();
    expect(screen.getByText(/use the personal access token/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Install on an organization" })).toHaveAttribute(
      "href",
      "https://github.com/apps/ts-harness-acme/installations/new"
    );

    await userEvent.click(screen.getByRole("button", { name: "Sync installations" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/admin/github/sync") && c.method === "POST")).toBe(true));
    const sync = calls.find((c) => c.url.endsWith("/admin/github/sync"));
    expect(JSON.parse(String(sync?.body))).toEqual({ host: "github.com" });
  });

  it("hides sync from non-admins", async () => {
    mockFetch(() => jsonResponse(overview(false)));
    renderPage(<GitHubPage />);
    expect(await screen.findByText("acme")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sync installations" })).not.toBeInTheDocument();
  });
});
