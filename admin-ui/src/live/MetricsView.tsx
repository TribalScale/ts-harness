import { Fragment, useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import type {
  HarnessLoopStatus,
  ImprovementBudgetStatus,
  MetricsGuardSummary,
  MetricsResponse,
  MetricsSummary,
  MetricsTicket,
} from "../api/types";
import { OUTCOME_LABELS } from "./HistoryView";
import { ImprovementsPanel } from "./ImprovementsPanel";
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

/** How a person stepped in on a ticket (see ts_harness.improvement.guard). */
export const INTERVENTION_LABELS: Record<string, string> = {
  decision_retry: "retry",
  decision_revise: "revise",
  decision_accept: "accept",
  decision_stop: "stop",
  jira_reply: "Jira reply",
  revise_grant: "/revise",
  pr_changes_requested: "PR changes requested",
  pr_closed: "PR closed",
  human_change_requests: "change requests",
  accepted_by_person: "accepted as is",
  stopped_by_person: "stopped",
};

const GUARD_LABELS: [keyof MetricsGuardSummary["signals"], string][] = [
  ["reverts", "reverts"],
  ["jira_bugs", "Jira bugs"],
  ["smoke_failures", "smoke failures"],
  ["arbiter_overruled", "arbiter overruled"],
];

function count(value: number | null | undefined): string {
  return value === null || value === undefined ? "unknown" : String(value);
}

function guardTile(guard: MetricsGuardSummary): { value: string; detail: string } {
  const signals = GUARD_LABELS.map(([key, label]) => {
    const signal = guard.signals[key];
    const partly = signal.count !== null && signal.unknown > 0 ? ` (${signal.unknown} unknown)` : "";
    return `${label} ${count(signal.count)}${partly}`;
  }).join(" · ");
  // Nothing could be checked (GitHub, Jira and the smoke record all unreadable): say so, not 0%.
  const nothingKnown = guard.merged > 0 && GUARD_LABELS.slice(0, 3).every(([key]) => guard.signals[key].count === null);
  return {
    value: nothingKnown ? "unknown" : percent(guard.problem_rate),
    detail: `${guard.tickets_with_problems} of ${guard.merged} merged within ${guard.window_days} days · ${signals}`,
  };
}

function budgetTile(budget: ImprovementBudgetStatus): { value: string; detail: string } {
  if (!budget.available || budget.spent_usd === null) {
    return { value: "unknown", detail: `cap ${dollars(budget.cap_usd)} for ${budget.month}` };
  }
  return {
    value: `${dollars(budget.spent_usd)} of ${dollars(budget.cap_usd)}`,
    detail: budget.paused
      ? `${budget.month} · cap reached: improvement work paused until next month`
      : `${budget.month} · ${dollars(budget.remaining_usd)} left · delivery budgets are separate`,
  };
}

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
          {ticket.interventions && ticket.interventions.length > 0
            ? ticket.interventions.map((kind) => INTERVENTION_LABELS[kind] || kind).join(", ")
            : ticket.outcome === "merged"
              ? "No"
              : "–"}
        </td>
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
          <td colSpan={8}>
            <TicketStory programId={ticket.program_id} workflowId={ticket.workflow_id} />
          </td>
        </tr>
      )}
    </Fragment>
  );
}

