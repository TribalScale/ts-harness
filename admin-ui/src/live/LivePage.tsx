import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import type { LiveItem, LiveResponse } from "../api/types";
import { StatusPill, type SemanticStatus } from "../components/StatusPill";
import { Timeline, formatDuration } from "./Timeline";

const POLL_MS = 10000;

export const WAITING_LABELS: Record<LiveItem["waiting_on"], string> = {
  you: "Waiting for you",
  agent: "Agent working",
  ci: "Waiting for CI",
  queue: "Queued for a coding slot",
  done: "Done",
};

export const WAITING_STATUS: Record<LiveItem["waiting_on"], SemanticStatus> = {
  you: "danger",
  agent: "neutral",
  ci: "warning",
  queue: "muted",
  done: "success",
};

function LiveCard({ item }: { item: LiveItem }) {
  const [open, setOpen] = useState(false);
  const title = item.jira_issue_key || item.label;
  return (
    <li className={`live-card live-card-${item.waiting_on}`}>
      <div className="live-card-head">
        <strong>{title}</strong>
        <StatusPill status={WAITING_STATUS[item.waiting_on]} label={WAITING_LABELS[item.waiting_on]} />
      </div>
      {item.program_label && <div className="muted live-card-program">{item.program_label}</div>}
      <div className="live-card-state">{item.state}</div>
      <dl className="live-card-facts">
        <div>
          <dt>In this step</dt>
          <dd>{formatDuration(item.in_step_seconds)}</dd>
        </div>
        <div>
          <dt>Total</dt>
          <dd>{formatDuration(item.elapsed_seconds)}</dd>
        </div>
        <div>
          <dt>Revisions</dt>
          <dd>
            {item.revisions} of 8{item.stronger_model ? " · stronger model" : ""}
          </dd>
        </div>
      </dl>
      {item.coding_job?.last_output && (
        <code className="live-card-output" title="Latest coding output">
          {item.coding_job.last_output}
        </code>
      )}
      <div className="live-card-links">
        {item.pr_url && (
          <a href={item.pr_url} target="_blank" rel="noreferrer">
            Pull request
          </a>
        )}
        {item.stuck_task_id && <a href={`#/inbox/stuck_step/${item.stuck_task_id}`}>Decide in Inbox</a>}
        {item.program_id && <a href={`#/programs/${item.program_id}`}>Program</a>}
        <button type="button" className="link-button" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? "Hide timeline" : "Timeline"}
        </button>
      </div>
      {open && <Timeline bars={item.timeline} label={`${title} timeline`} />}
    </li>
  );
}

export function LivePage() {
  const [data, setData] = useState<LiveResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await apiFetch<LiveResponse>("/admin/live"));
      setError(null);
    } catch (e: any) {
      setError(String(e?.message || e));
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const items = data?.items || [];
  const forYou = items.filter((item) => item.waiting_on === "you").length;
  return (
    <div className="page live-page">
      <header className="page-header">
        <h2>Live</h2>
        <p className="muted">Every ticket being built right now, refreshed every 10 seconds.</p>
      </header>
      {error && <div className="error-banner" role="alert">{error}</div>}
      {data && (
        <section className="live-capacity" aria-label="Coding capacity">
          <span>
            <strong>{data.capacity.running}</strong>
            {data.capacity.limit ? ` of ${data.capacity.limit}` : ""} coding jobs running
          </span>
          <span>
            <strong>{data.capacity.queued}</strong> queued
          </span>
          <span>
            <strong>{items.length}</strong> tickets in progress
          </span>
          <span className={forYou ? "live-capacity-you" : ""}>
            <strong>{forYou}</strong> waiting for you
          </span>
        </section>
      )}
      {data && !items.length && <p className="muted">Nothing is being built right now.</p>}
      <ul className="live-cards">
        {items.map((item) => (
          <LiveCard key={item.workflow_id} item={item} />
        ))}
      </ul>
    </div>
  );
}
