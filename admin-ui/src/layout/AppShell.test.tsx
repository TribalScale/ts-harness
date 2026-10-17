import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderPage } from "../test/render";
import { AppShell } from "./AppShell";

function installMediaQuery(desktop: boolean) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: query.includes("min-width: 1024px") ? desktop : !desktop,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
}

describe("AppShell on desktop", () => {
  beforeEach(() => installMediaQuery(true));

  it("renders one labeled navigation item per row and marks the current page", async () => {
    const onNavigate = vi.fn();
    renderPage(<AppShell active="programs" onNavigate={onNavigate}>content</AppShell>);
    const current = screen.getByRole("button", { name: "Programs" });
    expect(current).toHaveAttribute("aria-current", "page");
    await userEvent.click(screen.getByRole("button", { name: "Chat" }));
    expect(onNavigate).toHaveBeenCalledWith("chat");
  });

  it("keeps the compact labeled sidebar and operator footer without a menu button", () => {
    renderPage(<AppShell active="inbox" onNavigate={() => undefined}>content</AppShell>);
    const nav = screen.getByRole("navigation", { name: "Primary" });
    for (const label of ["Inbox", "Chat", "Programs", "Workflows", "System"]) {
      expect(within(nav).getByRole("button", { name: label })).toBeVisible();
    }
    expect(screen.getByText("operator")).toBeVisible();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Menu" })).not.toBeInTheDocument();
  });
});

describe("AppShell on phone widths", () => {
  beforeEach(() => installMediaQuery(false));

  it("keeps navigation collapsed behind a labeled drawer until the menu is opened", () => {
    renderPage(<AppShell active="inbox" onNavigate={() => undefined}>content</AppShell>);

    expect(screen.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument();
    const menu = screen.getByRole("button", { name: "Menu" });
    expect(menu).toBeVisible();
    expect(menu).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("content")).toBeVisible();
  });

  it("opens a labeled drawer with one link per row and closes it after navigating", async () => {
    const onNavigate = vi.fn();
    renderPage(<AppShell active="inbox" onNavigate={onNavigate}>content</AppShell>);

    await userEvent.click(screen.getByRole("button", { name: "Menu" }));

    const nav = screen.getByRole("navigation", { name: "Primary" });
    for (const label of ["Inbox", "Chat", "Programs", "Workflows", "System"]) {
      expect(within(nav).getByRole("button", { name: label })).toBeVisible();
    }
    expect(screen.getByRole("button", { name: "Menu" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: "Sign out" })).toBeVisible();

    await userEvent.keyboard("{Escape}");
    await waitFor(() =>
      expect(screen.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument()
    );

    await userEvent.click(screen.getByRole("button", { name: "Menu" }));
    expect(screen.getByRole("navigation", { name: "Primary" })).toBeInTheDocument();
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    await waitFor(() =>
      expect(screen.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument()
    );

    await userEvent.click(screen.getByRole("button", { name: "Menu" }));
    await userEvent.click(within(screen.getByRole("navigation", { name: "Primary" })).getByRole("button", { name: "Chat" }));

    expect(onNavigate).toHaveBeenCalledWith("chat");
    await waitFor(() =>
      expect(screen.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument()
    );
  });
});
