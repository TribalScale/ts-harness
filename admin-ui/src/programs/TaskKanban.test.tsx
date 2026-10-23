import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { renderPage } from "../test/render";
import { TaskKanban } from "./TaskKanban";

beforeEach(() => {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: query.includes("max-width"),
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
});

it("hides approval actions on a cancelled task", async () => {
  renderPage(
    <TaskKanban
      tasks={[
        {
          id: "done-task",
          step_name: "approve_prd",
          status: "cancelled",
          approval_required: true,
          approval_status: "pending",
        },
      ]}
    />
  );
  await userEvent.click(screen.getByRole("button", { name: /done \(1\)/i }));
  expect(screen.getByText("approve_prd")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
});

it("hides approval actions when the program is cancelled", () => {
  renderPage(
    <TaskKanban
      programStatus="cancelled"
      tasks={[
        {
          id: "blocked-task",
          step_name: "approve_prd",
          status: "blocked",
          approval_required: true,
          approval_status: "pending",
        },
      ]}
    />
  );
  expect(screen.getByText("approve_prd")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
});

it("shows approval actions for a pending blocked task", () => {
  renderPage(
    <TaskKanban
      tasks={[
        {
          id: "blocked-task",
          step_name: "approve_prd",
          status: "blocked",
          approval_required: true,
          approval_status: "pending",
        },
      ]}
    />
  );
  expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Reject" })).toBeInTheDocument();
});

it("uses mobile filters for responsive task columns", async () => {
  renderPage(
    <TaskKanban
      tasks={[
        { id: "running-task", step_name: "Implement feature", status: "running" },
        {
          id: "blocked-task",
          step_name: "Review gate",
          status: "blocked",
          approval_required: true,
          approval_status: "pending",
        },
      ]}
    />
  );

  expect(screen.getByRole("button", { name: /blocked \/ waiting \(1\)/i })).toHaveAttribute(
    "aria-pressed",
    "true"
  );
  expect(screen.getByRole("button", { name: /running \(1\)/i })).toHaveAttribute(
    "aria-pressed",
    "false"
  );
  expect(screen.getByText("Review gate")).toBeInTheDocument();
  expect(screen.queryByText("Implement feature")).not.toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: /running \(1\)/i }));

  expect(screen.getByText("Implement feature")).toBeInTheDocument();
  expect(screen.queryByText("Review gate")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /running \(1\)/i })).toHaveAttribute(
    "aria-pressed",
    "true"
  );
});
