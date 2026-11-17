import { describe, expect, it } from "vitest";
import {
  artifactRows,
  programDocuments,
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
    expect(chatReplyText({ output: { summary: "" }, reply: "fallback" })).toBe(
      "",
    );
  });

  it("hides tool payloads and system events", () => {
    expect(
      messageDisplayText({
        role: "tool",
        name: "program_status",
        content: '{"resolved_config":{"big":true}}',
      }),
    ).toBe("");
    expect(
      messageDisplayText({ role: "system", content: "session metadata" }),
    ).toBe("");
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

describe("programDocuments", () => {
  it("lists the PRD and the planning stages' documents once each", () => {
    const docs = programDocuments({
      approved_prd: { result: { prd_markdown: "# PRD" } },
      refinery_artifacts: { frds_markdown: "# FRDs", other: "x" },
      foundry_artifacts: { blueprints_markdown: "# Blueprints" },
      planner_artifacts: {
        work_orders_markdown: "# WOs",
        context_graph_markdown: "graph",
      },
      work_orders_markdown: "# WOs",
      pr_url: "https://github.com/o/r/pull/1",
    });
    expect(docs.map((d) => [d.stage, d.title])).toEqual([
      ["PRD", "PRD"],
      ["Refinery", "Feature requirements (FRDs)"],
      ["Foundry", "Blueprints"],
      ["Planner", "Work orders"],
      ["Planner", "Context graph"],
    ]);
    expect(programDocuments({ approved_prd: "# Plain PRD" })[0].markdown).toBe(
      "# Plain PRD",
    );
    expect(programDocuments({})).toEqual([]);
  });
});
