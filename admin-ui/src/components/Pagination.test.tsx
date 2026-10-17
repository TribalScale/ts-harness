import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { renderPage } from "../test/render";
import { DEFAULT_PAGE_SIZE, pageForIndex, paginate, Pagination } from "./Pagination";

it("slices a page and clamps out of range", () => {
  const items = Array.from({ length: 12 }, (_, index) => index + 1);
  expect(paginate(items, 1)).toMatchObject({ items: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10], page: 1, pageCount: 2, start: 1, end: 10 });
  expect(paginate(items, 2)).toMatchObject({ items: [11, 12], page: 2, start: 11, end: 12 });
  expect(paginate(items, 99).page).toBe(2);
  expect(paginate(items, 0).page).toBe(1);
});

it("maps an index onto its page", () => {
  expect(pageForIndex(0)).toBe(1);
  expect(pageForIndex(DEFAULT_PAGE_SIZE)).toBe(2);
  expect(pageForIndex(-1)).toBe(1);
});

it("hides when everything fits on one page", () => {
  renderPage(
    <Pagination page={1} pageCount={1} total={3} start={1} end={3} onPageChange={() => undefined} />
  );
  expect(screen.queryByRole("navigation", { name: "Pagination" })).not.toBeInTheDocument();
});

it("moves forward and back", async () => {
  const onPageChange = vi.fn();
  renderPage(
    <Pagination
      page={1}
      pageCount={3}
      total={25}
      start={1}
      end={10}
      onPageChange={onPageChange}
      itemLabel="skills"
    />
  );
  expect(screen.getByText("Showing 1–10 of 25 skills")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: "Next" }));
  expect(onPageChange).toHaveBeenCalledWith(2);
});
