import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { ProgramDefinitionLibrary } from "./ProgramDefinitionLibrary";

it("loads program definitions and supports new, view, and clone actions", async () => {
  const onSelect = vi.fn();
  const onNew = vi.fn();
  const onClone = vi.fn();
  mockFetch((url) => {
    if (url === "/admin/program-definitions") {
      return jsonResponse({
        definitions: [
          {
            program_type: "software_delivery",
            definition_version: "1.0.0",
            display_name: "Software delivery",
            status: "built_in",
            read_only: true,
          },
        ],
      });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(
    <ProgramDefinitionLibrary
      selected={{ kind: "library" }}
      onSelect={onSelect}
      onNew={onNew}
      onClone={onClone}
    />
  );

  expect(await screen.findByText("Software delivery")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /new program/i }));
  expect(onNew).toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: /^view$/i }));
  expect(onSelect).toHaveBeenCalledWith({
    kind: "program",
    programType: "software_delivery",
  });
  await userEvent.click(screen.getByRole("button", { name: /^clone software_delivery$/i }));
  expect(onClone).toHaveBeenCalledWith(
    expect.objectContaining({ program_type: "software_delivery", status: "built_in" })
  );
});

it("opens the agent builder from the library", async () => {
  const onBuildWithAgent = vi.fn();
  mockFetch(() => jsonResponse({ definitions: [] }));
  renderPage(
    <ProgramDefinitionLibrary
      selected={{ kind: "library" }}
      onSelect={() => undefined}
      onNew={() => undefined}
      onBuildWithAgent={onBuildWithAgent}
    />
  );
  await userEvent.click(await screen.findByRole("button", { name: /build with agent/i }));
  expect(onBuildWithAgent).toHaveBeenCalled();
});

it("shows empty and error states", async () => {
  mockFetch((url) => {
    if (url === "/admin/program-definitions") return jsonResponse({ definitions: [] });
    throw new Error(`Unexpected URL ${url}`);
  });

  const { unmount } = renderPage(
    <ProgramDefinitionLibrary selected={{ kind: "library" }} onSelect={() => undefined} />
  );
  await waitFor(() => expect(screen.getByText(/no program definitions/i)).toBeInTheDocument());
  unmount();

  mockFetch(() => jsonResponse({ error: "boom" }, 500));
  renderPage(
    <ProgramDefinitionLibrary selected={{ kind: "library" }} onSelect={() => undefined} />
  );
  expect(await screen.findByText(/could not load program definitions/i)).toBeInTheDocument();
});
