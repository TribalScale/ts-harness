import { useState } from "react";
import { fireEvent, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { renderPage } from "../test/render";
import { ConfirmDialog } from "./ConfirmDialog";
import { StatusPill } from "./StatusPill";
import { Tabs, tabButtonId, tabPanelId } from "./Tabs";

function installDialogMethods() {
  HTMLDialogElement.prototype.showModal = vi.fn(function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  });
  HTMLDialogElement.prototype.close = vi.fn(function close(this: HTMLDialogElement) {
    this.removeAttribute("open");
    this.dispatchEvent(new Event("close"));
  });
}

it("renders semantic status text", () => {
  renderPage(<StatusPill status="unavailable" />);
  expect(screen.getByText("Unavailable")).toBeInTheDocument();
});

const DEMO_TABS = [
  { id: "overview", label: "Overview" },
  { id: "tasks", label: "Tasks" },
];

function ControlledTabs({ onChange }: { onChange?: (id: string) => void }) {
  const [active, setActive] = useState("overview");
  return (
    <>
      <Tabs
        idBase="demo"
        ariaLabel="Program detail"
        tabs={DEMO_TABS}
        active={active}
        onChange={(id) => {
          onChange?.(id);
          setActive(id);
        }}
      />
      <div
        role="tabpanel"
        id={tabPanelId("demo", active)}
        aria-labelledby={tabButtonId("demo", active)}
      >
        {active}
      </div>
    </>
  );
}

it("changes controlled tabs", async () => {
  const onChange = vi.fn();
  renderPage(
    <Tabs
      idBase="demo"
      ariaLabel="Program detail"
      tabs={DEMO_TABS}
      active="overview"
      onChange={onChange}
    />
  );
  await userEvent.click(screen.getByRole("tab", { name: "Tasks" }));
  expect(onChange).toHaveBeenCalledWith("tasks");
});

it("supports arrow-key tab navigation", async () => {
  const onChange = vi.fn();
  renderPage(
    <Tabs
      idBase="demo"
      ariaLabel="Program detail"
      tabs={DEMO_TABS}
      active="overview"
      onChange={onChange}
    />
  );
  screen.getByRole("tab", { name: "Overview" }).focus();
  const event = new KeyboardEvent("keydown", {
    key: "ArrowRight",
    bubbles: true,
    cancelable: true,
  });
  fireEvent(screen.getByRole("tab", { name: "Overview" }), event);
  expect(event.defaultPrevented).toBe(true);
  expect(onChange).toHaveBeenCalledWith("tasks");
});

it("gives every tab a stable id wired to its panel", () => {
  renderPage(<ControlledTabs />);
  const overview = screen.getByRole("tab", { name: "Overview" });
  const tasks = screen.getByRole("tab", { name: "Tasks" });

  expect(overview).toHaveAttribute("id", "demo-tab-overview");
  expect(overview).toHaveAttribute("aria-controls", "demo-panel-overview");
  expect(tasks).toHaveAttribute("id", "demo-tab-tasks");
  expect(tasks).toHaveAttribute("aria-controls", "demo-panel-tasks");
  expect(screen.getByRole("tabpanel")).toHaveAttribute("aria-labelledby", "demo-tab-overview");
});

it("moves DOM focus with the selection on arrow-key navigation", async () => {
  renderPage(<ControlledTabs />);
  screen.getByRole("tab", { name: "Overview" }).focus();

  await userEvent.keyboard("{ArrowRight}");
  expect(screen.getByRole("tab", { name: "Tasks" })).toHaveFocus();
  expect(screen.getByRole("tab", { name: "Tasks" })).toHaveAttribute("aria-selected", "true");

  await userEvent.keyboard("{ArrowLeft}");
  expect(screen.getByRole("tab", { name: "Overview" })).toHaveFocus();
  expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
});

it("confirms destructive actions", async () => {
  installDialogMethods();
  const onConfirm = vi.fn();
  renderPage(
    <ConfirmDialog
      open
      title="Cancel program?"
      description="This stops future phases."
      confirmLabel="Cancel program"
      danger
      onConfirm={onConfirm}
      onCancel={() => undefined}
    />
  );
  await userEvent.click(screen.getByRole("button", { name: "Cancel program" }));
  expect(onConfirm).toHaveBeenCalled();
});

it("calls cancel exactly once when canceling the dialog", async () => {
  installDialogMethods();
  const onCancel = vi.fn();
  function ControlledDialog() {
    const [open, setOpen] = useState(true);
    return (
      <ConfirmDialog
        open={open}
        title="Cancel program?"
        description="This stops future phases."
        confirmLabel="Cancel program"
        onConfirm={() => undefined}
        onCancel={() => {
          onCancel();
          setOpen(false);
        }}
      />
    );
  }

  renderPage(<ControlledDialog />);
  await userEvent.click(screen.getByRole("button", { name: "Keep working" }));
  expect(onCancel).toHaveBeenCalledTimes(1);
});

it("labels each dialog instance with its own generated ids", () => {
  installDialogMethods();
  renderPage(
    <>
      <ConfirmDialog
        open
        title="Cancel program?"
        description="This stops future phases."
        confirmLabel="Cancel program"
        onConfirm={() => undefined}
        onCancel={() => undefined}
      />
      <ConfirmDialog
        open
        title="Delete cron job?"
        description="This removes the schedule."
        confirmLabel="Delete job"
        onConfirm={() => undefined}
        onCancel={() => undefined}
      />
    </>
  );

  const [first, second] = screen.getAllByRole("dialog");
  const firstLabel = first.getAttribute("aria-labelledby");
  const secondLabel = second.getAttribute("aria-labelledby");
  expect(firstLabel).toBeTruthy();
  expect(secondLabel).toBeTruthy();
  expect(firstLabel).not.toBe(secondLabel);
  expect(first.getAttribute("aria-describedby")).not.toBe(
    second.getAttribute("aria-describedby")
  );
  expect(document.getElementById(String(firstLabel))).toHaveTextContent("Cancel program?");
  expect(document.getElementById(String(secondLabel))).toHaveTextContent("Delete cron job?");
});

it("does not cancel when confirm closes the dialog", async () => {
  installDialogMethods();
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  function ControlledDialog() {
    const [open, setOpen] = useState(true);
    return (
      <ConfirmDialog
        open={open}
        title="Cancel program?"
        description="This stops future phases."
        confirmLabel="Cancel program"
        onConfirm={() => {
          onConfirm();
          setOpen(false);
        }}
        onCancel={onCancel}
      />
    );
  }

  renderPage(<ControlledDialog />);
  await userEvent.click(screen.getByRole("button", { name: "Cancel program" }));
  expect(onConfirm).toHaveBeenCalledTimes(1);
  expect(onCancel).not.toHaveBeenCalled();
});
