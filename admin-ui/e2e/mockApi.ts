/**
 * A fake gateway for the browser tests: answers every API call the console makes with
 * fixture JSON via Playwright route interception. It keeps a little state (decided Inbox
 * items, the runtime-settings draft) so the page sees what a real gateway would return next,
 * and records every non-GET request for assertions.
 */
import type { Page, Route } from "@playwright/test";
import * as fx from "./fixtures";

const API_PREFIXES = ["/admin", "/healthz", "/session", "/intake", "/cron", "/workflow"];

export interface RecordedCall {
  method: string;
  path: string;
  body: any;
}

export interface MockApi {
  calls: RecordedCall[];
  /** Non-GET calls to *path*, oldest first. */
  callsTo(path: string): RecordedCall[];
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

export async function mockApi(page: Page): Promise<MockApi> {
  const calls: RecordedCall[] = [];
  let inbox = fx.inboxItems();
  const settings = fx.runtimeSettings();
  let draft = settings.draft!;
  let draftRevision = 1;

  await page.route(
    (url) => API_PREFIXES.some((prefix) => url.pathname === prefix || url.pathname.startsWith(`${prefix}/`)),
    async (route) => {
      const request = route.request();
      const method = request.method();
      const path = new URL(request.url()).pathname;
      let body: any = null;
      if (method !== "GET") {
        try {
          body = request.postDataJSON();
        } catch {
          body = request.postData();
        }
        calls.push({ method, path, body });
      }

      if (path === "/admin/auth/config" || path === "/admin/auth/session") return json(route, fx.authConfig);
      if (path === "/admin/inbox") return json(route, { items: inbox, total: inbox.length });
      if (path === "/admin/workflows/approve" || path === "/admin/workflows/reject") {
        inbox = inbox.filter((item) => item.id !== body?.id);
        return json(route, { status: "approved", task_id: body?.id });
      }
      if (path === "/admin/workflows/decide") {
        inbox = inbox.filter((item) => item.id !== body?.task_id);
        return json(route, { status: "ok", action: body?.action });
      }
      if (path === "/admin/proposals/cancel") {
        inbox = inbox.filter((item) => item.meta?.session_id !== body?.session_id);
        return json(route, { status: "cancelled", cancelled: [{ key: "pending_program", description: fx.proposalItem.title }] });
      }
      if (path === "/admin/live") return json(route, fx.liveResponse());
      if (path === "/admin/history") {
        return json(route, { generated_at: new Date().toISOString(), days: 14, items: [], total: 0, facets: { projects: [], repos: [], outcomes: [] } });
      }
      if (path.startsWith("/admin/programs/") && path.endsWith("/story")) return json(route, fx.ticketStory());
      if (path === "/admin/runtime-settings") return json(route, { ...settings, draft });
      if (path === "/admin/runtime-settings/drafts" && method === "POST") return json(route, { draft });
      if (path.startsWith("/admin/runtime-settings/drafts/") && path.endsWith("/validate")) {
        return json(route, { valid: true, issues: [] });
      }
      if (path.startsWith("/admin/runtime-settings/drafts/") && method === "PUT") {
        draftRevision += 1;
        draft = { id: draft.id, revision: `rev${String(draftRevision).padStart(5, "0")}aaaa`, definition: body.definition };
        return json(route, { draft });
      }
      if (path === "/healthz") return json(route, { status: "ok", components: {} });
      // Anything else the pages touch on the way: an empty but valid answer.
      return json(route, method === "GET" ? {} : { status: "ok" });
    },
  );

  return {
    calls,
    callsTo: (path: string) => calls.filter((call) => call.path === path),
  };
}
