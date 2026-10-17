import { describe, expect, it } from "vitest";
import {
  artifactRows,
  chatReplyText,
  messageDisplayText,
  normalizeHealth,
} from "./normalizers";

describe("normalizeHealth", () => {
  it("preserves backend component statuses", () => {
    const result = normalizeHealth({
      status: "degraded",
      components: {
        provider: { status: "ok", detail: null },
        memory_api: { status: "unavailable", detail: "connection refused" },
      },
    });
    expect(result.components).toEqual([
      { name: "provider", label: "Provider", status: "ok", detail: null },
      {
        name: "memory_api",
        label: "Memory API",
        status: "unavailable",
        detail: "connection refused",
      },
    ]);
  });
});

describe("chat normalization", () => {
  it("reads the gateway output summary", () => {
    expect(chatReplyText({ output: { summary: "Done" } })).toBe("Done");
  });

  it("keeps an explicitly empty output summary", () => {
    expect(chatReplyText({ output: { summary: "" }, reply: "fallback" })).toBe("");
  });

  it("hides tool payloads and system events", () => {
    expect(
      messageDisplayText({
        role: "tool",
        name: "program_status",
        content: '{"resolved_config":{"big":true}}',
      })
    ).toBe("");
    expect(messageDisplayText({ role: "system", content: "session metadata" })).toBe("");
  });
});

describe("artifactRows", () => {
  it("identifies links and collapsible long values", () => {
    const rows = artifactRows({
      pr_url: "https://github.com/acme/repo/pull/1",
      work_order: "x".repeat(180),
    });
    expect(rows[0].href).toBe("https://github.com/acme/repo/pull/1");
    expect(rows[1].expandable).toBe(true);
  });

  it("stringifies undefined artifact values safely", () => {
    const rows = artifactRows({ missing: undefined });

    expect(rows[0]).toMatchObject({
      key: "missing",
      label: "missing",
      value: "undefined",
      expandable: false,
    });
  });
});
