import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { expect, it } from "vitest";
import { GateReplyCard } from "./InboxPage";

function Harness({ reply }: { reply?: { kind?: string; options?: { id: string; default_branch?: string }[] } }) {
  const [value, setValue] = useState("");
  return (
    <>
      <GateReplyCard reply={reply} value={value} onChange={setValue} />
      <output data-testid="value">{value}</output>
    </>
  );
}

it("offers the allowlisted repositories on a repo pick", async () => {
  render(<Harness reply={{ kind: "repo", options: [{ id: "org/api", default_branch: "dev" }, { id: "org/web", default_branch: "dev" }] }} />);
  await userEvent.selectOptions(screen.getByLabelText("Repository"), "org/web");
  expect(screen.getByTestId("value").textContent).toBe("org/web");
  expect(screen.getByRole("option", { name: "org/api (dev)" })).toBeTruthy();
});

it("takes a typed repository when none is allowlisted", async () => {
  render(<Harness reply={{ kind: "repo", options: [] }} />);
  await userEvent.type(screen.getByLabelText("Repository"), "org/api");
  expect(screen.getByTestId("value").textContent).toBe("org/api");
});

it("asks for answers on an intake gate and for an optional reply elsewhere", () => {
  const { unmount } = render(<Harness reply={{ kind: "answer" }} />);
  expect(screen.getByLabelText("Your answers")).toBeTruthy();
  unmount();
  render(<Harness />);
  expect(screen.getByLabelText("Reply")).toBeTruthy();
});
