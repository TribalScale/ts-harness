import { useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import type { CanarySide, ImprovementProposal, ImprovementsResponse, ReplayScore } from "../api/types";

/** Self-improvement proposals (ts_harness.improvement): the digest's proposals, their offline
 * replays, canaries with variant-vs-control numbers, and the month's improvement spend. */

const STATUS_LABELS: Record<string, string> = {
  proposed: "proposed",
  evaluating: "evaluating offline",
  canary: "canary running",
  awaiting_approval: "waiting for approval",
  promoted: "promoted",
  rolled_back: "rolled back",
  rejected: "rejected",
};

const TYPE_LABELS: Record<string, string> = {
  knowledge_note: "knowledge note",
  skill_edit: "skill edit",
  setting_change: "setting change",
  fragment_change: "workflow change",
  needs_design: "needs a person to design",
};

function percent(value: number | null | undefined): string {
  return value === null || value === undefined ? "–" : `${Math.round(value * 100)}%`;
}

function dollars(value: number | null | undefined): string {
  return value === null || value === undefined ? "–" : `$${value.toFixed(2)}`;
}

function show(value: unknown): string {
  if (value === null || value === undefined) return "(unset)";
  return typeof value === "string" ? value : JSON.stringify(value);
}

export function statusLabel(status: string): string {
  return STATUS_LABELS[status] || status.replace(/_/g, " ");
}

export function typeLabel(type: string): string {
  return TYPE_LABELS[type] || type.replace(/_/g, " ");
}

function ScoreRow({ label, score }: { label: string; score: ReplayScore }) {
  return (
    <tr>
      <td>{label}</td>
      <td>
        {percent(score.catch_rate)} ({score.caught}/{score.blocks})
      </td>
      <td>
        {percent(score.false_block_rate)} ({score.false_blocks}/{score.passes})
      </td>
      <td>{percent(score.agreement)}</td>
    </tr>
  );
}

function SideRow({ label, side }: { label: string; side: CanarySide }) {
  return (
    <tr>
      <td>{label}</td>
      <td>{side.finished}</td>
      <td>{percent(side.autonomy_rate)}</td>
      <td>
        {percent(side.problem_rate)} ({side.tickets_with_problems})
      </td>
      <td>{dollars(side.cost_median_usd)}</td>
    </tr>
  );
}

/** The full proposal: evidence, the change (diff, setting, note), evaluation and canary numbers. */
export function ProposalDetail({ proposal }: { proposal: ImprovementProposal }) {
  const change = proposal.change || {};
  const evidence = proposal.evidence || {};
  const evaluation = proposal.evaluation;
  const latest = proposal.canary?.latest;
  return (
    <div className="detail-sections" aria-label="Improvement proposal">
      <section className="detail-card">
        <h3>
          {typeLabel(proposal.type)} · {statusLabel(proposal.status)}
        </h3>
        {proposal.summary && <p>{proposal.summary}</p>}
        <p className="muted">
          Target: <span className="mono">{proposal.target || "–"}</span> · metric: {proposal.metric || "–"} · cost so far{" "}
          {dollars(proposal.cost_usd)}
        </p>
        {proposal.expected_effect && <p>Expected effect: {proposal.expected_effect}</p>}
        {proposal.risk && <p>Risk: {proposal.risk}</p>}
      </section>
      <section className="detail-card">
        <h3>Evidence</h3>
        <p>
          {evidence.tickets ?? 0} tickets show <span className="mono">{evidence.pattern || proposal.pattern_key}</span>
          {evidence.digest_id ? ` (${evidence.digest_id})` : ""}
        </p>
        {evidence.examples && evidence.examples.length > 0 && (
          <ul>
            {evidence.examples.map((text, index) => (
              <li key={index}>{text}</li>
            ))}
          </ul>
        )}
        {evidence.refs && evidence.refs.length > 0 && (
          <ul aria-label="Tickets">
            {evidence.refs.map((ref, index) => (
              <li key={index} className="mono">
                {ref.jira_issue_key || ref.workflow_id}
                {ref.pr_url ? (
                  <>
                    {" "}
                    <a href={ref.pr_url} target="_blank" rel="noreferrer">
                      PR
                    </a>
                  </>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </section>
      {proposal.type === "needs_design" && (
        <section className="detail-card">
          <h3>Off limits for the loop</h3>
          <p>{change.idea || proposal.summary}</p>
          <p className="muted">
            It touches {change.off_limits?.match}
            {change.off_limits?.section ? ` (${change.off_limits.section})` : ""}: {change.off_limits?.why}. A person has to
            design this change; the loop will not act on it.
          </p>
        </section>
      )}
      {change.diff && (
        <section className="detail-card">
          <h3>Change</h3>
          <pre className="mono" aria-label="Diff">
            {change.diff}
          </pre>
        </section>
      )}
      {change.setting && (
        <section className="detail-card">
          <h3>Setting</h3>
          <p className="mono">
            {change.setting.name}: {show(change.setting.old)} → {show(change.setting.new)}
          </p>
        </section>
      )}
      {change.note && (
        <section className="detail-card">
          <h3>Knowledge note</h3>
          <p>
            <strong>{change.note.title}</strong>
          </p>
          <p>{change.note.body}</p>
        </section>
      )}
      {evaluation && (
        <section className="detail-card">
          <h3>Offline check</h3>
          <p>
            {evaluation.status === "unavailable" ? "No offline check available" : evaluation.status}
            {evaluation.reason && evaluation.status !== "unavailable" ? `: ${evaluation.reason}` : ""}
            {evaluation.cost_usd ? ` · ${dollars(evaluation.cost_usd)}` : ""}
          </p>
          {evaluation.scores && (
            <table className="metrics-table" aria-label="Replay scores">
              <thead>
                <tr>
                  <th>Reviewer</th>
                  <th>Catch rate</th>
                  <th>False blocks</th>
                  <th>Agreement</th>
                </tr>
              </thead>
              <tbody>
                <ScoreRow label="current" score={evaluation.scores.current} />
                <ScoreRow label="candidate" score={evaluation.scores.candidate} />
              </tbody>
            </table>
          )}
        </section>
      )}
      {latest && (
        <section className="detail-card">
          <h3>Canary</h3>
          <p className="muted">
            {proposal.canary?.variant} since {proposal.canary?.started_at} · {percent(proposal.canary?.share)} of new
            tickets · checked {latest.checked_at}
          </p>
          <table className="metrics-table" aria-label="Variant against control">
            <thead>
              <tr>
                <th />
                <th>Finished</th>
                <th>Merged without a person</th>
                <th>Problems after merge</th>
                <th>Cost per ticket</th>
              </tr>
            </thead>
            <tbody>
              <SideRow label="candidate" side={latest.candidate} />
              <SideRow label="control" side={latest.control} />
            </tbody>
          </table>
          {latest.decision && <p>{latest.decision.reason}</p>}
        </section>
      )}
      {proposal.promotion && (
        <section className="detail-card">
          <h3>Promotion</h3>
          {proposal.promotion.pr_url ? (
            <p>
              <a href={proposal.promotion.pr_url} target="_blank" rel="noreferrer">
                Pull request
              </a>{" "}
              opened by the harness (not merged).
            </p>
          ) : (
            <p>Apply it by hand: {proposal.promotion.reason}</p>
          )}
        </section>
      )}
    </div>
  );
}

/** The Metrics tab's "Improvements" section. */
export function ImprovementsPanel() {
  const [data, setData] = useState<ImprovementsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<number | null>(null);

  useEffect(() => {
    let live = true;
    apiFetch<ImprovementsResponse>("/admin/improvements")
      .then((body) => {
        if (live && body && Array.isArray(body.proposals)) setData(body);
      })
      .catch((e: unknown) => {
        if (live) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      live = false;
    };
  }, []);

  if (error) return <p className="muted">Improvements unavailable: {error}</p>;
  if (!data) return null;
  const spent = data.budget?.spent_usd;
  const live = data.proposals.filter((p) => p.status === "canary" || p.status === "awaiting_approval");
  return (
    <section className="metrics-trend" aria-label="Improvements">
      <h3>Improvements</h3>
      <p className="muted">
        This month {dollars(spent)} of {dollars(data.budget?.cap_usd)} improvement budget
        {data.budget?.paused ? " (paused until next month)" : ""} · {live.length} live canar{live.length === 1 ? "y" : "ies"}
        {data.rules ? ` · canary: ${percent(data.rules.canary.share)} of new tickets, ${data.rules.canary.min_tickets} per side` : ""}
        {data.rules && !data.rules.canary.enabled ? " · canaries are off" : ""}
      </p>
      {data.proposals.length === 0 ? (
        <p className="muted">No proposals yet. The weekly digest proposes changes for failures that recur on 3 or more tickets.</p>
      ) : (
        <table className="metrics-table" aria-label="Improvement proposals">
          <thead>
            <tr>
              <th>Proposal</th>
              <th>Type</th>
              <th>Status</th>
              <th>Offline check</th>
              <th>Candidate vs control</th>
              <th>Cost</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {data.proposals.map((p) => {
              const latest = p.canary?.latest;
              return (
                <tr key={p.id}>
                  <td>
                    #{p.id} {p.title}
                  </td>
                  <td>{typeLabel(p.type)}</td>
                  <td>{statusLabel(p.status)}</td>
                  <td>{p.evaluation ? (p.evaluation.status === "unavailable" ? "none available" : p.evaluation.status) : "–"}</td>
                  <td>
                    {latest
                      ? `${percent(latest.candidate.autonomy_rate)} vs ${percent(latest.control.autonomy_rate)} (${latest.candidate.finished}/${latest.control.finished})`
                      : "–"}
                  </td>
                  <td>{dollars(p.cost_usd)}</td>
                  <td>
                    <button type="button" className="link-button" onClick={() => setOpen(open === p.id ? null : p.id)}>
                      {open === p.id ? "Hide" : "Details"}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {open !== null && data.proposals.find((p) => p.id === open) && (
        <ProposalDetail proposal={data.proposals.find((p) => p.id === open) as ImprovementProposal} />
      )}
    </section>
  );
}
