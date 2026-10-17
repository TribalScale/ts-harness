import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setPermissions } from "../auth/session";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { prepareUpload, SkillCreatePanel } from "./SkillCreatePanel";
import { SkillsCatalogPage } from "./SkillsCatalogPage";

const SKILL_MD = "---\nid: launch-kit\nversion: 1.2.0\ncategory: marketing\ndescription: Launch kit\n---\n# Kit\n";

function fileAt(path: string, content: string | Uint8Array): File {
  const file = new File([content], path.split("/").pop() || path);
  Object.defineProperty(file, "webkitRelativePath", { value: path });
  return file;
}

beforeEach(() => {
  setPermissions(["view", "edit_config"]);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SkillCreatePanel", () => {
  it("prefills fields from pasted frontmatter and submits text mode", async () => {
    const fetchMock = mockFetch((url) => {
      if (url === "/admin/skills/drafts") {
        return jsonResponse({
          status: "draft",
          skill_id: "launch-kit",
          layout: "file",
          inbox_href: "#/inbox/skill_draft/launch-kit",
        });
      }
      return jsonResponse({});
    });
    renderPage(<SkillCreatePanel mode="paste" onClose={() => undefined} />);
    fireEvent.change(screen.getByLabelText("Skill markdown"), { target: { value: SKILL_MD } });
    expect(screen.getByLabelText("Skill id")).toHaveValue("launch-kit");
    expect(screen.getByLabelText("Skill category")).toHaveValue("marketing");
    expect(screen.getByLabelText("Skill description")).toHaveValue("Launch kit");
    expect(screen.getByLabelText("Skill version")).toHaveValue("1.2.0");

    await userEvent.click(screen.getByRole("button", { name: "Submit for review" }));
    expect(await screen.findByRole("link", { name: "Open in Inbox" })).toHaveAttribute(
      "href",
      "#/inbox/skill_draft/launch-kit"
    );
    const call = fetchMock.mock.calls.find((item) => String(item[0]) === "/admin/skills/drafts");
    const body = JSON.parse(String((call?.[1] as RequestInit).body));
    expect(body).toMatchObject({ mode: "text", id: "launch-kit", category: "marketing", markdown: SKILL_MD });
  });

  it("uploads a nested folder and shows server errors with the path", async () => {
    const fetchMock = mockFetch((url) => {
      if (url === "/admin/skills/drafts") {
        return jsonResponse(
          { error: "skill draft rejected by threat scan", error_code: "skill_upload_scan", path: "stages/plan.md" },
          400
        );
      }
      return jsonResponse({});
    });
    renderPage(<SkillCreatePanel mode="folder" onClose={() => undefined} />);
    fireEvent.change(screen.getByLabelText("Skill folder"), {
      target: {
        files: [
          fileAt("launch-kit/SKILL.md", SKILL_MD),
          fileAt("launch-kit/stages/plan.md", "# Plan\n"),
          fileAt("launch-kit/.DS_Store", "junk"),
        ],
      },
    });
    expect(await screen.findByText(/stages\/plan\.md/)).toBeInTheDocument();
    expect(screen.getByText(/Skipped: launch-kit\/\.DS_Store/)).toBeInTheDocument();

    const submit = screen.getByRole("button", { name: "Submit for review" });
    await waitFor(() => expect(submit).toBeEnabled());
    await userEvent.click(submit);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "skill draft rejected by threat scan (stages/plan.md)"
    );
    const call = fetchMock.mock.calls.find((item) => String(item[0]) === "/admin/skills/drafts");
    const body = JSON.parse(String((call?.[1] as RequestInit).body));
    expect(body.mode).toBe("folder");
    expect(body.files.map((item: { path: string }) => item.path)).toEqual(["SKILL.md", "stages/plan.md"]);
  });

  it("blocks submit for disallowed files until they are removed", async () => {
    renderPage(<SkillCreatePanel mode="folder" onClose={() => undefined} />);
    fireEvent.change(screen.getByLabelText("Skill folder"), {
      target: { files: [fileAt("kit/SKILL.md", SKILL_MD), fileAt("kit/run.py", "print(1)")] },
    });
    expect(await screen.findByText("file type not allowed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Submit for review" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(screen.getByRole("button", { name: "Submit for review" })).toBeEnabled();
  });
});

describe("prepareUpload", () => {
  it("strips junk, flags invalid UTF-8, nested SKILL.md, and a missing entrypoint", async () => {
    const prepared = await prepareUpload([
      { path: "kit/SKILL.md", file: new Blob([SKILL_MD]) },
      { path: "kit/bad.txt", file: new Blob([new Uint8Array([0xff, 0xfe, 0x00])]) },
      { path: "kit/sub/SKILL.md", file: new Blob([SKILL_MD]) },
      { path: "kit/__MACOSX/._SKILL.md", file: new Blob(["x"]) },
      { path: "kit/Thumbs.db", file: new Blob(["x"]) },
    ]);
    expect(prepared.skipped).toEqual(["kit/__MACOSX/._SKILL.md", "kit/Thumbs.db"]);
    const problems = Object.fromEntries(prepared.files.map((item) => [item.path, item.problem]));
    expect(problems["SKILL.md"]).toBeUndefined();
    expect(problems["bad.txt"]).toBe("not valid UTF-8 text");
    expect(problems["sub/SKILL.md"]).toBe("nested SKILL.md");

    const missing = await prepareUpload([{ path: "kit/notes.md", file: new Blob(["x"]) }]);
    expect(missing.problems).toContain("The folder needs SKILL.md at its top level.");
  });
});

describe("Skills catalog create buttons", () => {
  function mockCatalog() {
    mockFetch((url) => {
      if (url === "/admin/skills") {
        return jsonResponse({
          skills: [
            {
              id: "launch-kit",
              category: "marketing",
              version: "1.0.0",
              trust_level: "trusted",
              approval_status: "approved",
              origin: "managed",
              description: "Launch kit",
              agents: [],
              doctor_ok: true,
              used_by_workflows: 0,
              used_by_cron: 0,
            },
          ],
        });
      }
      if (url === "/admin/workflow-definitions") return jsonResponse({ definitions: [] });
      if (url === "/admin/cron/jobs") return jsonResponse({ jobs: [] });
      return jsonResponse({});
    });
  }

  it("shows Paste and Upload for managers with an Origin column", async () => {
    mockCatalog();
    renderPage(<SkillsCatalogPage />);
    expect(await screen.findByRole("columnheader", { name: "Origin" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "managed" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Upload folder" }));
    expect(screen.getByRole("region", { name: "Upload skill folder" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Paste skill" }));
    expect(screen.getByRole("region", { name: "Paste skill" })).toBeInTheDocument();
  });

  it("hides Paste and Upload without edit_config", async () => {
    setPermissions(["view"]);
    mockCatalog();
    renderPage(<SkillsCatalogPage />);
    expect(await screen.findByRole("link", { name: "launch-kit" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Paste skill" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Upload folder" })).not.toBeInTheDocument();
  });
});
