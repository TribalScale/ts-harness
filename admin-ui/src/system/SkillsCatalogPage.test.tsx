import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { SystemPage } from "./SystemPage";
import { SkillsCatalogPage } from "./SkillsCatalogPage";

function skillRow() {
  return {
    id: "hello-skill",
    category: "general",
    version: "1.0.0",
    trust_level: "builtin",
    approval_status: "approved",
    description: "Hello skill",
    agents: ["orchestrator"],
    doctor_ok: true,
    used_by_workflows: 1,
    used_by_cron: 0,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("System Skills catalog", () => {
  it("renders the list on #/system/skills", async () => {
    mockFetch((url) => {
      if (url === "/admin/skills") return jsonResponse({ skills: [skillRow()] });
      if (url === "/admin/workflow-definitions") return jsonResponse({ definitions: [] });
      if (url === "/admin/cron/jobs") return jsonResponse({ jobs: [] });
      return jsonResponse({});
    });
    renderPage(<SystemPage section="skills" />);
    expect(await screen.findByRole("tab", { name: "Skills" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByRole("heading", { name: "Skills" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "hello-skill" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Learn a skill" })).toBeInTheDocument();
  });

  it("Learn a skill opens Chat with /learn prefilled", async () => {
    mockFetch((url) => {
      if (url === "/admin/skills") return jsonResponse({ skills: [skillRow()] });
      if (url === "/admin/workflow-definitions") return jsonResponse({ definitions: [] });
      if (url === "/admin/cron/jobs") return jsonResponse({ jobs: [] });
      return jsonResponse({});
    });
    renderPage(<SkillsCatalogPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Learn a skill" }));
    expect(window.location.hash).toBe("#/chat?prefill=%2Flearn%20");
  });

  it("shows a rate limit error instead of an empty catalog", async () => {
    const fetchMock = mockFetch((url) => {
      if (url === "/admin/skills") return jsonResponse({ error: "rate limit exceeded" }, 429);
      return jsonResponse({});
    });
    renderPage(<SkillsCatalogPage />);
    expect(await screen.findByText(/rate limit exceeded/)).toBeInTheDocument();
    expect(screen.queryByText("No catalog skills")).not.toBeInTheDocument();
    const skillCalls = fetchMock.mock.calls.filter((call) => String(call[0]) === "/admin/skills");
    expect(skillCalls).toHaveLength(1);
  });

  it("shows Learn a skill from an empty catalog", async () => {
    mockFetch((url) => {
      if (url === "/admin/skills") return jsonResponse({ skills: [] });
      if (url === "/admin/workflow-definitions") return jsonResponse({ definitions: [] });
      if (url === "/admin/cron/jobs") return jsonResponse({ jobs: [] });
      return jsonResponse({});
    });
    renderPage(<SkillsCatalogPage />);
    expect(await screen.findByText("No catalog skills")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Learn a skill" }));
    expect(window.location.hash).toBe("#/chat?prefill=%2Flearn%20");
  });

  it("renders detail metadata", async () => {
    mockFetch((url) => {
      if (url === "/admin/skills") return jsonResponse({ skills: [skillRow()] });
      if (url === "/admin/skills/hello-skill") {
        return jsonResponse({
          ...skillRow(),
          source_path: "skills/general/hello-skill.md",
          doctor: {},
          used_by: { workflows: [{ workflow_type: "prd_request", stage: "intake" }], cron: [] },
        });
      }
      if (url === "/admin/workflow-definitions") {
        return jsonResponse({ definitions: [{ workflow_type: "prd_request", display_name: "PRD" }] });
      }
      if (url === "/admin/cron/jobs") return jsonResponse({ jobs: [] });
      return jsonResponse({});
    });
    renderPage(<SkillsCatalogPage selectedId="hello-skill" />);
    expect(await screen.findByRole("heading", { name: "hello-skill" })).toBeInTheDocument();
    expect(screen.getByText("skills/general/hello-skill.md")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "prd_request" })).toHaveAttribute(
      "href",
      "#/workflows/prd_request?stage=intake"
    );
  });

  it("pages the catalog ten skills at a time", async () => {
    const skills = Array.from({ length: 12 }, (_, index) => ({
      ...skillRow(),
      id: `skill-${String(index + 1).padStart(2, "0")}`,
      description: `Skill ${index + 1}`,
    }));
    mockFetch((url) => {
      if (url === "/admin/skills") return jsonResponse({ skills });
      if (url === "/admin/workflow-definitions") return jsonResponse({ definitions: [] });
      if (url === "/admin/cron/jobs") return jsonResponse({ jobs: [] });
      return jsonResponse({});
    });
    renderPage(<SkillsCatalogPage />);
    expect(await screen.findByRole("link", { name: "skill-01" })).toBeInTheDocument();
    expect(screen.getByText("Showing 1–10 of 12 skills")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "skill-11" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByRole("link", { name: "skill-11" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "skill-12" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "skill-01" })).not.toBeInTheDocument();
    expect(screen.getByText("Showing 11–12 of 12 skills")).toBeInTheDocument();
  });

  it("writes attachSkill hash and does not PUT workflows", async () => {
    const user = userEvent.setup();
    const methods: string[] = [];
    mockFetch((url, init) => {
      methods.push(`${init?.method || "GET"} ${url}`);
      if (url === "/admin/skills") return jsonResponse({ skills: [skillRow()] });
      if (url === "/admin/skills/hello-skill") {
        return jsonResponse({
          ...skillRow(),
          doctor: {},
          used_by: { workflows: [], cron: [] },
        });
      }
      if (url === "/admin/workflow-definitions") {
        return jsonResponse({ definitions: [{ workflow_type: "prd_request" }] });
      }
      if (url === "/admin/cron/jobs") return jsonResponse({ jobs: [] });
      return jsonResponse({});
    });
    renderPage(<SkillsCatalogPage selectedId="hello-skill" />);
    await screen.findByLabelText("Workflow type");
    await user.selectOptions(screen.getByLabelText("Workflow type"), "prd_request");
    await user.type(screen.getByLabelText("Workflow stage"), "intake");
    await user.click(screen.getByRole("button", { name: "Attach to workflow" }));
    expect(window.location.hash).toBe("#/workflows/prd_request?attachSkill=hello-skill&stage=intake");
    expect(methods.some((item) => item.startsWith("PUT ") || item.startsWith("PATCH "))).toBe(false);
  });
});
