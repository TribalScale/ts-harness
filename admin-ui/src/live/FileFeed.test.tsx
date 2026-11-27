import { screen, within } from "@testing-library/react";
import { expect, it } from "vitest";
import { parseHash } from "../App";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { FileEventPage } from "./FileEventPage";
import { FileFeed } from "./FileFeed";

const edited = {
  id: 7,
  job_id: "job-1",
  at: "2026-11-27T01:05:00Z",
  kind: "edited",
  path: "src/components/Sidebar.tsx",
  file_count: 1,
  additions: 34,
  deletions: 8,
  step: "coding",
  actor: "agent",
  truncated: false,
  binary: false,
  masked: false,
  work_order_id: "PROJ-48",
  program_id: "prog-1",
};

const bulk = {
  ...edited,
  id: 8,
  kind: "bulk",
  bulk_kind: "edited",
  path: "docs",
  paths: ["docs/a.md", "docs/b.md"],
  file_count: 68,
  additions: 120,
  deletions: 90,
  step: "verification · fixing test failures (pass 1)",
  actor: "harness",
};

it("lists a run's file changes newest first, each linking to its detail", async () => {
  mockFetch((url) => {
    if (url.startsWith("/admin/coding-jobs/job-1/files")) return jsonResponse({ events: [edited, bulk], next_after: 8 });
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<FileFeed jobId="job-1" title="File changes" />);

  const feed = await screen.findByRole("region", { name: "File changes" });
  const links = await within(feed).findAllByRole("link");
  expect(links[0]).toHaveAttribute("href", "#/live/files/8");
  expect(links[0]).toHaveTextContent("edited 68 files in docs");
  expect(links[0]).toHaveTextContent("by the harness");
  expect(links[1]).toHaveAttribute("href", "#/live/files/7");
  expect(links[1]).toHaveTextContent("src/components/Sidebar.tsx");
  expect(links[1]).toHaveTextContent("+34");
});

it("the global feed shows the ticket of each change", async () => {
  mockFetch((url) => {
    if (url.startsWith("/admin/live/files")) return jsonResponse({ events: [edited], next_after: 7 });
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<FileFeed all title="File changes in running tickets" />);

  const feed = await screen.findByRole("region", { name: "File changes in running tickets" });
  expect(await within(feed).findByText("PROJ-48")).toBeInTheDocument();
});

it("a change's page shows the diff, its history in the run and checks that failed on it", async () => {
  mockFetch((url) => {
    if (url === "/admin/live/files/7")
      return jsonResponse({
        event: { ...edited, excerpt: "--- a/src/x\n+++ b/src/x\n@@ -1 +1 @@\n-old\n+new\n", masked: true },
        history: [edited, { ...edited, id: 9, additions: 2, deletions: 2, step: "verification" }],
        failed_checks: [{ check: "format", command: "npm run format:check", output_tail: "[warn] src/components/Sidebar.tsx" }],
        pr_url: "https://github.com/org/web/pull/3",
      });
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<FileEventPage eventId="7" />);

  const change = await screen.findByLabelText("Change");
  expect(within(change).getByText("+new")).toHaveClass("diff-add");
  expect(within(change).getByText("-old")).toHaveClass("diff-del");
  expect(screen.getByText(/shown as \*\*\*/)).toBeInTheDocument();
  expect(screen.getByText("format")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "PROJ-48" })).toHaveAttribute("href", "#/programs/prog-1");
  expect(screen.getByRole("link", { name: /pull\/3/ })).toBeInTheDocument();
  const history = screen.getByRole("heading", { name: "This file in the run" }).closest("section") as HTMLElement;
  expect(within(history).getAllByRole("link")).toHaveLength(2);
});

it("routes the Files tab and a change's page", () => {
  expect(parseHash("#/live/files")).toMatchObject({ nav: "live", liveTab: "files", fileEventId: undefined });
  expect(parseHash("#/live/files/42")).toMatchObject({ nav: "live", liveTab: "files", fileEventId: "42" });
  expect(parseHash("#/live/history")).toMatchObject({ nav: "live", liveTab: "history" });
});
