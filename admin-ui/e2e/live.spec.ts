import { expect, test } from "@playwright/test";
import { mockApi } from "./mockApi";

test.beforeEach(async ({ page }) => {
  await mockApi(page);
});

test("a live card shows the current run's phase breakdown", async ({ page }) => {
  await page.goto("/#/live");
  const card = page.locator("li.live-card", { hasText: "DEMO-14" });
  await expect(card).toBeVisible();
  await expect(card).toContainText("Agent working");
  await expect(card).toContainText("Bookshelf: reading list and loans");
  const run = card.getByRole("region", { name: "Current coding run" });
  await expect(run).toContainText("Revision 1");
  await expect(run).toContainText("on Opus");
  const phases = run.locator("li.story-phase");
  await expect(phases).toHaveCount(5);
  await expect(run.locator("li.story-phase-done")).toHaveCount(2);
  await expect(run.locator("li.story-phase-active")).toContainText("Running tests");
  await expect(run.locator("li.story-phase-active")).toContainText("npm test");
  await expect(run.locator("li.story-phase", { hasText: "Coding" })).toContainText("5 m 55 s");
});

test("Details opens the ticket story and Show more expands the findings", async ({ page }) => {
  await page.goto("/#/live");
  const card = page.locator("li.live-card", { hasText: "DEMO-14" });
  const story = page.waitForRequest((req) => req.url().includes("/admin/programs/prog-bookshelf-0001/tickets/wf-demo-14/story"));
  await card.getByRole("button", { name: "Details" }).click();
  await story;
  await expect(card.getByRole("button", { name: "Hide details" })).toHaveAttribute("aria-expanded", "true");
  await expect(card.getByLabel("Where the time went")).toContainText("coding cost $1.84");
  const events = card.getByRole("list", { name: "What happened" });
  await expect(events).toContainText("First attempt");
  await expect(events).toContainText("Code review asked for changes");
  const findings = events.locator("li.story-finding");
  await expect(findings).toHaveCount(2);
  await expect(events).toContainText("Plus 3 non-blocking findings on the PR.");
  await events.getByRole("button", { name: "Show 2 more" }).click();
  await expect(findings).toHaveCount(4);
  await expect(events).toContainText("Reminder copy says 'book' for every item type.");
  await expect(events.getByRole("button", { name: /Show \d+ more/ })).toHaveCount(0);
});
