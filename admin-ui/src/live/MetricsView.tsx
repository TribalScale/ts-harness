import { Fragment, useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import type { MetricsResponse, MetricsTicket } from "../api/types";
import { OUTCOME_LABELS } from "./HistoryView";
import { TicketStory } from "./TicketStory";
import { formatDuration } from "./Timeline";

export const TRIGGER_LABELS: Record<string, string> = {
  code_review: "code review",
  qa: "QA",
  ci: "CI",
  guided: "guided",
  triage: "triage",
  human: "/revise",
  rerun: "re-run",
  other: "other",
};

function percent(value: number | null | undefined): string {
  return value === null || value === undefined ? "–" : `${Math.round(value * 100)}%`;
}

function dollars(value: number | null | undefined): string {
  return value === null || value === undefined ? "–" : `$${value.toFixed(2)}`;
}

function duration(value: number | null | undefined): string {
  return value === null || value === undefined ? "–" : formatDuration(value);
}

function Tile({ label, value, detail }: { label: string; value: string; detail?: string }) {
  return (
    <li className="metrics-tile">
      <span className="metrics-tile-label">{label}</span>
      <strong className="metrics-tile-value">{value}</strong>
      {detail && <span className="muted metrics-tile-detail">{detail}</span>}
    </li>
  );
}

function TicketRow({ ticket }: { ticket: MetricsTicket }) {
  const [open, setOpen] = useState(false);
  const title = ticket.jira_issue_key || ticket.label;
  const triggers = Object.entries(ticket.revisions_by_trigger)
    .map(([kind, count]) => `${count} ${TRIGGER_LABELS[kind] || kind}`)
    .join(", ");
  return (
    <Fragment>
      <tr>
        <td>
          <strong>{title}</strong>
          <div className="muted metrics-ticket-meta">
            {ticket.program_label}
            {ticket.repo ? ` · ${ticket.repo}` : ""}
          </div>
        </td>
        <td>{ticket.outcome === "running" ? "Running" : OUTCOME_LABELS[ticket.outcome] || ticket.outcome}</td>
        <td>
          {ticket.revisions}
          {triggers && <div className="muted metrics-ticket-meta">{triggers}</div>}
        </td>
        <td>{dollars(ticket.cost_usd)}</td>
        <td>{duration(ticket.time_to_merge_seconds)}</td>
        <td>{formatDuration(ticket.you_seconds)}</td>
        <td>
          {ticket.program_id && (
            <button type="button" className="link-button" aria-expanded={open} onClick={() => setOpen(!open)}>
              {open ? "Hide story" : "Story"}
            </button>
          )}
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={7}>
            <TicketStory programId={ticket.program_id} workflowId={ticket.workflow_id} />
          </td>
        </tr>
      )}
    </Fragment>
  );
}

/** Delivery quality: first-try pass rate, revisions, time to merge, review catch rate, gate disagreement, cost. */
export function MetricsView() {
  const [days, setDays] = useState("14");
  const [project, setProject] = useState("");
  const [repo, setRepo] = useState("");
  const [running, setRunning] = useState(false);
  const [data, setData] = useState<MetricsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const params = new URLSearchParams({ days });
    if (project) params.set("project", project);
    if (repo) params.set("repo", repo);
    if (running) params.set("running", "1");
    try {
      setData(await apiFetch<MetricsResponse>(`/admin/metrics?${params.toString()}`));
      setError(null);
    } catch (e: any) {
      setError(String(e?.message || e));
    }
  }, [days, project, repo, running]);

  useEffect(() => {
    void load();
  }, [load]);

  const facets = data?.facets || { projects: [], repos: [] };
  const s = data?.summary;
  const triggers = s
    ? Object.entries(s.revisions.by_trigger)
        .map(([kind, count]) => `${count} ${TRIGGER_LABELS[kind] || kind}`)
        .join(", ")
    : "";
  const weeks = data?.weekly || [];
  const maxRevisions = Math.max(1, ...weeks.map((w) => w.revisions_mean));
  return (
    <section aria-label="Metrics">
      <div className="history-filters">
        <label>
          <span>Period</span>
          <select aria-label="Period" value={days} onChange={(e) => setDays(e.target.value)}>
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
        <label className="metrics-running">
          <input type="checkbox" checked={running} onChange={(e) => setRunning(e.target.checked)} />
          <span>Include running tickets</span>
        </label>
      </div>
      {error && <div className="error-banner" role="alert">{error}</div>}
      {s && (
        <ul className="metrics-tiles" aria-label="Headline numbers">
          <Tile
            label="First-try pass"
            value={percent(s.first_try_rate)}
            detail={`${s.first_try} of ${s.finished} finished merged with no revision`}
          />
          <Tile
            label="Revisions per ticket"
            value={s.revisions.median === null ? "–" : String(s.revisions.median)}
            detail={`median · mean ${s.revisions.mean ?? "–"} · max ${s.revisions.max ?? "–"}${triggers ? ` · ${triggers}` : ""}`}
          />
          <Tile
            label="Time to merge"
            value={duration(s.time_to_merge_seconds.median)}
            detail={`median over ${s.merged} merged · waiting for a person ${percent(s.you_share)} of the time`}
          />
          <Tile
            label="Pre-push review catch rate"
            value={percent(s.review_catch_rate)}
            detail={`${s.review_fixed} blocking issues fixed over ${s.reviewed_runs} reviewed runs`}
          />
          <Tile
            label="Gate disagreement"
            value={percent(s.gate_disagreement_rate)}
            detail={`code review blocked ${s.gate_disagreed} of ${s.gate_compared} runs the pre-push review passed`}
          />
          <Tile
            label="Cost per ticket"
            value={dollars(s.cost_usd.median)}
            detail={`median · ${dollars(s.cost_usd.total)} total · ${percent(s.strong_cost_share)} on the stronger model`}
          />
          <Tile label="Lost coding jobs" value={String(s.lost_jobs)} detail={`of ${s.coding_runs} coding runs`} />
        </ul>
      )}
      {weeks.length > 1 && (
        <section className="metrics-trend" aria-label="Weekly trend">
          <h3>By week</h3>
          <table className="metrics-table">
            <thead>
              <tr>
                <th>Week of</th>
                <th>Tickets</th>
                <th>First-try pass</th>
                <th>Revisions per ticket (mean)</th>
              </tr>
            </thead>
            <tbody>
              {weeks.map((w) => (
                <tr key={w.week}>
                  <td>{w.week}</td>
                  <td>{w.tickets}</td>
                  <td>
                    <span className="metrics-bar" style={{ width: `${((w.first_try_rate || 0) * 6).toFixed(2)}rem` }} />
                    {percent(w.first_try_rate)}
                  </td>
                  <td>
                    <span className="metrics-bar metrics-bar-revisions" style={{ width: `${((w.revisions_mean / maxRevisions) * 6).toFixed(2)}rem` }} />
                    {w.revisions_mean}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
      {data && !data.tickets.length && <p className="muted">No tickets finished in this period.</p>}
      {data && data.tickets.length > 0 && (
        <table className="metrics-table" aria-label="Tickets by cost">
          <thead>
            <tr>
              <th>Ticket</th>
              <th>Outcome</th>
              <th>Revisions</th>
              <th>Cost</th>
              <th>Time to merge</th>
              <th>Waiting for you</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {data.tickets.map((ticket) => (
              <TicketRow key={ticket.workflow_id} ticket={ticket} />
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
