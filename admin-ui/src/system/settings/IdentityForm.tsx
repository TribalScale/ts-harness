import { useState } from "react";
import { Button } from "../../components/Button";
import { TextInput } from "@astryxdesign/core/TextInput";
import type { RuntimeSettingsDefinition } from "./types";

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function mapEntries(value: unknown): Array<[string, string]> {
  return Object.entries(asRecord(value)).map(([key, item]) => [key, String(item)]);
}

export function IdentityForm({
  merged,
  definition,
  onChange,
}: {
  merged: RuntimeSettingsDefinition | Record<string, unknown>;
  sources: Record<string, "inherited" | "overlay">;
  definition: RuntimeSettingsDefinition;
  onChange: (next: RuntimeSettingsDefinition) => void;
}) {
  const mergedIdentity = asRecord(asRecord(merged).identity);
  const slackOverlay = definition.identity?.slack_user_emails != null;
  const jiraOverlay = definition.identity?.jira_account_ids != null;
  const slack =
    definition.identity?.slack_user_emails ||
    (mergedIdentity.slack_user_emails as Record<string, string>) ||
    {};
  const jira =
    definition.identity?.jira_account_ids ||
    (mergedIdentity.jira_account_ids as Record<string, string>) ||
    {};
  const [slackId, setSlackId] = useState("");
  const [slackEmail, setSlackEmail] = useState("");
  const [jiraEmail, setJiraEmail] = useState("");
  const [accountId, setAccountId] = useState("");

  function patchIdentity(next: RuntimeSettingsDefinition["identity"]) {
    onChange({ ...definition, identity: { ...definition.identity, ...next } });
  }

  return (
    <div>
      <h3>Identity</h3>
      <section>
        <div className="system-header">
          <h4>Slack user → email</h4>
          <Button
            variant="secondary"
            size="sm"
            type="button"
            onClick={() => {
              const identity = { ...definition.identity };
              delete identity.slack_user_emails;
              onChange({ ...definition, identity });
            }}
          >
            Reset to inherited
          </Button>
        </div>
        <p className="muted">Source: {slackOverlay ? "overlay" : "inherited"}</p>
        <table>
          <thead>
            <tr>
              <th>Slack user id</th>
              <th>Email</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {mapEntries(slack).map(([key, value]) => (
              <tr key={key}>
                <td>{key}</td>
                <td>{value}</td>
                <td>
                  {slackOverlay && (
                    <Button
                      variant="ghost"
                      onClick={() => {
                        const next = { ...slack };
                        delete next[key];
                        patchIdentity({ slack_user_emails: next });
                      }}
                    >
                      Remove
                    </Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="form-pair">
          <div className="form-field">
            <TextInput
              label="Slack user id"
              placeholder="U01234567"
              value={slackId}
              onChange={(value) => setSlackId(value)}
            />
          </div>
          <div className="form-field">
            <TextInput
              label="Slack email"
              placeholder="name@company.com"
              value={slackEmail}
              onChange={(value) => setSlackEmail(value)}
            />
          </div>
          <Button
            variant="secondary"
            onClick={() => {
              if (!slackId.trim() || !slackEmail.trim()) return;
              patchIdentity({
                slack_user_emails: { ...slack, [slackId.trim()]: slackEmail.trim().toLowerCase() },
              });
              setSlackId("");
              setSlackEmail("");
            }}
          >
            Add
          </Button>
        </div>
      </section>
      <section>
        <div className="system-header">
          <h4>Email → Jira accountId</h4>
          <Button
            variant="secondary"
            size="sm"
            type="button"
            onClick={() => {
              const identity = { ...definition.identity };
              delete identity.jira_account_ids;
              onChange({ ...definition, identity });
            }}
          >
            Reset to inherited
          </Button>
        </div>
        <p className="muted">Source: {jiraOverlay ? "overlay" : "inherited"}</p>
        <table>
          <thead>
            <tr>
              <th>Email</th>
              <th>Jira accountId</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {mapEntries(jira).map(([key, value]) => (
              <tr key={key}>
                <td>{key}</td>
                <td>{value}</td>
                <td>
                  {jiraOverlay && (
                    <Button
                      variant="ghost"
                      onClick={() => {
                        const next = { ...jira };
                        delete next[key];
                        patchIdentity({ jira_account_ids: next });
                      }}
                    >
                      Remove
                    </Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="form-pair">
          <div className="form-field">
            <TextInput
              label="Jira email"
              placeholder="name@company.com"
              value={jiraEmail}
              onChange={(value) => setJiraEmail(value)}
            />
          </div>
          <div className="form-field">
            <TextInput
              label="Jira account id"
              placeholder="accountId"
              value={accountId}
              onChange={(value) => setAccountId(value)}
            />
          </div>
          <Button
            variant="secondary"
            onClick={() => {
              if (!jiraEmail.trim() || !accountId.trim()) return;
              patchIdentity({
                jira_account_ids: { ...jira, [jiraEmail.trim().toLowerCase()]: accountId.trim() },
              });
              setJiraEmail("");
              setAccountId("");
            }}
          >
            Add
          </Button>
        </div>
      </section>
    </div>
  );
}
