import React from "react";
import { render } from "@testing-library/react";
import { vi } from "vitest";
import { AppTheme } from "../theme/AppTheme";

export const TEST_TOKEN = "test-token";

/** Pages under test are already signed in: the console no longer stores a bearer. */
export function seedTestSession() {
  localStorage.removeItem("ec_admin_token");
}

export function renderPage(node: React.ReactElement) {
  seedTestSession();
  if (!vi.isMockFunction(globalThis.fetch)) {
    mockFetch((url) => {
      if (String(url).includes("/admin/auth/config")) {
        return jsonResponse({
          mode: "static",
          authenticated: true,
          roles: ["admin"],
          permissions: [
            "view",
            "start_program",
            "approve_gate",
            "confirm_delegation",
            "edit_config",
            "edit_models",
            "edit_allowlists",
          ],
        });
      }
      return jsonResponse({});
    });
  }
  return render(node, { wrapper: AppTheme });
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function mockFetch(
  handler: (url: string, init?: RequestInit) => Promise<Response> | Response
) {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) =>
    Promise.resolve(handler(String(input), init))
  );
}
