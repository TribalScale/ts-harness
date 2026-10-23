import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import type { GitHubOverview } from "../api/types";
import { Button } from "../components/Button";
import { PageState } from "../components/PageState";
import { StatusPill } from "../components/StatusPill";

function when(value?: string | null) {
  return value ? new Date(value).toLocaleString() : "Never";
}

export function GitHubPage() {
  const [data, setData] = useState<GitHubOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [syncing, setSyncing] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await apiFetch<GitHubOverview>("/admin/github"));
      setError(null);
    } catch (e: any) {
      setError(String(e.message || e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function sync(host: string) {
    setSyncing(host);
    try {
      await apiFetch("/admin/github/sync", {
        method: "POST",
        body: JSON.stringify({ host }),
      });
      await load();
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setSyncing(null);
    }
  }

  if (!data && !error)
    return <PageState kind="loading" title="Loading GitHub" />;
  if (!data)
    return (
      <PageState
        kind="error"
        title="Could not load GitHub"
        detail={error || ""}
        onRetry={() => void load()}
      />
    );

  const webhookFor = (host: string) =>
    data.webhooks.find((w) => w.host === host);
  const limitFor = (host: string) =>
    data.rate_limits.find((r) => r.host === host);

  return (
    <section aria-label="GitHub" className="github-page">
      <div className="system-header">
        <div>
          <h3>GitHub App</h3>
          <p className="muted">
            Coding runs clone, push and open pull requests with short-lived
            tokens for one repository at a time.
            {data.pat_fallback
              ? " Repositories without an installation use the personal access token."
              : ""}
          </p>
        </div>
      </div>
      {error && (
        <p className="field-error" role="alert">
          {error}
        </p>
      )}
      <div className="cron-table-wrap">
        <table className="cron-table">
          <thead>
            <tr>
              <th>Host</th>
              <th>App</th>
              <th>Webhooks</th>
              <th>Rate limit</th>
              <th aria-label="Actions" />
            </tr>
          </thead>
          <tbody>
            {data.apps.map((app) => {
              const hook = webhookFor(app.host);
              const limit = limitFor(app.host);
              return (
                <tr key={app.host}>
                  <td>{app.host}</td>
                  <td>
                    {app.configured ? (
                      <StatusPill
                        status="success"
                        label={`${app.app_slug || "App"} (${app.app_id})`}
                      />
                    ) : (
                      <StatusPill status="muted" label="Not configured" />
                    )}
                  </td>
                  <td>
                    {hook ? (
                      <span>
                        Last delivery {when(hook.last_received_at)} ·{" "}
                        {hook.last_day} today
                      </span>
                    ) : (
                      <span className="muted">No deliveries</span>
                    )}
                  </td>
                  <td>
                    {limit ? (
                      <span>
                        {limit.remaining} / {limit.limit}
                      </span>
                    ) : (
                      <span className="muted">Not observed</span>
                    )}
                  </td>
                  <td className="actions">
                    {app.install_url && (
                      <a href={app.install_url} target="_blank" rel="noreferrer">
                        Install on an organization
                      </a>
                    )}
                    {app.configured && data.can_sync && (
                      <Button
                        variant="secondary"
                        onClick={() => void sync(app.host)}
                        disabled={syncing !== null}
                      >
                        {syncing === app.host ? "Syncing…" : "Sync installations"}
                      </Button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <h3>Installations</h3>
      {data.installations.length === 0 ? (
        <PageState
          kind="empty"
          title="No installations"
          detail="Install the App on an organization to give coding runs access to its repositories."
        />
      ) : (
        <div className="cron-table-wrap">
          <table className="cron-table">
            <thead>
              <tr>
                <th>Account</th>
                <th>Host</th>
                <th>Repositories</th>
                <th>Status</th>
                <th>Synced</th>
              </tr>
            </thead>
            <tbody>
              {data.installations.map((inst) => (
                <tr key={`${inst.host}-${inst.installation_id}`}>
                  <td>{inst.account_login}</td>
                  <td>{inst.host}</td>
                  <td>
                    {inst.repository_selection === "all"
                      ? `All (${inst.repositories.length})`
                      : inst.repositories.join(", ") || "None"}
                  </td>
                  <td>
                    {inst.suspended ? (
                      <StatusPill status="danger" label="Suspended" />
                    ) : (
                      <StatusPill status="success" label="Active" />
                    )}
                  </td>
                  <td>{when(inst.synced_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
