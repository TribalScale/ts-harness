import { expect, it } from "vitest";
import { jsonResponse, mockFetch } from "../test/render";
import { apiFetch } from "./client";

it("sends cookies and no Authorization header", async () => {
  const inits: (RequestInit | undefined)[] = [];
  mockFetch((_url, init) => {
    inits.push(init);
    return jsonResponse({ items: [] });
  });

  await apiFetch("/admin/inbox");

  expect(inits[0]?.credentials).toBe("include");
  expect(new Headers(inits[0]?.headers).has("Authorization")).toBe(false);
});
