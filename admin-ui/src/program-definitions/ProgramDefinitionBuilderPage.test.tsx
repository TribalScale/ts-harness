import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { ProgramDefinitionBuilderPage } from "./ProgramDefinitionBuilderPage";

vi.mock("@xyflow/react", async () => {
  const React = await import("react");
  return {
    Background: () => null,
    Controls: () => null,
    ReactFlowProvider: ({ children }: { children: React.ReactNode }) =>
      React.createElement("div", null, children),
    ReactFlow: ({ children }: { children: React.ReactNode }) =>
      React.createElement("div", { "aria-label": "mock react flow" }, children),
    addEdge: (edge: unknown, edges: unknown[]) => [...edges, edge],
    applyEdgeChanges: (_changes: unknown, edges: unknown[]) => edges,
    applyNodeChanges: (_changes: unknown, nodes: unknown[]) => nodes,
  };
});

function installDialogMethods() {
  HTMLDialogElement.prototype.showModal = vi.fn(function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  });
  HTMLDialogElement.prototype.close = vi.fn(function close(this: HTMLDialogElement) {
    this.removeAttribute("open");
  });
}

const definition = {
  program_type: "software_delivery",
  definition_version: "1.0.0",
  display_name: "Software delivery",
  startable_nodes: ["prd"],
  nodes: [
    {
      id: "prd",
      default_workflow_type: "prd_request",
      workflow_variants: [],
      required_artifacts: [],
      produces_artifacts: ["approved_prd"],
      input_bindings: [],
      completion_adapter: "extract_prd_outputs",
      terminal: true,
    },
  ],
  edges: [],
};

const draft = {
  id: "draft-1",
  program_type: "software_delivery",
  status: "draft",
  revision: "rev-1",
  definition,
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
    if (url === "/admin/program-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/program-definitions/catalog") {
      return jsonResponse({
        workflows: [],
        adapters: [],
        transition_modes: ["manual", "automatic"],
        templates: [],
      });
    }
    if (url === "/admin/program-definitions/drafts/draft-1") {
      if ((init?.method || "GET") === "PUT") return jsonResponse({ draft: { ...draft, revision: "rev-2" } });
      return jsonResponse({ draft });
    }
    if (url === "/admin/program-definitions/drafts/draft-1/validate") {
      return jsonResponse({ valid: true, issues: [], normalized_definition: definition });
    }
    if (url === "/admin/program-definitions/drafts/draft-1/activate") {
      return jsonResponse({
        active: { program_type: "software_delivery", definition_version: "1.0.0", revision: "active-rev" },
      });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(
    <ProgramDefinitionBuilderPage route={{ kind: "draft", draftId: "draft-1" }} onNavigate={() => undefined} />
  );

  expect(await screen.findByText("Software delivery")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /save draft/i }));
  await userEvent.click(screen.getByRole("button", { name: /validate/i }));
  await userEvent.click(screen.getByRole("button", { name: /activate/i }));
  await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: /activate draft/i }));

  await waitFor(() =>
    expect(calls).toContainEqual(
      expect.objectContaining({
        url: "/admin/program-definitions/drafts/draft-1",
        method: "PUT",
        body: expect.objectContaining({ expected_revision: "rev-1" }),
      })
    )
  );
  expect(calls).toContainEqual(
    expect.objectContaining({
      url: "/admin/program-definitions/drafts/draft-1/activate",
      method: "POST",
      body: expect.objectContaining({ expected_revision: "rev-2" }),
    })
  );
});

it("persists and reloads canvas layout from draft editor metadata", async () => {
  const calls: Array<{ url: string; method: string; body?: any }> = [];
  const positionedDraft = {
    ...draft,
    editor_metadata: {
      layout: { prd: { x: 321, y: 654 } },
      viewport: { x: 10, y: 20, zoom: 0.75 },
    },
  };
  mockFetch((url, init) => {
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, method: init?.method || "GET", body });
    if (url === "/admin/program-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/program-definitions/catalog") {
      return jsonResponse({
        workflows: [],
        adapters: [],
        transition_modes: ["manual", "automatic"],
        templates: [],
      });
    }
    if (url === "/admin/program-definitions/drafts/draft-1") {
      if ((init?.method || "GET") === "PUT") {
        return jsonResponse({ draft: { ...positionedDraft, revision: "rev-2" } });
      }
      return jsonResponse({ draft: positionedDraft });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(
    <ProgramDefinitionBuilderPage route={{ kind: "draft", draftId: "draft-1" }} onNavigate={() => undefined} />
  );

  expect(await screen.findByText("Software delivery")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /save draft/i }));

  await waitFor(() =>
    expect(calls).toContainEqual(
      expect.objectContaining({
        url: "/admin/program-definitions/drafts/draft-1",
        method: "PUT",
        body: expect.objectContaining({
          definition: expect.not.objectContaining({ "x-builder-layout": expect.anything() }),
          editor_metadata: {
            layout: { prd: { x: 321, y: 654 } },
            viewport: { x: 10, y: 20, zoom: 0.75 },
          },
        }),
      })
    )
  );
});

