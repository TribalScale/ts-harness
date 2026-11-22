import { expect, test } from "@playwright/test";
import { gateItem, proposalItem, stuckItem } from "./fixtures";
import { mockApi } from "./mockApi";

function row(page: import("@playwright/test").Page, title: string) {
  return page.locator("button.row-card", { hasText: title });
}

test.beforeEach(async ({ page }) => {
  await mockApi(page);
});

test("lists every item waiting on the operator", async ({ page }) => {
  await page.goto("/#/inbox");
  await expect(row(page, gateItem.title)).toBeVisible();
  await expect(row(page, stuckItem.title)).toBeVisible();
  await expect(row(page, proposalItem.title)).toBeVisible();
  await expect(row(page, stuckItem.title)).toContainText("Revision limit reached");
});

test("opens a gate and approves it", async ({ page }) => {
  await page.goto("/#/inbox");
  await row(page, gateItem.title).click();
  await expect(page).toHaveURL(/#\/inbox\/workflow_gate\/task-gate-prd-1/);
  const actions = page.getByLabel("Item actions");
  const approve = page.waitForRequest((req) => req.method() === "POST" && req.url().endsWith("/admin/workflows/approve"));
  await actions.getByRole("button", { name: "Approve" }).click();
  expect((await approve).postDataJSON()).toEqual({ id: "task-gate-prd-1" });
  await expect(row(page, gateItem.title)).toHaveCount(0);
});

test("opens a stuck step and revises it with guidance", async ({ page }) => {
  await page.goto("/#/inbox");
  await row(page, stuckItem.title).click();
  await expect(
    page.getByText("The loan due-date calculation ignores the library's time zone.", { exact: true }),
  ).toBeVisible();
  await page
    .getByPlaceholder("What should the next attempt do differently?")
    .fill("Use the library's configured time zone for due dates and add a DST test.");
  const decide = page.waitForRequest((req) => req.method() === "POST" && req.url().endsWith("/admin/workflows/decide"));
  await page.getByRole("button", { name: "Revise again" }).click();
  expect((await decide).postDataJSON()).toEqual({
    workflow_id: "wf-demo-12",
    task_id: "task-finalize-7",
    action: "revise",
    guidance: "Use the library's configured time zone for due dates and add a DST test.",
  });
  await expect(row(page, stuckItem.title)).toHaveCount(0);
});

test("cancels a chat proposal", async ({ page }) => {
  await page.goto("/#/inbox");
  await row(page, proposalItem.title).click();
  const cancel = page.waitForRequest((req) => req.method() === "POST" && req.url().endsWith("/admin/proposals/cancel"));
  await page.getByRole("button", { name: "Cancel proposal" }).click();
  expect((await cancel).postDataJSON()).toEqual({ session_id: "sess-web-77" });
  await expect(page.getByText("Proposal cancelled")).toBeVisible();
  await expect(row(page, proposalItem.title)).toHaveCount(0);
});
