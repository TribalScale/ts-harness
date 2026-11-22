import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import type { HistoryItem, HistoryResponse } from "../api/types";
import { StatusPill, type SemanticStatus } from "../components/StatusPill";
import { Timeline, formatDuration } from "./Timeline";

export const OUTCOME_LABELS: Record<string, string> = {
  merged: "Merged",
  accepted: "Accepted",
  stopped: "Stopped",
  cancelled: "Cancelled",
  failed: "Failed",
  replaced: "Replaced by a later run",
  done: "Done",
};

export const OUTCOME_STATUS: Record<string, SemanticStatus> = {
  merged: "success",
  accepted: "success",
  done: "success",
  stopped: "danger",
  failed: "danger",
  cancelled: "muted",
  replaced: "muted",
};

function finishedLabel(iso?: string): string {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString();
}

function HistoryRow({ item }: { item: HistoryItem }) {
  const [open, setOpen] = useState(false);
  const title = item.jira_issue_key || item.label;
  const outcome = item.outcome || "done";
  return (
    <li className="history-row">
      <div className="history-row-head">
        <strong>{title}</strong>
        <StatusPill status={OUTCOME_STATUS[outcome] || "neutral"} label={OUTCOME_LABELS[outcome] || outcome} />
        {item.decided_by && <span className="muted">by {item.decided_by}</span>}
      </div>
      <div className="muted history-row-meta">
        {item.program_label}
        {item.repo ? ` · ${item.repo}` : ""}
        {item.finished_at ? ` · finished ${finishedLabel(item.finished_at)}` : ""}
      </div>
      <div className="history-row-facts">
        <span>Took {formatDuration(item.elapsed_seconds)}</span>
        <span>{item.revisions} revisions{item.stronger_model ? " (stronger model)" : ""}</span>
        {item.pr_url && (
          <a href={item.pr_url} target="_blank" rel="noreferrer">
            Pull request
          </a>
        )}
        {item.program_id && <a href={`#/programs/${item.program_id}`}>Program</a>}
        <button type="button" className="link-button" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? "Hide timeline" : "Timeline"}
        </button>
      </div>
      {open && <Timeline bars={item.timeline} label={`${title} timeline`} />}
    </li>
  );
}

/** Finished tickets and work orders across programs, filterable and searchable. */
export function HistoryView() {
  const [days, setDays] = useState("14");
  const [project, setProject] = useState("");
  const [repo, setRepo] = useState("");
  const [outcome, setOutcome] = useState("");
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [data, setData] = useState<HistoryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => setSearch(query.trim()), 300);
    return () => window.clearTimeout(timer);
  }, [query]);

  const load = useCallback(async () => {
    const params = new URLSearchParams({ days });
    if (project) params.set("project", project);
    if (repo) params.set("repo", repo);
    if (outcome) params.set("outcome", outcome);
    if (search) params.set("q", search);
    try {
      setData(await apiFetch<HistoryResponse>(`/admin/history?${params.toString()}`));
      setError(null);
    } catch (e: any) {
      setError(String(e?.message || e));
    }
  }, [days, project, repo, outcome, search]);

  useEffect(() => {
    void load();
  }, [load]);

  const facets = data?.facets || { projects: [], repos: [], outcomes: [] };
  return (
    <section aria-label="History">
      <div className="history-filters">
        <label>
          <span>Finished in</span>
          <select aria-label="Finished in" value={days} onChange={(e) => setDays(e.target.value)}>
            <option value="7">the last 7 days</option>
            <option value="14">the last 14 days</option>
            <option value="30">the last 30 days</option>
            <option value="90">the last 90 days</option>
          </select>
        </label>
        <label>
          <span>Project</span>
          <select aria-label="Project" value={project} onChange={(e) => setProject(e.target.value)}>
            <option value="">All</option>
            {facets.projects.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Repository</span>
          <select aria-label="Repository" value={repo} onChange={(e) => setRepo(e.target.value)}>
            <option value="">All</option>
            {facets.repos.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Outcome</span>
          <select aria-label="Outcome" value={outcome} onChange={(e) => setOutcome(e.target.value)}>
            <option value="">All</option>
            {Object.keys(OUTCOME_LABELS).map((o) => (
              <option key={o} value={o}>
                {OUTCOME_LABELS[o]}
              </option>
            ))}
          </select>
        </label>
        <label className="history-search">
          <span>Search</span>
          <input
            aria-label="Search history"
            type="search"
            placeholder="Ticket key, title, program or repository"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
      </div>
      {error && <div className="error-banner" role="alert">{error}</div>}
      {data && (
        <p className="muted">
          {data.total} finished {data.total === 1 ? "run" : "runs"}
          {data.total > data.items.length ? `, showing the newest ${data.items.length}` : ""}.
        </p>
      )}
      <ul className="history-list" aria-label="Finished runs">
        {(data?.items || []).map((item) => (
          <HistoryRow key={item.workflow_id} item={item} />
        ))}
      </ul>
    </section>
  );
}