it("navigates to the created draft when edits continue during the first save", async () => {
  const onNavigate = vi.fn();
  const allowDirtyNavigation = vi.fn();
  const save = deferredResponse();
  const calls: Array<{ url: string; method: string; body?: any }> = [];
  mockFetch((url, init) => {
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, method: init?.method || "GET", body });
    if (url === "/admin/program-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/program-definitions/catalog") {
      return jsonResponse({
        workflows: [],
        adapters: [],
        transition_modes: ["manual", "automatic"],
        templates: [],
      });
    }
    if (url === "/admin/program-definitions/drafts" && init?.method === "POST") return save.promise;
    if (url === "/admin/program-definitions/drafts/created-1" && (init?.method || "GET") === "PUT") {
      return jsonResponse({
        draft: { ...draft, id: "created-1", revision: "rev-2", definition: body.definition },
      });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  const view = renderPage(
    <ProgramDefinitionBuilderPage
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
          definition: { ...definition, display_name: "New program", program_type: "new_program" },
        },
      })
    );
    await save.promise;
  });

  expect(allowDirtyNavigation).toHaveBeenCalled();
  expect(onNavigate).toHaveBeenCalledWith({ kind: "draft", draftId: "created-1" });
  expect(screen.getByLabelText("Display name")).toHaveValue("Edited while saving");

  view.rerender(
    <ProgramDefinitionBuilderPage
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
        url: "/admin/program-definitions/drafts/created-1",
        method: "PUT",
        body: expect.objectContaining({
          expected_revision: "rev-1",
          definition: expect.objectContaining({ display_name: "Edited while saving" }),
        }),
      })
    )
  );
});

it("ignores a stale new-draft save response after the route changes", async () => {
  const onNavigate = vi.fn();
  const save = deferredResponse();
  mockFetch((url, init) => {
    if (url === "/admin/program-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/program-definitions/catalog") {
      return jsonResponse({
        workflows: [],
        adapters: [],
        transition_modes: ["manual", "automatic"],
        templates: [],
      });
    }
    if (url === "/admin/program-definitions/drafts" && init?.method === "POST") return save.promise;
    throw new Error(`Unexpected URL ${url}`);
  });

  const view = renderPage(
    <ProgramDefinitionBuilderPage route={{ kind: "new" }} onNavigate={onNavigate} />
  );

  await userEvent.click(await screen.findByRole("button", { name: /save draft/i }));
  view.rerender(
    <ProgramDefinitionBuilderPage route={{ kind: "library" }} onNavigate={onNavigate} />
  );
  await act(async () => {
    save.resolve(jsonResponse({ draft: { ...draft, id: "late-draft" } }));
    await save.promise;
  });

  expect(onNavigate).not.toHaveBeenCalled();
});

