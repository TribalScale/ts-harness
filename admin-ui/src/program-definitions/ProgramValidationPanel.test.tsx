import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { renderPage } from "../test/render";
import {
  isActionableProgramValidationPath,
  ProgramValidationPanel,
} from "./ProgramValidationPanel";

it("marks node JSON pointer and JSONPath entries as actionable", () => {
  expect(isActionableProgramValidationPath("/nodes/0/id", 2)).toBe(true);
  expect(isActionableProgramValidationPath("$.nodes[1].default_workflow_type", 2)).toBe(true);
  expect(isActionableProgramValidationPath("$.program_type", 2)).toBe(false);
  expect(isActionableProgramValidationPath("/nodes/9/id", 2)).toBe(false);
});

it("renders valid state and focuses actionable issues", async () => {
  const onFocusPath = vi.fn();
  const { rerender } = renderPage(
    <ProgramValidationPanel valid issues={[]} onFocusPath={onFocusPath} />
  );
  expect(screen.getByText(/definition is valid/i)).toBeInTheDocument();

  rerender(
    <ProgramValidationPanel
      valid
      issues={[
        {
          severity: "warning",
          code: "unconfirmed_output_artifact",
          path: "$.nodes[0].produces_artifacts",
          message: "Adapter cannot confirm artifact.",
        },
      ]}
      onFocusPath={onFocusPath}
    />
  );
  expect(screen.getByText(/definition is valid with 1 warning/i)).toBeInTheDocument();
  expect(screen.getByText(/warning ·/i)).toBeInTheDocument();

  rerender(
    <ProgramValidationPanel
      valid={false}
      issues={[
        {
          severity: "error",
          code: "missing_workflow",
          path: "$.nodes[0].default_workflow_type",
          message: "Workflow required.",
        },
        {
          severity: "error",
          code: "missing_program_type",
          path: "$.program_type",
          message: "Program type required.",
        },
      ]}
      onFocusPath={onFocusPath}
    />
  );

  expect(screen.getByText("Workflow required.")).toBeInTheDocument();
  expect(screen.getByText("Program type required.")).toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: /focus/i })).toHaveLength(1);
  await userEvent.click(screen.getByRole("button", { name: /focus/i }));
  expect(onFocusPath).toHaveBeenCalledWith("$.nodes[0].default_workflow_type");
});
