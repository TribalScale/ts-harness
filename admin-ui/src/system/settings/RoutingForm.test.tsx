import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderPage } from "../../test/render";
import { RoutingForm } from "./RoutingForm";

const routing = {
  status: "active",
  definition: {
    definition_type: "channel_routing",
    definition_version: "1.0.0",
    intents: [],
  },
  yaml_intents: [
    {
      id: "governed_prd",
      signals: ["create a prd"],
      workflow_type: "prd_request",
    },
  ],
  merged_intents: [
    {
      id: "governed_prd",
      signals: ["create a prd"],
      workflow_type: "prd_request",
    },
  ],
};

function mockCatalogs() {
  mockFetch((url) => {
    if (url === "/admin/program-definitions") {
      return jsonResponse({
        definitions: [{ program_type: "software_delivery", status: "active", display_name: "Software" }],
      });
    }
    if (url === "/admin/program-definitions/catalog") {
      return jsonResponse({
        workflows: [{ workflow_type: "prd_request" }],
        adapters: [],
        transition_modes: [],
        templates: [{ program_type: "rfp_response", status: "built_in" }],
      });
    }
    if (url === "/admin/workflow-definitions/catalog") {
      return jsonResponse({
        hooks: [],
        agents: [{ id: "dev-agent", label: "Dev" }],
        skills: [],
        stage_kinds: [],
        templates: [],
      });
    }
    return jsonResponse({});
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("RoutingForm", () => {
  it("does not delete inherited intents and can reset an override", async () => {
    mockCatalogs();
    const user = userEvent.setup();
    const onChange = vi.fn();
    const { rerender } = renderPage(
      <RoutingForm
        routing={routing}
        draft={{
          id: "r1",
          revision: "1",
          definition: { definition_type: "channel_routing", definition_version: "1.0.0", intents: [] },
        }}
        onChange={onChange}
      />
    );
    expect(screen.getByText("Inherited")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "governed_prd" }));
    await user.type(screen.getByLabelText("Intent signals"), ", extra");
    expect(onChange).toHaveBeenCalled();
    const next = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    rerender(<RoutingForm routing={routing} draft={next} onChange={onChange} />);
    await user.click(screen.getByRole("button", { name: "Reset to inherited" }));
    const reset = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(reset.definition.intents).toEqual([]);
  });

  it("uses exclusive catalog selects and hides Reset on overlay-only intents", async () => {
    mockCatalogs();
    const user = userEvent.setup();
    const onChange = vi.fn();
    const overlayRouting = {
      ...routing,
      definition: { ...routing.definition, intents: [] },
      yaml_intents: [],
      merged_intents: [],
    };
    renderPage(
      <RoutingForm
        routing={overlayRouting}
        draft={{
          id: "r1",
          revision: "1",
          definition: {
            definition_type: "channel_routing",
            definition_version: "1.0.0",
            intents: [{ id: "new_intent", signals: ["start program"], program_type: "software_delivery" }],
          },
        }}
        onChange={onChange}
      />
    );
    expect(await screen.findByLabelText("Intent program type")).toBeInTheDocument();
    expect(screen.getByLabelText("Intent program type").tagName).toBe("SELECT");
    expect(screen.getByLabelText("Intent workflow type").tagName).toBe("SELECT");
    expect(screen.getByLabelText("Intent delegate agent").tagName).toBe("SELECT");
    expect(screen.queryByRole("button", { name: "Reset to inherited" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Intent id")).not.toBeDisabled();
    await waitFor(() => expect(screen.getByRole("option", { name: "software_delivery" })).toBeInTheDocument());
    expect(screen.getByRole("option", { name: "rfp_response" })).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Intent workflow type"), "prd_request");
    const next = onChange.mock.calls.at(-1)?.[0];
    expect(next.definition.intents[0].workflow_type).toBe("prd_request");
    expect(next.definition.intents[0].program_type).toBeUndefined();
    expect(next.definition.intents[0].delegate_agent).toBeUndefined();
  });

  it("edits deny_start_workflow_types on policy", async () => {
    mockCatalogs();
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderPage(
      <RoutingForm
        routing={routing}
        draft={{
          id: "r1",
          revision: "1",
          definition: { definition_type: "channel_routing", definition_version: "1.0.0", intents: [] },
        }}
        onChange={onChange}
      />
    );
    await user.click(screen.getByRole("button", { name: "governed_prd" }));
    fireEvent.change(screen.getByLabelText("Intent deny start workflow types"), {
      target: { value: "prd_request" },
    });
    const next = onChange.mock.calls.at(-1)?.[0];
    expect(next.definition.intents[0].policy.deny_start_workflow_types).toEqual(["prd_request"]);
  });
});
