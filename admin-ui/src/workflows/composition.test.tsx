import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { ChildWorkflowSection, FragmentIncludeSection } from "./CompositionSections";
import {
  addChildJoin,
  definitionToGraph,
  detectClientGraphIssues,
  findChildJoin,
  graphToDefinition,
  removeChildJoin,
  setChildJoinTimeout,
} from "./graph";
import { WorkflowBuilderPage } from "./WorkflowBuilderPage";
import type { WorkflowDefinition, WorkflowStage } from "./types";

const include: WorkflowStage = {
  name: "verified_merge",
  kind: "fragment",
  fragment: "verified_merge",
  version: "1.0.0",
  params: { implement_stage: "" },
  inputs: {},
  depends_on: ["deliver"],
};

const definition: WorkflowDefinition = {
  workflow_type: "ship",
  definition_version: "1.0.0",
  stages: [{ name: "deliver", kind: "task", agent_id: "dev-agent" }, include],
};

const fragmentDefinition = {
  fragment_id: "verified_merge",
  definition_version: "1.0.0",
  display_name: "Verified merge",
  params: { implement_stage: { kind: "stage_ref", required: true } },
  inputs: { repo_url: { required: true }, note: { required: false } },
  stages: [{ name: "await_pr_checks" }, { name: "qa_gate" }],
};

