import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { renderPage } from "../../test/render";
import { IdentityForm } from "./IdentityForm";

describe("IdentityForm", () => {
  it("does not remove inherited Slack rows and shows validate errors from parent", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderPage(
      <IdentityForm
        merged={{
          identity: { slack_user_emails: { U1: "yaml@example.com" }, jira_account_ids: {} },
        }}
        sources={{ "identity.slack_user_emails": "inherited", "identity.jira_account_ids": "overlay" }}
        definition={{
          definition_type: "runtime_settings",
          definition_version: "1.0.0",
          identity: { jira_account_ids: { "ada@example.com": "acct-1" } },
        }}
        onChange={onChange}
      />
    );
    expect(screen.getByText("U1")).toBeInTheDocument();
    expect(screen.queryAllByRole("button", { name: "Remove" })).toHaveLength(1);
    await user.type(screen.getByLabelText("Jira email"), "ada@example.com");
    expect(screen.getByLabelText("Jira email")).toHaveValue("ada@example.com");
  });

  it("shows Remove after adding a Slack mapping to an inherited map", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const { rerender } = renderPage(
      <IdentityForm
        merged={{
          identity: { slack_user_emails: { U1: "yaml@example.com" }, jira_account_ids: {} },
        }}
        sources={{ "identity.slack_user_emails": "inherited", "identity.jira_account_ids": "inherited" }}
        definition={{
          definition_type: "runtime_settings",
          definition_version: "1.0.0",
        }}
        onChange={onChange}
      />
    );
    expect(screen.queryByRole("button", { name: "Remove" })).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("Slack user id"), "U2");
    await user.type(screen.getByLabelText("Slack email"), "Ada@Example.com");
    await user.click(screen.getAllByRole("button", { name: "Add" })[0]);
    const next = onChange.mock.calls.at(-1)?.[0];
    expect(next.identity.slack_user_emails.U2).toBe("ada@example.com");
    rerender(
      <IdentityForm
        merged={{
          identity: { slack_user_emails: { U1: "yaml@example.com" }, jira_account_ids: {} },
        }}
        sources={{ "identity.slack_user_emails": "inherited", "identity.jira_account_ids": "inherited" }}
        definition={next}
        onChange={onChange}
      />
    );
    expect(screen.getByText("Source: overlay")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Remove" }).length).toBeGreaterThan(0);
  });

  it("maps a GitHub login and roles with fields, not JSON", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderPage(
      <IdentityForm
        merged={{ identity: { github_logins: { "old@example.com": "old-gh" } } }}
        sources={{}}
        definition={{ definition_type: "runtime_settings", definition_version: "1.0.0" }}
        onChange={onChange}
      />
    );
    expect(screen.queryByRole("textbox", { name: "github_logins" })).not.toBeInTheDocument();
    expect(screen.getByText("old-gh")).toBeInTheDocument();
    await user.type(screen.getByLabelText("GitHub email"), "Ada@Example.com");
    await user.type(screen.getByLabelText("GitHub login"), "ada-gh");
    await user.click(screen.getAllByRole("button", { name: "Add" })[2]);
    expect(onChange.mock.calls.at(-1)?.[0].identity.github_logins).toEqual({
      "old@example.com": "old-gh",
      "ada@example.com": "ada-gh",
    });
    await user.type(screen.getByLabelText("Role email"), "ada@example.com");
    await user.type(screen.getByLabelText("Roles"), "admin, approver");
    await user.click(screen.getAllByRole("button", { name: "Add" })[3]);
    expect(onChange.mock.calls.at(-1)?.[0].identity.roles).toEqual({ "ada@example.com": ["admin", "approver"] });
  });

  it("asks for both fields instead of saving half a mapping", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderPage(
      <IdentityForm
        merged={{ identity: {} }}
        sources={{}}
        definition={{ definition_type: "runtime_settings", definition_version: "1.0.0" }}
        onChange={onChange}
      />
    );
    await user.type(screen.getByLabelText("GitHub email"), "ada@example.com");
    await user.click(screen.getAllByRole("button", { name: "Add" })[2]);
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("Enter both github email and github login.");
  });

  it("adds service permissions as a list", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderPage(
      <IdentityForm
        merged={{ identity: { service_permissions: ["read"] } }}
        sources={{}}
        definition={{ definition_type: "runtime_settings", definition_version: "1.0.0" }}
        onChange={onChange}
      />
    );
    await user.type(screen.getByLabelText("Permission"), "Approve_Gate");
    await user.click(screen.getAllByRole("button", { name: "Add" }).at(-1)!);
    expect(onChange.mock.calls.at(-1)?.[0].identity.service_permissions).toEqual(["read", "approve_gate"]);
  });
});
