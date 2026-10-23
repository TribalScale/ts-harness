import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import type { ProgramAccessList } from "../api/types";
import { jsonResponse, mockFetch } from "../test/render";
import { ShareDialog } from "./ShareDialog";

const base = "/admin/programs/p-1";

function access(extra: Partial<ProgramAccessList> = {}): ProgramAccessList {
  return {
    program_id: "p-1",
    visibility: "private",
    owner: { subject: "auth0|alice", email: "alice@example.com", display_name: "Alice" },
    grants: [{ principal_type: "team", principal_id: "payments", level: "write" }],
    access_level: "admin",
    ...extra,
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

it("lists the owner and grants, adds a person from the directory and removes a team", async () => {
  const calls: string[] = [];
  let current = access();
  mockFetch((url, init) => {
    const method = (init?.method || "GET").toUpperCase();
    calls.push(`${method} ${url}${init?.body ? ` ${init.body}` : ""}`);
    if (url === `${base}/access` && method === "GET") return jsonResponse(current);
    if (url.startsWith("/admin/directory/search")) {
      return jsonResponse({
        users: [{ subject: "auth0|bob", email: "bob@example.com", display_name: "Bob" }],
        teams: [],
      });
    }
    if (url === `${base}/access` && method === "POST") {
      current = access({
        grants: [
          ...current.grants,
          { principal_type: "user", principal_id: "auth0|bob", level: "approve", display_name: "Bob" },
        ],
      });
      return jsonResponse(current);
    }
    if (url === `${base}/access/team/payments` && method === "DELETE") {
      current = access({ grants: current.grants.filter((g) => g.principal_id !== "payments") });
      return jsonResponse(current);
    }
    throw new Error(`Unexpected ${method} ${url}`);
  });

  render(<ShareDialog open programId="p-1" onClose={() => undefined} />);

  const grants = await screen.findByRole("list", { name: "Program access" });
  expect(await within(grants).findByText("Alice")).toBeInTheDocument();
  expect(within(grants).getByText("Owner")).toBeInTheDocument();
  expect(within(grants).getByText("Team payments")).toBeInTheDocument();
  expect(within(grants).getByText("Can contribute")).toBeInTheDocument();

  await userEvent.type(screen.getByRole("textbox", { name: "Search people and teams" }), "bo");
  await userEvent.click(await screen.findByRole("option", { name: /Bob/ }));
  await userEvent.selectOptions(screen.getByRole("combobox", { name: "Access level" }), "approve");
  await userEvent.click(screen.getByRole("button", { name: "Add" }));

  expect(await within(grants).findByText("Bob")).toBeInTheDocument();
  expect(calls).toContain(
    `POST ${base}/access {"principal_type":"user","principal_id":"auth0|bob","level":"approve"}`
  );

  await userEvent.click(screen.getByRole("button", { name: "Remove payments" }));
  await waitFor(() => expect(within(grants).queryByText("Team payments")).not.toBeInTheDocument());
});

it("switches visibility and reports the change", async () => {
  const changes: string[] = [];
  mockFetch((url, init) => {
    const method = (init?.method || "GET").toUpperCase();
    if (url === `${base}/access` && method === "GET") return jsonResponse(access());
    if (url === `${base}/visibility` && method === "PATCH") {
      expect(init?.body).toBe('{"visibility":"public"}');
      return jsonResponse(access({ visibility: "public" }));
    }
    throw new Error(`Unexpected ${method} ${url}`);
  });

  render(
    <ShareDialog open programId="p-1" onClose={() => undefined} onChanged={(next) => changes.push(String(next.visibility))} />
  );

  const publicOption = await screen.findByRole("radio", { name: /Public/ });
  await waitFor(() => expect(publicOption).toBeEnabled());
  await userEvent.click(publicOption);
  await waitFor(() => expect(changes).toEqual(["public"]));
  expect(publicOption).toBeChecked();
});

it("is read-only for people who cannot manage the program", async () => {
  mockFetch(() => jsonResponse(access({ access_level: "read" })));

  render(<ShareDialog open programId="p-1" onClose={() => undefined} />);

  expect(await screen.findByText("Team payments")).toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: "Search people and teams" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Remove/ })).not.toBeInTheDocument();
  expect(screen.getByRole("radio", { name: /Public/ })).toBeDisabled();
});
