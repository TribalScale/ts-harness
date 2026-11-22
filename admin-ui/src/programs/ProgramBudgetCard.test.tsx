import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ProgramOwnBudget } from "../api/types";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { ProgramBudgetCard } from "./ProgramBudgetCard";

function budget(overrides: Partial<ProgramOwnBudget> = {}): ProgramOwnBudget {
  return {
    budget_usd: 100,
    source: "default",
    spent_usd: 85,
    spent_month_usd: 40,
    percent: 85,
    status: "warning",
    action: "downgrade",
    currency: "USD",
    warn_at_percent: 80,
    held_jobs: 0,
    set_by: null,
    set_at: null,
    can_set: true,
    set_limit: null,
    ...overrides,
  };
}

beforeEach(() => {
  HTMLDialogElement.prototype.showModal = vi.fn(function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  });
  HTMLDialogElement.prototype.close = vi.fn(function close(this: HTMLDialogElement) {
    this.removeAttribute("open");
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ProgramBudgetCard", () => {
  it("shows spend against the budget and what happens at 100%", () => {
    renderPage(<ProgramBudgetCard programId="p-1" budget={budget()} onSaved={() => undefined} />);
    const card = screen.getByRole("region", { name: "Program LLM budget" });
    expect(card).toHaveTextContent("Spent 85.00 USD of 100.00 USD");
    expect(card).toHaveTextContent("40.00 USD this month.");
    expect(card).toHaveTextContent("Default for every program.");
    expect(within(card).getByText("Near budget")).toBeInTheDocument();
    expect(card).toHaveTextContent("at 100% revisions, complex first runs and pre-push reviews use the default coding model");
  });

  it("says when coding waits for a raise", () => {
    renderPage(
      <ProgramBudgetCard
        programId="p-1"
        budget={budget({ status: "reached", percent: 104, spent_usd: 104, action: "stop", held_jobs: 2, source: "program", set_by: "lead@acme.test", can_set: false })}
        onSaved={() => undefined}
      />,
    );
    const card = screen.getByRole("region", { name: "Program LLM budget" });
    expect(card).toHaveTextContent("Budget reached: new coding jobs for its tickets wait (2 waiting).");
    expect(card).toHaveTextContent("Set for this program by lead@acme.test.");
    expect(within(card).queryByRole("button")).not.toBeInTheDocument();
  });

  it("shows spend without a budget", () => {
    renderPage(
      <ProgramBudgetCard programId="p-1" budget={budget({ status: "none", source: "none", budget_usd: 0, percent: null })} onSaved={() => undefined} />,
    );
    expect(screen.getByRole("region", { name: "Program LLM budget" })).toHaveTextContent("No budget. Spent 85.00 USD (40.00 USD this month).");
  });

  it("sets the program's budget with a reason", async () => {
    const calls: { url: string; method: string; body?: string }[] = [];
    const saved = { team: null, program: budget({ source: "program", budget_usd: 250, percent: 34, status: "ok" as const }) };
    mockFetch((url, init) => {
      calls.push({ url, method: (init?.method || "GET").toUpperCase(), body: init?.body as string | undefined });
      return jsonResponse(saved);
    });
    const onSaved = vi.fn();
    renderPage(<ProgramBudgetCard programId="p 1" budget={budget()} onSaved={onSaved} />);

    await userEvent.click(screen.getByRole("button", { name: "Set budget" }));
    const dialog = screen.getByRole("region", { name: "Set program budget" });
    const save = within(dialog).getByRole("button", { name: "Save budget" });
    expect(save).toBeDisabledControl();
    await userEvent.type(within(dialog).getByLabelText("Program budget"), "250");
    await userEvent.type(within(dialog).getByLabelText("Reason"), "revisions on the stronger model");
    await userEvent.click(save);

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(saved));
    expect(calls[0].url).toBe("/admin/budgets/programs/p%201");
    expect(calls[0].method).toBe("PUT");
    expect(JSON.parse(String(calls[0].body))).toEqual({ amount_usd: 250, reason: "revisions on the stronger model" });
  });

  it("goes back to the default with an empty amount and stops managers above their limit", async () => {
    const calls: { body?: string }[] = [];
    mockFetch((_url, init) => {
      calls.push({ body: init?.body as string | undefined });
      return jsonResponse({ team: null, program: budget() });
    });
    renderPage(
      <ProgramBudgetCard programId="p-1" budget={budget({ source: "program", set_limit: 1000 })} onSaved={() => undefined} />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Change budget" }));
    const dialog = screen.getByRole("region", { name: "Set program budget" });
    expect(within(dialog).getByText(/Managers can set up to 1,000.00 USD/)).toBeInTheDocument();
    const amount = within(dialog).getByLabelText("Program budget");
    expect(amount).toHaveValue(100);
    await userEvent.clear(amount);
    await userEvent.type(amount, "5000");
    await userEvent.type(within(dialog).getByLabelText("Reason"), "more");
    expect(within(dialog).getByText("Above the manager limit; ask an admin.")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Save budget" })).toBeDisabledControl();
    await userEvent.clear(amount);
    await userEvent.click(within(dialog).getByRole("button", { name: "Save budget" }));
    await waitFor(() => expect(calls.length).toBe(1));
    expect(JSON.parse(String(calls[0].body))).toEqual({ amount_usd: null, reason: "more" });
  });
});
