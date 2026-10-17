import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { WorkflowBuilderPage } from "./WorkflowBuilderPage";

function installDialogMethods() {
  HTMLDialogElement.prototype.showModal = vi.fn(function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  });
  HTMLDialogElement.prototype.close = vi.fn(function close(this: HTMLDialogElement) {
    this.removeAttribute("open");
  });
}

const draft = {
  id: "draft-1",
  workflow_type: "ship_feature",
  status: "draft",
  revision: "rev-1",
  definition: {
    workflow_type: "ship_feature",
    definition_version: "1.0.0",
    display_name: "Ship feature",
    notifications: { bound_channel: true },
    promotion: { intake_stage: "draft_plan" },
    stages: [{ name: "draft_plan", kind: "task", agent_id: "pm-agent" }],
  },
};

const twoStageDraft = {
  ...draft,
  definition: {
    ...draft.definition,
    stages: [
      { name: "draft_plan", kind: "task", agent_id: "pm-agent" },
      { name: "review_plan", kind: "task", agent_id: "orchestrator", depends_on: ["draft_plan"] },
    ],
  },
};

function deferredResponse() {
  let resolve!: (value: Response) => void;
  const promise = new Promise<Response>((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

beforeEach(() => {
  vi.restoreAllMocks();
  installDialogMethods();
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation(() => ({
      matches: true,
      media: "",
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
});

it("saves, validates, and activates a draft with optimistic revisions", async () => {
  const calls: Array<{ url: string; method: string; body?: any }> = [];
  mockFetch((url, init) => {
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, method: init?.method || "GET", body });
    if (url === "/admin/workflow-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/workflow-definitions/catalog") {
      return jsonResponse({ hooks: ["hydrate_input"], agents: [], skills: [], stage_kinds: ["task"], templates: [] });
    }
    if (url === "/admin/workflow-definitions/drafts/draft-1") {
      if ((init?.method || "GET") === "PUT") return jsonResponse({ draft: { ...draft, revision: "rev-2" } });
      return jsonResponse({ draft });
    }
    if (url === "/admin/workflow-definitions/drafts/draft-1/validate") {
      return jsonResponse({ valid: true, issues: [], normalized_definition: draft.definition });
    }
    if (url === "/admin/workflow-definitions/drafts/draft-1/activate") {
      return jsonResponse({ active: { workflow_type: "ship_feature", definition_version: "1.0.0", revision: "active-rev" } });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<WorkflowBuilderPage route={{ kind: "draft", draftId: "draft-1" }} onNavigate={() => undefined} />);

  expect(await screen.findByText("Ship feature")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /save draft/i }));
  await userEvent.click(screen.getByRole("button", { name: /validate/i }));
  await userEvent.click(screen.getByRole("button", { name: /activate/i }));
  await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: /activate draft/i }));

  await waitFor(() =>
    expect(calls).toContainEqual(
      expect.objectContaining({
        url: "/admin/workflow-definitions/drafts/draft-1",
        method: "PUT",
        body: expect.objectContaining({ expected_revision: "rev-1" }),
      })
    )
  );
  expect(calls).toContainEqual(
    expect.objectContaining({
      url: "/admin/workflow-definitions/drafts/draft-1/activate",
      method: "POST",
      body: expect.objectContaining({ expected_revision: "rev-2" }),
    })
  );
});

it("focuses validation issues that use JSONPath or JSON pointer stage paths", async () => {
  mockFetch((url) => {
    if (url === "/admin/workflow-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/workflow-definitions/catalog") {
      return jsonResponse({ hooks: [], agents: [], skills: [], stage_kinds: ["task"], templates: [] });
    }
    if (url === "/admin/workflow-definitions/drafts/draft-1") return jsonResponse({ draft: twoStageDraft });
    if (url === "/admin/workflow-definitions/drafts/draft-1/validate") {
      return jsonResponse({
        valid: false,
        issues: [
          {
            severity: "error",
            code: "missing_agent_id",
            path: "$.stages[1].agent_id",
            message: "Review agent is required.",
          },
          {
            severity: "error",
            code: "missing_agent_id",
            path: "/stages/0/agent_id",
            message: "Draft agent is required.",
          },
        ],
      });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<WorkflowBuilderPage route={{ kind: "draft", draftId: "draft-1" }} onNavigate={() => undefined} />);

  await waitFor(() => expect(screen.getByLabelText("Stage name")).toHaveValue("draft_plan"));
  await userEvent.click(screen.getByRole("button", { name: /validate/i }));
  await userEvent.click((await screen.findAllByRole("button", { name: /focus/i }))[0]);

  expect(screen.getByLabelText("Stage name")).toHaveValue("review_plan");

  await userEvent.click(screen.getAllByRole("button", { name: /focus/i })[1]);

  expect(screen.getByLabelText("Stage name")).toHaveValue("draft_plan");
});

it("creates new workflows with the standard orchestrator agent", async () => {
  const calls: Array<{ url: string; method: string; body?: any }> = [];
  mockFetch((url, init) => {
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, method: init?.method || "GET", body });
    if (url === "/admin/workflow-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/workflow-definitions/catalog") {
      return jsonResponse({
        hooks: [],
        agents: [{ id: "orchestrator", label: "Orchestrator" }],
        skills: [],
        stage_kinds: ["task"],
        templates: [],
      });
    }
    if (url === "/admin/workflow-definitions/drafts") {
      return jsonResponse({ draft: { ...draft, definition: body.definition } });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<WorkflowBuilderPage route={{ kind: "new" }} onNavigate={() => undefined} />);

  expect(await screen.findByRole("heading", { name: "New workflow" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /save draft/i }));

  await waitFor(() =>
    expect(calls).toContainEqual(
      expect.objectContaining({
        url: "/admin/workflow-definitions/drafts",
        method: "POST",
        body: expect.objectContaining({
          definition: expect.objectContaining({
            stages: [expect.objectContaining({ agent_id: "orchestrator" })],
          }),
        }),
      })
    )
  );
});

it("persists and reloads canvas layout from draft editor metadata", async () => {
  const calls: Array<{ url: string; method: string; body?: any }> = [];
  const positionedDraft = {
    ...draft,
    editor_metadata: {
      layout: { draft_plan: { x: 321, y: 654 } },
      viewport: { x: 10, y: 20, zoom: 0.75 },
    },
  };
  mockFetch((url, init) => {
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, method: init?.method || "GET", body });
    if (url === "/admin/workflow-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/workflow-definitions/catalog") {
      return jsonResponse({ hooks: [], agents: [], skills: [], stage_kinds: ["task"], templates: [] });
    }
    if (url === "/admin/workflow-definitions/drafts/draft-1") {
      if ((init?.method || "GET") === "PUT") return jsonResponse({ draft: { ...positionedDraft, revision: "rev-2" } });
      return jsonResponse({ draft: positionedDraft });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<WorkflowBuilderPage route={{ kind: "draft", draftId: "draft-1" }} onNavigate={() => undefined} />);

  expect(await screen.findByText("Ship feature")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /save draft/i }));

  await waitFor(() =>
    expect(calls).toContainEqual(
      expect.objectContaining({
        url: "/admin/workflow-definitions/drafts/draft-1",
        method: "PUT",
        body: expect.objectContaining({
          definition: expect.not.objectContaining({ "x-builder-layout": expect.anything() }),
          editor_metadata: {
            layout: { draft_plan: { x: 321, y: 654 } },
            viewport: { x: 10, y: 20, zoom: 0.75 },
          },
        }),
      })
    )
  );
});

it("lets parent hash navigation handle dirty draft route changes without a local prompt", async () => {
  const onNavigate = vi.fn();
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  mockFetch((url) => {
    if (url === "/admin/workflow-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/workflow-definitions/catalog") {
      return jsonResponse({ hooks: [], agents: [], skills: [], stage_kinds: ["task"], templates: [] });
    }
    if (url === "/admin/workflow-definitions/drafts/draft-1") return jsonResponse({ draft });
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<WorkflowBuilderPage route={{ kind: "draft", draftId: "draft-1" }} onNavigate={onNavigate} />);

  expect(await screen.findByRole("heading", { name: "Ship feature" })).toBeInTheDocument();
  await userEvent.clear(screen.getByLabelText("Display name"));
  await userEvent.type(screen.getByLabelText("Display name"), "Ship feature changed");
  await userEvent.click(screen.getByRole("button", { name: /new workflow/i }));

  expect(confirm).not.toHaveBeenCalled();
  expect(onNavigate).toHaveBeenCalledWith({ kind: "new" });
});

it("ignores a stale new-draft save response after the route changes", async () => {
  const onNavigate = vi.fn();
  const save = deferredResponse();
  mockFetch((url, init) => {
    if (url === "/admin/workflow-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/workflow-definitions/catalog") {
      return jsonResponse({ hooks: [], agents: [], skills: [], stage_kinds: ["task"], templates: [] });
    }
    if (url === "/admin/workflow-definitions/drafts" && init?.method === "POST") return save.promise;
    throw new Error(`Unexpected URL ${url}`);
  });

  const view = renderPage(<WorkflowBuilderPage route={{ kind: "new" }} onNavigate={onNavigate} />);

  await userEvent.click(await screen.findByRole("button", { name: /save draft/i }));
  view.rerender(<WorkflowBuilderPage route={{ kind: "library" }} onNavigate={onNavigate} />);
  await act(async () => {
    save.resolve(jsonResponse({ draft: { ...draft, id: "late-draft" } }));
    await save.promise;
  });

  expect(onNavigate).not.toHaveBeenCalled();
});

it("navigates to the created draft when edits continue during the first save", async () => {
  const onNavigate = vi.fn();
  const allowDirtyNavigation = vi.fn();
  const save = deferredResponse();
  const calls: Array<{ url: string; method: string; body?: any }> = [];
  mockFetch((url, init) => {
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, method: init?.method || "GET", body });
    if (url === "/admin/workflow-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/workflow-definitions/catalog") {
      return jsonResponse({ hooks: [], agents: [], skills: [], stage_kinds: ["task"], templates: [] });
    }
    if (url === "/admin/workflow-definitions/drafts" && init?.method === "POST") return save.promise;
    if (url === "/admin/workflow-definitions/drafts/created-1" && (init?.method || "GET") === "PUT") {
      return jsonResponse({ draft: { ...draft, id: "created-1", revision: "rev-2", definition: body.definition } });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  const view = renderPage(
    <WorkflowBuilderPage
      route={{ kind: "new" }}
      onNavigate={onNavigate}
      allowDirtyNavigation={allowDirtyNavigation}
    />
  );

  await userEvent.click(await screen.findByRole("button", { name: /save draft/i }));
  await userEvent.clear(screen.getByLabelText("Display name"));
  await userEvent.type(screen.getByLabelText("Display name"), "Edited while saving");
  await act(async () => {
    save.resolve(
      jsonResponse({
        draft: {
          ...draft,
          id: "created-1",
          revision: "rev-1",
          definition: { ...draft.definition, display_name: "New workflow" },
        },
      })
    );
    await save.promise;
  });

  expect(allowDirtyNavigation).toHaveBeenCalled();
  expect(onNavigate).toHaveBeenCalledWith({ kind: "draft", draftId: "created-1" });
  expect(screen.getByLabelText("Display name")).toHaveValue("Edited while saving");

  view.rerender(
    <WorkflowBuilderPage
      route={{ kind: "draft", draftId: "created-1" }}
      onNavigate={onNavigate}
      allowDirtyNavigation={allowDirtyNavigation}
    />
  );
  expect(screen.getByLabelText("Display name")).toHaveValue("Edited while saving");
  await userEvent.click(screen.getByRole("button", { name: /save draft/i }));
  await waitFor(() =>
    expect(calls).toContainEqual(
      expect.objectContaining({
        url: "/admin/workflow-definitions/drafts/created-1",
        method: "PUT",
        body: expect.objectContaining({
          expected_revision: "rev-1",
          definition: expect.objectContaining({ display_name: "Edited while saving" }),
        }),
      })
    )
  );
});

it("shows an activation warning when runtime reload fails", async () => {
  const onNavigate = vi.fn();
  mockFetch((url, init) => {
    if (url === "/admin/workflow-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/workflow-definitions/catalog") {
      return jsonResponse({ hooks: [], agents: [], skills: [], stage_kinds: ["task"], templates: [] });
    }
    if (url === "/admin/workflow-definitions/drafts/draft-1") return jsonResponse({ draft });
    if (url === "/admin/workflow-definitions/drafts/draft-1/validate") {
      return jsonResponse({ valid: true, issues: [], normalized_definition: draft.definition });
    }
    if (url === "/admin/workflow-definitions/drafts/draft-1/activate" && init?.method === "POST") {
      return jsonResponse({
        active: { workflow_type: "ship_feature", definition_version: "1.0.0", revision: "active-rev" },
        reload_ok: false,
        warning: "workflow definition activated but reload failed",
      });
    }
    if (url === "/admin/workflow-definitions/ship_feature") {
      return jsonResponse({ status: "active", read_only: false, definition: draft.definition });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  const view = renderPage(
    <WorkflowBuilderPage route={{ kind: "draft", draftId: "draft-1" }} onNavigate={onNavigate} />
  );

  await userEvent.click(await screen.findByRole("button", { name: /validate/i }));
  await waitFor(() => expect(screen.getByRole("button", { name: /activate/i })).toBeEnabled());
  await userEvent.click(screen.getByRole("button", { name: /activate/i }));
  await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: /activate draft/i }));

  await waitFor(() =>
    expect(onNavigate).toHaveBeenCalledWith({ kind: "workflow", workflowType: "ship_feature" })
  );
  view.rerender(
    <WorkflowBuilderPage route={{ kind: "workflow", workflowType: "ship_feature" }} onNavigate={onNavigate} />
  );
  expect(await screen.findByText(/workflow definition activated but reload failed/i)).toBeInTheDocument();
});

it("ignores a stale clone response after the route changes", async () => {
  const onNavigate = vi.fn();
  const cloneCreate = deferredResponse();
  mockFetch((url, init) => {
    if (url === "/admin/workflow-definitions") {
      return jsonResponse({
        definitions: [
          {
            workflow_type: "ship_feature",
            definition_version: "1.0.0",
            display_name: "Ship feature",
            status: "active",
          },
        ],
      });
    }
    if (url === "/admin/workflow-definitions/drafts" && init?.method === "POST") return cloneCreate.promise;
    if (url === "/admin/workflow-definitions/catalog") {
      return jsonResponse({ hooks: [], agents: [], skills: [], stage_kinds: ["task"], templates: [] });
    }
    if (url === "/admin/workflow-definitions/ship_feature") return jsonResponse({ definition: draft.definition });
    throw new Error(`Unexpected URL ${url}`);
  });

  const view = renderPage(<WorkflowBuilderPage route={{ kind: "library" }} onNavigate={onNavigate} />);

  await userEvent.click(await screen.findByRole("button", { name: /clone ship_feature/i }));
  view.rerender(<WorkflowBuilderPage route={{ kind: "new" }} onNavigate={onNavigate} />);
  await act(async () => {
    cloneCreate.resolve(jsonResponse({ draft: { ...draft, id: "late-clone" } }));
    await cloneCreate.promise;
  });

  expect(onNavigate).not.toHaveBeenCalled();
});

it("clones draft rows from the draft endpoint and preserves editor metadata", async () => {
  const onNavigate = vi.fn();
  const calls: Array<{ url: string; method: string; body?: any }> = [];
  const positionedDraft = {
    ...draft,
    editor_metadata: { layout: { draft_plan: { x: 321, y: 654 } } },
  };
  mockFetch((url, init) => {
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, method: init?.method || "GET", body });
    if (url === "/admin/workflow-definitions") {
      return jsonResponse({
        definitions: [
          {
            id: "draft-1",
            workflow_type: "ship_feature",
            definition_version: "1.0.0",
            display_name: "Ship feature draft",
            status: "draft",
            revision: "rev-1",
          },
        ],
      });
    }
    if (url === "/admin/workflow-definitions/catalog") {
      return jsonResponse({ hooks: [], agents: [], skills: [], stage_kinds: ["task"], templates: [] });
    }
    if (url === "/admin/workflow-definitions/drafts/draft-1") return jsonResponse({ draft: positionedDraft });
    if (url === "/admin/workflow-definitions/drafts") {
      return jsonResponse({ draft: { ...positionedDraft, id: "draft-copy", definition: body.definition } });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<WorkflowBuilderPage route={{ kind: "library" }} onNavigate={onNavigate} />);

  await userEvent.click(await screen.findByRole("button", { name: /clone ship_feature/i }));

  await waitFor(() =>
    expect(calls).toContainEqual(
      expect.objectContaining({
        url: "/admin/workflow-definitions/drafts",
        method: "POST",
        body: {
          definition: expect.objectContaining({
            workflow_type: "ship_feature_custom",
            definition_version: "0.1.0",
            display_name: "Ship feature copy",
          }),
          editor_metadata: positionedDraft.editor_metadata,
        },
      })
    )
  );
  expect(calls).toContainEqual(expect.objectContaining({ url: "/admin/workflow-definitions/drafts/draft-1" }));
  expect(calls).not.toContainEqual(expect.objectContaining({ url: "/admin/workflow-definitions/ship_feature" }));
  expect(onNavigate).toHaveBeenCalledWith({ kind: "draft", draftId: "draft-copy" });
});

it("keeps activation disabled until validation succeeds", async () => {
  mockFetch((url) => {
    if (url === "/admin/workflow-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/workflow-definitions/catalog") {
      return jsonResponse({ hooks: [], agents: [], skills: [], stage_kinds: ["task"], templates: [] });
    }
    if (url === "/admin/workflow-definitions/drafts/draft-1") return jsonResponse({ draft });
    if (url === "/admin/workflow-definitions/drafts/draft-1/validate") {
      return jsonResponse({ valid: true, issues: [], normalized_definition: draft.definition });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<WorkflowBuilderPage route={{ kind: "draft", draftId: "draft-1" }} onNavigate={() => undefined} />);

  const activate = await screen.findByRole("button", { name: /activate/i });
  expect(activate).toBeDisabled();

  await userEvent.click(screen.getByRole("button", { name: /validate/i }));

  await waitFor(() => expect(activate).toBeEnabled());
});

it("only offers validation focus for stage paths", async () => {
  mockFetch((url) => {
    if (url === "/admin/workflow-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/workflow-definitions/catalog") {
      return jsonResponse({ hooks: [], agents: [], skills: [], stage_kinds: ["task"], templates: [] });
    }
    if (url === "/admin/workflow-definitions/drafts/draft-1") return jsonResponse({ draft });
    if (url === "/admin/workflow-definitions/drafts/draft-1/validate") {
      return jsonResponse({
        valid: false,
        issues: [
          {
            severity: "error",
            code: "missing_agent_id",
            path: "$.stages[0].agent_id",
            message: "Draft agent is required.",
          },
          {
            severity: "error",
            code: "missing_workflow_type",
            path: "$.workflow_type",
            message: "Workflow type is required.",
          },
          {
            severity: "error",
            code: "missing_agent_id",
            path: "$.stages[99].agent_id",
            message: "Missing stage should not be focusable.",
          },
        ],
      });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<WorkflowBuilderPage route={{ kind: "draft", draftId: "draft-1" }} onNavigate={() => undefined} />);

  await userEvent.click(await screen.findByRole("button", { name: /validate/i }));

  expect(await screen.findByText("Draft agent is required.")).toBeInTheDocument();
  expect(screen.getByText("Workflow type is required.")).toBeInTheDocument();
  expect(screen.getByText("Missing stage should not be focusable.")).toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: /focus/i })).toHaveLength(1);
});

it("disables activation after workflow-level edits until revalidated", async () => {
  mockFetch((url) => {
    if (url === "/admin/workflow-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/workflow-definitions/catalog") {
      return jsonResponse({ hooks: [], agents: [], skills: [], stage_kinds: ["task"], templates: [] });
    }
    if (url === "/admin/workflow-definitions/drafts/draft-1") return jsonResponse({ draft });
    if (url === "/admin/workflow-definitions/drafts/draft-1/validate") {
      return jsonResponse({ valid: true, issues: [], normalized_definition: draft.definition });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<WorkflowBuilderPage route={{ kind: "draft", draftId: "draft-1" }} onNavigate={() => undefined} />);

  const activate = await screen.findByRole("button", { name: /activate/i });
  await userEvent.click(screen.getByRole("button", { name: /validate/i }));

  await waitFor(() => expect(activate).toBeEnabled());
  await userEvent.clear(screen.getByLabelText("Workflow type"));
  await userEvent.type(screen.getByLabelText("Workflow type"), "ship_feature_v2");

  expect(activate).toBeDisabled();
});

it("ignores a stale validate response after switching drafts", async () => {
  const validate = deferredResponse();
  const draftB = {
    ...draft,
    id: "draft-2",
    definition: { ...draft.definition, display_name: "Ship feature B", workflow_type: "ship_feature_b" },
  };
  mockFetch((url) => {
    if (url === "/admin/workflow-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/workflow-definitions/catalog") {
      return jsonResponse({ hooks: [], agents: [], skills: [], stage_kinds: ["task"], templates: [] });
    }
    if (url === "/admin/workflow-definitions/drafts/draft-1") return jsonResponse({ draft });
    if (url === "/admin/workflow-definitions/drafts/draft-2") return jsonResponse({ draft: draftB });
    if (url === "/admin/workflow-definitions/drafts/draft-1/validate") return validate.promise;
    throw new Error(`Unexpected URL ${url}`);
  });

  const view = renderPage(
    <WorkflowBuilderPage route={{ kind: "draft", draftId: "draft-1" }} onNavigate={() => undefined} />
  );

  await userEvent.click(await screen.findByRole("button", { name: /validate/i }));
  view.rerender(
    <WorkflowBuilderPage route={{ kind: "draft", draftId: "draft-2" }} onNavigate={() => undefined} />
  );
  expect(await screen.findByRole("heading", { name: "Ship feature B" })).toBeInTheDocument();
  await act(async () => {
    validate.resolve(jsonResponse({ valid: true, issues: [], normalized_definition: draft.definition }));
    await validate.promise;
  });

  expect(screen.getByRole("button", { name: /activate/i })).toBeDisabled();
});

it("keeps newer local edits when a save response arrives late", async () => {
  const save = deferredResponse();
  mockFetch((url, init) => {
    if (url === "/admin/workflow-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/workflow-definitions/catalog") {
      return jsonResponse({ hooks: [], agents: [], skills: [], stage_kinds: ["task"], templates: [] });
    }
    if (url === "/admin/workflow-definitions/drafts/draft-1") {
      if ((init?.method || "GET") === "PUT") return save.promise;
      return jsonResponse({ draft });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<WorkflowBuilderPage route={{ kind: "draft", draftId: "draft-1" }} onNavigate={() => undefined} />);

  expect(await screen.findByRole("heading", { name: "Ship feature" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /save draft/i }));
  await userEvent.clear(screen.getByLabelText("Display name"));
  await userEvent.type(screen.getByLabelText("Display name"), "Edited during save");
  await act(async () => {
    save.resolve(jsonResponse({ draft: { ...draft, revision: "rev-2" } }));
    await save.promise;
  });

  expect(screen.getByLabelText("Display name")).toHaveValue("Edited during save");
  expect(screen.getByRole("heading", { name: "Edited during save" })).toBeInTheDocument();
});

it("clears save busy state after navigating away from an in-flight save", async () => {
  const save = deferredResponse();
  mockFetch((url, init) => {
    if (url === "/admin/workflow-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/workflow-definitions/catalog") {
      return jsonResponse({ hooks: [], agents: [], skills: [], stage_kinds: ["task"], templates: [] });
    }
    if (url === "/admin/workflow-definitions/drafts" && init?.method === "POST") return save.promise;
    if (url === "/admin/workflow-definitions/drafts/draft-1") return jsonResponse({ draft });
    throw new Error(`Unexpected URL ${url}`);
  });

  const view = renderPage(<WorkflowBuilderPage route={{ kind: "new" }} onNavigate={() => undefined} />);
  await userEvent.click(await screen.findByRole("button", { name: /save draft/i }));
  view.rerender(
    <WorkflowBuilderPage route={{ kind: "draft", draftId: "draft-1" }} onNavigate={() => undefined} />
  );
  expect(await screen.findByRole("heading", { name: "Ship feature" })).toBeInTheDocument();
  await act(async () => {
    save.resolve(jsonResponse({ draft: { ...draft, id: "late-draft" } }));
    await save.promise;
  });

  expect(screen.getByRole("button", { name: /save draft/i })).not.toHaveAttribute("aria-busy", "true");
});

it("restores dirty state when clone fails after discard confirmation", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  mockFetch((url, init) => {
    if (url === "/admin/workflow-definitions") {
      return jsonResponse({
        definitions: [
          {
            workflow_type: "ship_feature",
            definition_version: "1.0.0",
            display_name: "Ship feature",
            status: "active",
          },
        ],
      });
    }
    if (url === "/admin/workflow-definitions/catalog") {
      return jsonResponse({ hooks: [], agents: [], skills: [], stage_kinds: ["task"], templates: [] });
    }
    if (url === "/admin/workflow-definitions/drafts/draft-1") return jsonResponse({ draft });
    if (url === "/admin/workflow-definitions/ship_feature") return jsonResponse({ definition: draft.definition });
    if (url === "/admin/workflow-definitions/drafts" && init?.method === "POST") {
      return Promise.reject(new Error("clone failed"));
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<WorkflowBuilderPage route={{ kind: "draft", draftId: "draft-1" }} onNavigate={() => undefined} />);
  await userEvent.clear(await screen.findByLabelText("Display name"));
  await userEvent.type(screen.getByLabelText("Display name"), "Dirty draft");
  await userEvent.click(screen.getByRole("button", { name: /clone ship_feature/i }));

  expect(confirm).toHaveBeenCalled();
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/clone failed/i));
  expect(screen.getByLabelText("Display name")).toHaveValue("Dirty draft");
});

it("appends attachSkill to the selected stage skill hints", async () => {
  mockFetch((url) => {
    if (url === "/admin/workflow-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/workflow-definitions/catalog") {
      return jsonResponse({
        hooks: [],
        agents: [],
        skills: [{ id: "hello-skill", label: "hello-skill" }],
        stage_kinds: ["task"],
        templates: [],
      });
    }
    if (url === "/admin/workflow-definitions/ship_feature") {
      return jsonResponse({ status: "active", read_only: false, definition: draft.definition });
    }
    return jsonResponse({});
  });
  window.location.hash = "#/workflows/ship_feature?attachSkill=hello-skill&stage=draft_plan";
  renderPage(
    <WorkflowBuilderPage
      route={{ kind: "workflow", workflowType: "ship_feature" }}
      onNavigate={() => undefined}
      attachSkill="hello-skill"
      attachStage="draft_plan"
    />
  );
  const hints = await screen.findByLabelText("Skill hints");
  await waitFor(() => expect((hints as HTMLSelectElement).value).toBe("hello-skill"));
  expect(window.location.hash).toBe("#/workflows/ship_feature");
});

it("selects a used-by stage from the stage query alone", async () => {
  mockFetch((url) => {
    if (url === "/admin/workflow-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/workflow-definitions/catalog") {
      return jsonResponse({ hooks: [], agents: [], skills: [], stage_kinds: ["task"], templates: [] });
    }
    if (url === "/admin/workflow-definitions/ship_feature") {
      return jsonResponse({ status: "active", read_only: false, definition: twoStageDraft.definition });
    }
    return jsonResponse({});
  });
  window.location.hash = "#/workflows/ship_feature?stage=review_plan";
  renderPage(
    <WorkflowBuilderPage
      route={{ kind: "workflow", workflowType: "ship_feature" }}
      onNavigate={() => undefined}
      attachStage="review_plan"
    />
  );
  await waitFor(() => expect(screen.getByLabelText("Stage name")).toHaveValue("review_plan"));
  expect(window.location.hash).toBe("#/workflows/ship_feature");
});

it("shows a banner when attachSkill is missing from the catalog", async () => {
  mockFetch((url) => {
    if (url === "/admin/workflow-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/workflow-definitions/catalog") {
      return jsonResponse({ hooks: [], agents: [], skills: [], stage_kinds: ["task"], templates: [] });
    }
    if (url === "/admin/workflow-definitions/ship_feature") {
      return jsonResponse({ status: "active", read_only: false, definition: draft.definition });
    }
    return jsonResponse({});
  });
  renderPage(
    <WorkflowBuilderPage
      route={{ kind: "workflow", workflowType: "ship_feature" }}
      onNavigate={() => undefined}
      attachSkill="missing-skill"
    />
  );
  expect(await screen.findByText(/missing or unapproved/i)).toBeInTheDocument();
});