it("shows an activation warning when runtime reload fails", async () => {
  const onNavigate = vi.fn();
  mockFetch((url, init) => {
    if (url === "/admin/program-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/program-definitions/catalog") {
      return jsonResponse({
        workflows: [],
        adapters: [],
        transition_modes: ["manual", "automatic"],
        templates: [],
      });
    }
    if (url === "/admin/program-definitions/drafts/draft-1") return jsonResponse({ draft });
    if (url === "/admin/program-definitions/drafts/draft-1/validate") {
      return jsonResponse({ valid: true, issues: [], normalized_definition: definition });
    }
    if (url === "/admin/program-definitions/drafts/draft-1/activate" && init?.method === "POST") {
      return jsonResponse({
        active: { program_type: "software_delivery", definition_version: "1.0.0", revision: "active-rev" },
        reload_ok: false,
        warning: "program definition activated but reload failed",
      });
    }
    if (url === "/admin/program-definitions/software_delivery") {
      return jsonResponse({ status: "active", read_only: false, definition });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  const view = renderPage(
    <ProgramDefinitionBuilderPage route={{ kind: "draft", draftId: "draft-1" }} onNavigate={onNavigate} />
  );

  await userEvent.click(await screen.findByRole("button", { name: /validate/i }));
  await waitFor(() => expect(screen.getByRole("button", { name: /activate/i })).toBeEnabled());
  await userEvent.click(screen.getByRole("button", { name: /activate/i }));
  await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: /activate draft/i }));

  await waitFor(() =>
    expect(onNavigate).toHaveBeenCalledWith({
      kind: "program",
      programType: "software_delivery",
    })
  );
  view.rerender(
    <ProgramDefinitionBuilderPage
      route={{ kind: "program", programType: "software_delivery" }}
      onNavigate={onNavigate}
    />
  );
  expect(await screen.findByText(/program definition activated but reload failed/i)).toBeInTheDocument();
});