function GroupsTable({ groupBy, groups }: { groupBy: string; groups: { value: string; tickets: number; summary: MetricsSummary }[] }) {
  return (
    <section className="metrics-trend" aria-label="By version">
      <h3>By {groupBy}</h3>
      <table className="metrics-table">
        <thead>
          <tr>
            <th>Value</th>
            <th>Tickets</th>
            <th>Merged without a person</th>
            <th>First-try pass</th>
            <th>Problems after merge</th>
            <th>Cost per ticket</th>
            <th>Time to merge</th>
          </tr>
        </thead>
        <tbody>
          {groups.map((g) => (
            <tr key={g.value}>
              <td>{g.value}</td>
              <td>{g.tickets}</td>
              <td>{percent(g.summary.autonomy?.rate)}</td>
              <td>{percent(g.summary.first_try_rate)}</td>
              <td>{g.summary.guard ? guardTile(g.summary.guard).value : "–"}</td>
              <td>{dollars(g.summary.cost_usd.median)}</td>
              <td>{duration(g.summary.time_to_merge_seconds.median)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

const FIX_LABELS: Record<string, string> = {
  none: "not started",
  not_fixable: "needs a person (not a code fix)",
  needs_human: "needs a person",
  started: "self-fix running",
  pr_open: "draft PR open",
  merged: "merged",
  closed: "PR closed",
  failed: "self-fix failed",
};

/** Bugs the harness filed against its own repository, and its self-fix PRs (a person always merges). */
export function HarnessLoopSection({ loop }: { loop: HarnessLoopStatus }) {
  const { settings } = loop;
  if (!settings.repo) {
    return (
      <section className="metrics-trend" aria-label="Harness issues">
        <h3>Harness issues</h3>
        <p className="muted">Off: set TS_HARNESS_SELF_REPO to let the harness file bugs against its own repository.</p>
        {loop.problems.map((p) => (
          <p key={p} className="muted">{p}</p>
        ))}
      </section>
    );
  }
  const limits = loop.limits;
  return (
    <section className="metrics-trend" aria-label="Harness issues">
      <h3>Harness issues</h3>
      <p className="muted">
        {settings.repo} · filing {settings.file_issues ? "on" : "off"} · self-fix {settings.self_fix ? "on" : "off"} into{" "}
        {settings.branch}
        {limits ? ` · ${limits.open_fixes} of ${limits.max_open} self-fix PRs open · ${limits.fixes_last_7_days} of ${limits.max_per_week} this week` : ""}
      </p>
      {!loop.available && <p className="muted">The harness issue ledger could not be read.</p>}
      {loop.problems.length > 0 && (
        <ul aria-label="Harness loop setup">
          {loop.problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
      {loop.issues.length === 0 ? (
        <p className="muted">No harness issues filed.</p>
      ) : (
        <table className="metrics-table">
          <thead>
            <tr>
              <th>Issue</th>
              <th>Component</th>
              <th>Seen</th>
              <th>Self-fix</th>
              <th>PR</th>
            </tr>
          </thead>
          <tbody>
            {loop.issues.map((issue) => (
              <tr key={issue.id}>
                <td>
                  {issue.issue_url ? (
                    <a href={issue.issue_url} target="_blank" rel="noreferrer">
                      #{issue.issue_number}
                    </a>
                  ) : (
                    "not filed yet"
                  )}{" "}
                  <span className="pill">Harness issue</span> {issue.issue_state === "closed" ? "(closed) " : ""}
                  {issue.title}
                </td>
                <td>{issue.component}</td>
                <td>{issue.occurrences}×</td>
                <td title={issue.fix_reason || undefined}>{FIX_LABELS[issue.fix_status] || issue.fix_status}</td>
                <td>
                  {issue.fix_pr_url ? (
                    <a href={issue.fix_pr_url} target="_blank" rel="noreferrer">
                      PR
                    </a>
                  ) : (
                    "–"
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

/** Delivery quality: first-try pass rate, revisions, time to merge, review catch rate, gate disagreement, cost. */
export function MetricsView() {
  const [days, setDays] = useState("14");
  const [project, setProject] = useState("");
  const [repo, setRepo] = useState("");
  const [running, setRunning] = useState(false);
  const [groupBy, setGroupBy] = useState("");
  const [data, setData] = useState<MetricsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const params = new URLSearchParams({ days });
    if (project) params.set("project", project);
    if (repo) params.set("repo", repo);
    if (running) params.set("running", "1");
    if (groupBy) params.set("group_by", groupBy);
    try {
      setData(await apiFetch<MetricsResponse>(`/admin/metrics?${params.toString()}`));
      setError(null);
    } catch (e: any) {
      setError(String(e?.message || e));
    }
  }, [days, project, repo, running, groupBy]);

  useEffect(() => {
    void load();
  }, [load]);

  const facets = data?.facets || { projects: [], repos: [] };
  const groupKeys = facets.group_keys || [];
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
        <label>
          <span>Group by</span>
          <select aria-label="Group by" value={groupBy} onChange={(e) => setGroupBy(e.target.value)}>
            <option value="">Nothing</option>
            {groupKeys.map((key) => (
              <option key={key} value={key}>
                {key}
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
          {s.autonomy && (
            <Tile
              label="Merged without a person"
              value={percent(s.autonomy.rate)}
              detail={`${s.autonomy.autonomous} of ${s.finished} finished · ${s.autonomy.intervened} needed a person${
                Object.keys(s.autonomy.by_kind).length
                  ? ` (${Object.entries(s.autonomy.by_kind)
                      .map(([kind, n]) => `${n} ${INTERVENTION_LABELS[kind] || kind}`)
                      .join(", ")})`
                  : ""
              }`}
            />
          )}
          {s.guard && <Tile label="Problems after merge" {...guardTile(s.guard)} />}
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
          {data?.improvement_budget && <Tile label="Improvement budget" {...budgetTile(data.improvement_budget)} />}
        </ul>
      )}
      {data?.harness_loop && <HarnessLoopSection loop={data.harness_loop} />}
      {data?.groups && data.groups.length > 0 && <GroupsTable groupBy={data.group_by || groupBy} groups={data.groups} />}
      <ImprovementsPanel />
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
                <th>Merged without a person</th>
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
                  <td>{percent(w.autonomy_rate)}</td>
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
              <th>Person stepped in</th>
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
