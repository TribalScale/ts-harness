import { useState } from "react";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Button } from "../../components/Button";
import type { RepoRow, RuntimeSettingsDefinition } from "./types";

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function overlayFlag(
  definition: RuntimeSettingsDefinition,
  key: "repo_picker" | "jira_filing" | "ticket_picker"
): boolean {
  return definition.program_defaults?.[key] != null;
}

export function JiraDefaultsForm({
  merged,
  definition,
  onChange,
}: {
  merged: RuntimeSettingsDefinition | Record<string, unknown>;
  sources: Record<string, "inherited" | "overlay">;
  definition: RuntimeSettingsDefinition;
  onChange: (next: RuntimeSettingsDefinition) => void;
}) {
  const mergedDefaults = asRecord(asRecord(merged).program_defaults);
  const mergedRepos = ((asRecord(mergedDefaults.repo_picker).repos as RepoRow[]) || []).slice();
  const overlayRepos = definition.program_defaults?.repo_picker?.repos;
  const repos = overlayRepos || mergedRepos;
  const filing = {
    default_issue_type:
      definition.program_defaults?.jira_filing?.default_issue_type ||
      String(asRecord(mergedDefaults.jira_filing).default_issue_type || "Story"),
    issue_type_by_project:
      definition.program_defaults?.jira_filing?.issue_type_by_project ||
      (asRecord(mergedDefaults.jira_filing).issue_type_by_project as Record<string, string>) ||
      {},
  };
  const statuses =
    definition.program_defaults?.ticket_picker?.statuses ||
    (asRecord(mergedDefaults.ticket_picker).statuses as string[]) ||
    [];
  const [repoId, setRepoId] = useState("");
  const [branch, setBranch] = useState("");
  const [statusText, setStatusText] = useState("");
  const [projectKey, setProjectKey] = useState("");
  const [projectType, setProjectType] = useState("");
  const repoOverlay = overlayFlag(definition, "repo_picker");
  const filingOverlay = overlayFlag(definition, "jira_filing");
  const pickerOverlay = overlayFlag(definition, "ticket_picker");

  function patchDefaults(next: RuntimeSettingsDefinition["program_defaults"]) {
    onChange({
      ...definition,
      program_defaults: { ...definition.program_defaults, ...next },
    });
  }

  return (
    <div>
      <h3>Jira defaults</h3>
      <section>
        <div className="system-header">
          <h4>Repo allowlist</h4>
          <Button
            variant="secondary"
            size="sm"
            type="button"
            onClick={() => {
              const defaults = { ...definition.program_defaults };
              delete defaults.repo_picker;
              onChange({ ...definition, program_defaults: defaults });
            }}
          >
            Reset to inherited
          </Button>
        </div>
        <p className="muted">Source: {repoOverlay ? "overlay" : "inherited"}</p>
        <table>
          <thead>
            <tr>
              <th>Repo</th>
              <th>Default branch</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {repos.map((row, index) => (
              <tr key={`${row.id}-${index}`}>
                <td>{row.id}</td>
                <td>{row.default_branch || "dev"}</td>
                <td>
                  {repoOverlay && (
                    <Button
                      variant="ghost"
                      onClick={() => {
                        const next = repos.filter((_, itemIndex) => itemIndex !== index);
                        patchDefaults({ repo_picker: { repos: next } });
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
              label="Repo id"
              placeholder="org/repo"
              value={repoId}
              onChange={(value) => setRepoId(value)}
            />
          </div>
          <div className="form-field">
            <TextInput
              label="Default branch"
              placeholder="dev"
              value={branch}
              onChange={(value) => setBranch(value)}
            />
          </div>
          <Button
            variant="secondary"
            onClick={() => {
              const id = repoId.trim();
              if (!id) return;
              patchDefaults({
                repo_picker: {
                  repos: [...repos, { id, default_branch: branch.trim() || "dev" }],
                },
              });
              setRepoId("");
              setBranch("");
            }}
          >
            Add repo
          </Button>
        </div>
      </section>
      <section>
        <div className="system-header">
          <h4>Jira filing</h4>
          <Button
            variant="secondary"
            size="sm"
            type="button"
            onClick={() => {
              const defaults = { ...definition.program_defaults };
              delete defaults.jira_filing;
              onChange({ ...definition, program_defaults: defaults });
            }}
          >
            Reset to inherited
          </Button>
        </div>
        <p className="muted">Source: {filingOverlay ? "overlay" : "inherited"}</p>
        <div className="form-field">
          <TextInput
            label="Default issue type"
            value={filing.default_issue_type}
            onChange={(value) =>
              patchDefaults({
                jira_filing: {
                  ...filing,
                  default_issue_type: value,
                },
              })
            }
          />
        </div>
        <table>
          <thead>
            <tr>
              <th>Project</th>
              <th>Type</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(filing.issue_type_by_project || {}).map(([key, value]) => (
              <tr key={key}>
                <td>{key}</td>
                <td>{value}</td>
                <td>
                  {filingOverlay && (
                    <Button
                      variant="ghost"
                      aria-label={`Remove project ${key}`}
                      onClick={() => {
                        const next = { ...filing.issue_type_by_project };
                        delete next[key];
                        patchDefaults({
                          jira_filing: { ...filing, issue_type_by_project: next },
                        });
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
              label="Filing project"
              value={projectKey}
              onChange={(value) => setProjectKey(value)}
            />
          </div>
          <div className="form-field">
            <TextInput
              label="Filing type"
              value={projectType}
              onChange={(value) => setProjectType(value)}
            />
          </div>
          <Button
            variant="secondary"
            onClick={() => {
              if (!projectKey.trim() || !projectType.trim()) return;
              patchDefaults({
                jira_filing: {
                  ...filing,
                  issue_type_by_project: {
                    ...filing.issue_type_by_project,
                    [projectKey.trim()]: projectType.trim(),
                  },
                },
              });
              setProjectKey("");
              setProjectType("");
            }}
          >
            Add project type
          </Button>
        </div>
      </section>
      <section>
        <div className="system-header">
          <h4>Ticket picker</h4>
          <Button
            variant="secondary"
            size="sm"
            type="button"
            onClick={() => {
              const defaults = { ...definition.program_defaults };
              delete defaults.ticket_picker;
              onChange({ ...definition, program_defaults: defaults });
            }}
          >
            Reset to inherited
          </Button>
        </div>
        <p className="muted">Source: {pickerOverlay ? "overlay" : "inherited"}</p>
        <ul className="status-chips">
          {statuses.map((status) => (
            <li key={status} className="status-chip">
              <span>{status}</span>
              {pickerOverlay && (
                <Button
                  variant="ghost"
                  aria-label={`Remove status ${status}`}
                  onClick={() =>
                    patchDefaults({
                      ticket_picker: {
                        assignee: "mapped_user",
                        statuses: statuses.filter((item) => item !== status),
                      },
                    })
                  }
                >
                  Remove
                </Button>
              )}
            </li>
          ))}
        </ul>
        {!statuses.length && (
          <p className="muted">To Do, Open, Ready for Dev, In Progress</p>
        )}
        <div className="form-pair-wide">
          <div className="form-field">
            <TextInput
              label="Add status"
              value={statusText}
              onChange={(value) => setStatusText(value)}
            />
          </div>
          <Button
            variant="secondary"
            onClick={() => {
              const next = statusText.trim();
              if (!next) return;
              patchDefaults({
                ticket_picker: { assignee: "mapped_user", statuses: [...statuses, next] },
              });
              setStatusText("");
            }}
          >
            Add status
          </Button>
        </div>
      </section>
    </div>
  );
}