beforeEach(() => {
  vi.restoreAllMocks();
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

describe("fragment graph mapping", () => {
  it("maps fragment includes to fragment nodes and round-trips them", () => {
    const graph = definitionToGraph(definition);
    expect(graph.nodes.map((node) => node.type)).toEqual(["workflowStage", "workflowFragment"]);
    const back = graphToDefinition(graph, definition);
    expect(back.stages[1]).toEqual(include);
  });

  it("does not require an agent on fragment includes but requires id and version", () => {
    const graph = definitionToGraph(definition);
    expect(detectClientGraphIssues(graph)).toEqual([]);
    const broken = definitionToGraph({ ...definition, stages: [definition.stages[0], { ...include, version: "" }] });
    expect(detectClientGraphIssues(broken).map((issue) => issue.code)).toEqual(["missing_fragment"]);
  });
});

describe("child join helpers", () => {
  const forkDefinition: WorkflowDefinition = {
    workflow_type: "localize",
    definition_version: "1.0.0",
    stages: [
      {
        name: "fork",
        kind: "task",
        agent_id: "orchestrator",
        barrier: { hook: "fork_children", workflow_type: "translate" },
      },
    ],
  };

  it("adds, retimes and removes the join stage", () => {
    const graph = definitionToGraph(forkDefinition);
    const joined = addChildJoin(graph, "fork", 600);
    const join = findChildJoin(joined.nodes.map((node) => node.data), "fork");
    expect(join?.name).toBe("join_fork");
    expect(join?.wait_external).toEqual({
      hook: "wait_external",
      event_type: "workflow.child_terminal",
      children: { source: "$prior.fork.output.children" },
      timeout_seconds: 600,
    });
    expect(graphToDefinition(joined, forkDefinition).stages[1].depends_on).toEqual(["fork"]);
    const retimed = setChildJoinTimeout(joined, "fork", 60);
    expect(findChildJoin(retimed.nodes.map((node) => node.data), "fork")?.wait_external?.timeout_seconds).toBe(60);
    const removed = removeChildJoin(retimed, "fork");
    expect(removed.nodes.map((node) => node.id)).toEqual(["fork"]);
    expect(removed.edges).toEqual([]);
  });
});

describe("FragmentIncludeSection", () => {
  it("offers stage refs, marks required inputs and hints at upgrades", async () => {
    mockFetch((url) => {
      if (url === "/admin/workflow-fragments/verified_merge?version=1.0.0") {
        return jsonResponse({ status: "built_in", read_only: true, definition: fragmentDefinition });
      }
      if (url === "/admin/workflow-fragments") {
        return jsonResponse({ fragments: [{ fragment_id: "verified_merge", definition_version: "1.1.0", status: "built_in" }] });
      }
      throw new Error(`Unexpected URL ${url}`);
    });
    const onChange = vi.fn();
    renderPage(<FragmentIncludeSection stage={include} stageNames={["deliver"]} readOnly={false} onChange={onChange} />);

    const param = await screen.findByLabelText("Fragment param implement_stage");
    await userEvent.selectOptions(param, "deliver");
    expect(onChange).toHaveBeenLastCalledWith({ params: { implement_stage: "deliver" } });

    expect(screen.getByText("Input repo_url (required)")).toBeInTheDocument();
    expect(screen.getByText("Input note")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Fragment input repo_url"), "$");
    expect(onChange).toHaveBeenLastCalledWith({ inputs: { repo_url: "$" } });

    expect(await screen.findByText(/Version 1.1.0 is available/)).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Fragment version"), "1.1.0");
    expect(onChange).toHaveBeenLastCalledWith({ version: "1.1.0" });
  });
});

describe("ChildWorkflowSection", () => {
  const fork: WorkflowStage = {
    name: "fork",
    kind: "task",
    agent_id: "orchestrator",
    barrier: { hook: "fork_children" },
  };

  it("pre-fills the child's inputs and toggles the join", async () => {
    const onChange = vi.fn();
    const onJoinChange = vi.fn();
    renderPage(
      <ChildWorkflowSection
        stage={fork}
        childWorkflows={[{ workflow_type: "translate", active_version: "2.0.0", workflow_input_keys: ["locale"] }]}
        join={{ enabled: false }}
        readOnly={false}
        onChange={onChange}
        onJoinChange={onJoinChange}
      />
    );
    await userEvent.selectOptions(screen.getByLabelText("Child workflow"), "translate");
    expect(onChange).toHaveBeenLastCalledWith({
      barrier: { hook: "fork_children", workflow_type: "translate", input_map: { locale: "" } },
    });
    await userEvent.click(screen.getByLabelText("Wait for children"));
    expect(onJoinChange).toHaveBeenLastCalledWith({ enabled: true });
  });
});

describe("builder join toggle", () => {
  it("adds and removes the join stage from the inspector", async () => {
    const forkDraft = {
      id: "draft-fork",
      workflow_type: "localize",
      status: "draft",
      revision: "rev-1",
      definition: {
        workflow_type: "localize",
        definition_version: "1.0.0",
        display_name: "Localize",
        stages: [
          {
            name: "fork",
            kind: "task",
            agent_id: "orchestrator",
            barrier: { hook: "fork_children", workflow_type: "translate" },
          },
        ],
      },
    };
    mockFetch((url) => {
      if (url === "/admin/workflow-definitions") return jsonResponse({ definitions: [] });
      if (url === "/admin/workflow-definitions/catalog") {
        return jsonResponse({
          hooks: [],
          agents: [],
          skills: [],
          stage_kinds: ["task"],
          templates: [],
          child_workflows: [{ workflow_type: "translate", workflow_input_keys: [] }],
        });
      }
      if (url === "/admin/workflow-definitions/drafts/draft-fork") return jsonResponse({ draft: forkDraft });
      throw new Error(`Unexpected URL ${url}`);
    });
    renderPage(<WorkflowBuilderPage route={{ kind: "draft", draftId: "draft-fork" }} onNavigate={() => undefined} />);

    const toggle = await screen.findByLabelText("Wait for children");
    await userEvent.click(toggle);
    const stages = screen.getByRole("list", { name: "Workflow stages" });
    await waitFor(() => expect(within(stages).getByText(/join_fork/)).toBeInTheDocument());
    const json = () => (screen.getByLabelText("Current definition JSON") as HTMLTextAreaElement).value;
    expect(json()).toContain("$prior.fork.output.children");

    await userEvent.click(screen.getByLabelText("Wait for children"));
    await waitFor(() => expect(within(stages).queryByText(/join_fork/)).not.toBeInTheDocument());
    expect(json()).not.toContain("join_fork");
  });
});
