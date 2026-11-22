import { expect, test, type Page } from "@playwright/test";
import { mockApi } from "./mockApi";

function githubSection(page: Page) {
  // The settings page is itself a <section>; the mapping's own section is the innermost match.
  return page.locator("section", { has: page.locator("h4", { hasText: "Email → GitHub login" }) }).last();
}

test("adds a GitHub login mapping row, saves it, and removes it", async ({ page }) => {
  const api = await mockApi(page);
  await page.goto("/#/system/settings/identity");
  const section = githubSection(page);
  await expect(section).toContainText("bookshelf-lead");
  await expect(section).toContainText("Source: inherited");

  await section.getByLabel("GitHub email").fill("Dev@Bookshelf.example");
  await section.getByLabel("GitHub login").fill("bookshelf-dev");
  await section.getByRole("button", { name: "Add" }).click();
  const added = section.getByRole("row", { name: /dev@bookshelf\.example/ });
  await expect(added).toContainText("bookshelf-dev");
  await expect(section).toContainText("Source: overlay");

  await page.getByRole("button", { name: "Validate" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Validated" })).toBeVisible();
  const saved = api.callsTo("/admin/runtime-settings/drafts/draft-rs-1");
  expect(saved).toHaveLength(1);
  expect(saved[0].method).toBe("PUT");
  expect(saved[0].body.definition.identity.github_logins).toEqual({
    "lead@bookshelf.example": "bookshelf-lead",
    "dev@bookshelf.example": "bookshelf-dev",
  });

  await added.getByRole("button", { name: "Remove" }).click();
  await expect(section.getByRole("row", { name: /dev@bookshelf\.example/ })).toHaveCount(0);
  await page.getByRole("button", { name: "Validate" }).click();
  await expect.poll(() => api.callsTo("/admin/runtime-settings/drafts/draft-rs-1").length).toBe(2);
  const removed = api.callsTo("/admin/runtime-settings/drafts/draft-rs-1")[1];
  expect(removed.body.definition.identity.github_logins).toEqual({ "lead@bookshelf.example": "bookshelf-lead" });
});
