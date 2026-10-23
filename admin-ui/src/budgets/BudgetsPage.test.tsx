import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { BudgetsPage } from "./BudgetsPage";

function overview(admin: boolean) {
  return {
    month: "2026-10-01",
    currency: "USD",
    default_team_monthly: 5000,
    warn_at_percent: 80,
    manager_max_monthly: 10000,
    is_admin: admin,
    raise_limit: admin ? null : 10000,
    unassigned_spent_usd: admin ? 12.5 : undefined,
    teams: [
      {
        id: "t-web",
        slug: "web",
        display_name: "Web",
        budget_usd: 5000,
        spent_usd: 5012.4,
        percent: 100.2,
        status: "reached",
        held_tasks: 3,
        overrides: [],
        can_raise: true,
      },
      {
        id: "t-data",
        slug: "data",
        display_name: "Data",
        budget_usd: 8000,
        spent_usd: 6500,
        percent: 81.3,
        status: "warning",
        held_tasks: 0,
        overrides: [
          {
            id: "o1",
            team_id: "t-data",
            month: null,
            scope: "permanent",
            amount_usd: 8000,
            raised_by: "lead@acme.test",
            reason: "bigger roadmap",
            created_at: "2026-10-02T00:00:00Z",
          },
        ],
        can_raise: false,
      },
    ],
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("BudgetsPage", () => {
  it("shows spend and status per team and who changed a budget", async () => {
    mockFetch(() => jsonResponse(overview(true)));
    renderPage(<BudgetsPage />);

    const web = (await screen.findByText("Web")).closest("tr") as HTMLElement;
    expect(within(web).getByText("Budget reached")).toBeInTheDocument();
    expect(within(web).getByText("3 tasks waiting")).toBeInTheDocument();
    expect(within(web).getByText("100.2%")).toBeInTheDocument();
    const data = screen.getByText("Data").closest("tr") as HTMLElement;
    expect(within(data).getByText("Near budget")).toBeInTheDocument();
    expect(within(data).getByText(/Set by lead@acme.test \(permanent\)/)).toBeInTheDocument();
    expect(within(data).queryByRole("button", { name: "Change budget" })).not.toBeInTheDocument();
    expect(screen.getByText(/came from calls without a team/)).toBeInTheDocument();
  });

  it("raises a budget for this month with a reason", async () => {
    const calls: { url: string; method: string; body?: string }[] = [];
    mockFetch((url, init) => {
      const method = (init?.method || "GET").toUpperCase();
      calls.push({ url, method, body: init?.body as string | undefined });
      if (url.includes("/raise")) return jsonResponse({ override_id: "o2" });
      return jsonResponse(overview(true));
    });
    renderPage(<BudgetsPage />);

    const web = (await screen.findByText("Web")).closest("tr") as HTMLElement;
    await userEvent.click(within(web).getByRole("button", { name: "Change budget" }));
    const dialog = screen.getByRole("region", { name: "Raise budget" });
    const save = within(dialog).getByRole("button", { name: "Save budget" });
    expect(save).toBeDisabled();
    const amount = within(dialog).getByLabelText("Monthly budget");
    await userEvent.clear(amount);
    await userEvent.type(amount, "7500");
    await userEvent.type(within(dialog).getByLabelText("Reason"), "launch week");
    await userEvent.click(save);

    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/admin/budgets/t-web/raise"))).toBe(true));
    const raise = calls.find((c) => c.url.endsWith("/raise"));
    expect(raise?.method).toBe("POST");
    expect(JSON.parse(String(raise?.body))).toEqual({ amount_usd: 7500, scope: "month", reason: "launch week" });
    await waitFor(() => expect(calls.filter((c) => c.url.endsWith("/admin/budgets")).length).toBe(2));
  });

  it("stops managers above their limit", async () => {
    mockFetch(() => jsonResponse(overview(false)));
    renderPage(<BudgetsPage />);

    const web = (await screen.findByText("Web")).closest("tr") as HTMLElement;
    await userEvent.click(within(web).getByRole("button", { name: "Change budget" }));
    const dialog = screen.getByRole("region", { name: "Raise budget" });
    expect(within(dialog).getByText(/Managers can set up to 10,000.00 USD/)).toBeInTheDocument();
    const amount = within(dialog).getByLabelText("Monthly budget");
    await userEvent.clear(amount);
    await userEvent.type(amount, "12000");
    await userEvent.type(within(dialog).getByLabelText("Reason"), "more");
    expect(within(dialog).getByText("Above the manager limit; ask an admin.")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Save budget" })).toBeDisabled();
    expect(screen.queryByText(/came from calls without a team/)).not.toBeInTheDocument();
  });
});