it("clones built-in rows into unique drafts and preserves editor metadata for draft clones", async () => {
  const onNavigate = vi.fn();
  const calls: Array<{ url: string; method: string; body?: any }> = [];
  const positionedDraft = {
    ...draft,
    program_type: "software_delivery_custom",
    definition: {
      ...definition,
      program_type: "software_delivery_custom",
      display_name: "Software delivery draft",
    },
    editor_metadata: { layout: { prd: { x: 321, y: 654 } } },
  };
  mockFetch((url, init) => {
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, method: init?.method || "GET", body });
    if (url === "/admin/program-definitions") {
      return jsonResponse({
        definitions: [
          {
            program_type: "software_delivery",
            definition_version: "1.0.0",
            display_name: "Software delivery",
            status: "built_in",
            read_only: true,
          },
          {
            id: "draft-1",
            program_type: "software_delivery_custom",
            definition_version: "0.1.0",
            display_name: "Software delivery draft",
            status: "draft",
            revision: "rev-1",
          },
        ],
      });
    }
    if (url === "/admin/program-definitions/catalog") {
      return jsonResponse({
        workflows: [],
        adapters: [],
        transition_modes: ["manual", "automatic"],
        templates: [],
      });
    }
    if (url === "/admin/program-definitions/software_delivery") {
      return jsonResponse({ definition });
    }
    if (url === "/admin/program-definitions/drafts/draft-1") {
      return jsonResponse({ draft: positionedDraft });
    }
    if (url === "/admin/program-definitions/drafts") {
      return jsonResponse({ draft: { ...positionedDraft, id: "draft-copy", definition: body.definition } });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(<ProgramDefinitionBuilderPage route={{ kind: "library" }} onNavigate={onNavigate} />);

  await userEvent.click(await screen.findByRole("button", { name: /clone software_delivery_custom/i }));

  await waitFor(() =>
    expect(calls).toContainEqual(
      expect.objectContaining({
        url: "/admin/program-definitions/drafts",
        method: "POST",
        body: {
          definition: expect.objectContaining({
            program_type: "software_delivery_custom_custom",
            definition_version: "0.1.0",
            display_name: "Software delivery draft copy",
          }),
          editor_metadata: positionedDraft.editor_metadata,
        },
      })
    )
  );
  expect(onNavigate).toHaveBeenCalledWith({ kind: "draft", draftId: "draft-copy" });
});

it("keeps activation disabled until validation succeeds and after edits", async () => {
  mockFetch((url) => {
    if (url === "/admin/program-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/program-definitions/catalog") {
      return jsonResponse({
        workflows: [],
        adapters: [],
        transition_modes: ["manual", "automatic"],
        templates: [],
      });
    }
    if (url === "/admin/program-definitions/drafts/draft-1") return jsonResponse({ draft });
    if (url === "/admin/program-definitions/drafts/draft-1/validate") {
      return jsonResponse({ valid: true, issues: [], normalized_definition: definition });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(
    <ProgramDefinitionBuilderPage route={{ kind: "draft", draftId: "draft-1" }} onNavigate={() => undefined} />
  );

  const activate = await screen.findByRole("button", { name: /activate/i });
  expect(activate).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: /validate/i }));
  await waitFor(() => expect(activate).toBeEnabled());

  await userEvent.clear(screen.getByLabelText("Program type"));
  await userEvent.type(screen.getByLabelText("Program type"), "software_delivery_v2");
  expect(activate).toBeDisabled();
});

it("restores dirty state when clone fails after discard confirmation", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  mockFetch((url, init) => {
    if (url === "/admin/program-definitions") {
      return jsonResponse({
        definitions: [
          {
            program_type: "software_delivery",
            definition_version: "1.0.0",
            display_name: "Software delivery",
            status: "built_in",
          },
        ],
      });
    }
    if (url === "/admin/program-definitions/catalog") {
      return jsonResponse({
        workflows: [],
        adapters: [],
        transition_modes: ["manual", "automatic"],
        templates: [],
      });
    }
    if (url === "/admin/program-definitions/drafts/draft-1") return jsonResponse({ draft });
    if (url === "/admin/program-definitions/software_delivery") {
      return jsonResponse({ definition });
    }
    if (url === "/admin/program-definitions/drafts" && init?.method === "POST") {
      return Promise.reject(new Error("clone failed"));
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(
    <ProgramDefinitionBuilderPage route={{ kind: "draft", draftId: "draft-1" }} onNavigate={() => undefined} />
  );
  await userEvent.clear(await screen.findByLabelText("Display name"));
  await userEvent.type(screen.getByLabelText("Display name"), "Dirty draft");
  await userEvent.click(screen.getByRole("button", { name: /clone software_delivery/i }));

  expect(confirm).toHaveBeenCalled();
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/clone failed/i));
  expect(screen.getByLabelText("Display name")).toHaveValue("Dirty draft");
});

it("keeps newer local edits when a save response arrives late", async () => {
  const save = deferredResponse();
  mockFetch((url, init) => {
    if (url === "/admin/program-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/program-definitions/catalog") {
      return jsonResponse({
        workflows: [],
        adapters: [],
        transition_modes: ["manual", "automatic"],
        templates: [],
      });
    }
    if (url === "/admin/program-definitions/drafts/draft-1") {
      if ((init?.method || "GET") === "PUT") return save.promise;
      return jsonResponse({ draft });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(
    <ProgramDefinitionBuilderPage route={{ kind: "draft", draftId: "draft-1" }} onNavigate={() => undefined} />
  );

  expect(await screen.findByRole("heading", { name: "Software delivery" })).toBeInTheDocument();
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

it("focuses validation issues that use JSONPath or JSON pointer node paths", async () => {
  const twoNodeDraft = {
    ...draft,
    definition: {
      ...definition,
      startable_nodes: ["prd", "deploy"],
      nodes: [
        definition.nodes[0],
        {
          id: "deploy",
          default_workflow_type: "deploy_release",
          workflow_variants: [],
          required_artifacts: [],
          produces_artifacts: [],
          input_bindings: [],
          completion_adapter: "extract_deploy_outputs",
          terminal: true,
        },
      ],
    },
  };
  mockFetch((url) => {
    if (url === "/admin/program-definitions") return jsonResponse({ definitions: [] });
    if (url === "/admin/program-definitions/catalog") {
      return jsonResponse({
        workflows: [],
        adapters: [],
        transition_modes: ["manual", "automatic"],
        templates: [],
      });
    }
    if (url === "/admin/program-definitions/drafts/draft-1") return jsonResponse({ draft: twoNodeDraft });
    if (url === "/admin/program-definitions/drafts/draft-1/validate") {
      return jsonResponse({
        valid: false,
        issues: [
          {
            severity: "error",
            code: "missing_adapter",
            path: "$.nodes[1].completion_adapter",
            message: "Deploy adapter is required.",
          },
          {
            severity: "error",
            code: "missing_adapter",
            path: "/nodes/0/completion_adapter",
            message: "PRD adapter is required.",
          },
        ],
      });
    }
    throw new Error(`Unexpected URL ${url}`);
  });

  renderPage(
    <ProgramDefinitionBuilderPage route={{ kind: "draft", draftId: "draft-1" }} onNavigate={() => undefined} />
  );

  await waitFor(() => expect(screen.getByLabelText("Node id")).toHaveValue("prd"));
  await userEvent.click(screen.getByRole("button", { name: /validate/i }));
  await userEvent.click((await screen.findAllByRole("button", { name: /focus/i }))[0]);
  expect(screen.getByLabelText("Node id")).toHaveValue("deploy");
  await userEvent.click(screen.getAllByRole("button", { name: /focus/i })[1]);
  expect(screen.getByLabelText("Node id")).toHaveValue("prd");
});
