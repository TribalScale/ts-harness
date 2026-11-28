import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import type { LiveItem, LiveResponse } from "../api/types";
import { StatusPill, type SemanticStatus } from "../components/StatusPill";
import { HistoryView } from "./HistoryView";
import { MetricsView } from "./MetricsView";
import { formatDuration } from "./Timeline";
import { CurrentRunBreakdown, TicketStory } from "./TicketStory";
import { FileEventPage } from "./FileEventPage";
import { FileFeed } from "./FileFeed";
import { AmendWorkOrder } from "./AmendWorkOrder";
import { PauseBadge, PauseButton } from "./PauseControls";

const POLL_MS = 10000;

export const WAITING_LABELS: Record<LiveItem["waiting_on"], string> = {
  you: "Waiting for you",
  paused: "Paused",
  agent: "Agent working",
  ci: "Waiting for CI",
  queue: "Queued for a coding slot",
  done: "Done",
};

export const WAITING_STATUS: Record<LiveItem["waiting_on"], SemanticStatus> = {
  you: "danger",
  paused: "warning",
  agent: "neutral",
  ci: "warning",
  queue: "muted",
  done: "success",
};

/** "claude-opus-5-5" reads as "Opus". */
export function modelLabel(model?: string): string {
  const text = String(model || "").split("/").pop()?.toLowerCase() || "";
  for (const name of ["opus", "sonnet", "haiku", "fable"]) if (text.includes(name)) return name[0].toUpperCase() + name.slice(1);
  return model || "";
}

function LiveCard({ item, onChanged }: { item: LiveItem; onChanged?: () => void }) {
  const [open, setOpen] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);

  async function retry() {
    setRetrying(true);
    setRetryError(null);
    try {
      await apiFetch("/admin/workflows/decide", {
        method: "POST",
        body: JSON.stringify({ workflow_id: item.workflow_id, task_id: item.stuck_task_id, action: "retry" }),
      });
      onChanged?.();
    } catch (e: any) {
      setRetryError(String(e?.message || e));
    } finally {
      setRetrying(false);
    }
  }
  const title = item.jira_issue_key || item.label;
  return (
    <li className={`live-card live-card-${item.waiting_on}`}>
      <div className="live-card-head">
        <strong>{title}</strong>
        <StatusPill status={WAITING_STATUS[item.waiting_on]} label={WAITING_LABELS[item.waiting_on]} />
      </div>
      {item.program_label && <div className="muted live-card-program">{item.program_label}</div>}
      <div className="live-card-state">{item.state}</div>
      {item.paused && <PauseBadge entry={item.paused} own={item.paused.scope === "ticket"} />}
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
      {item.coding_job && (item.coding_job.status !== "queued" || item.coding_job.progress) && (
        <CurrentRunBreakdown
          run={{
            title: item.revisions ? `Revision ${item.revisions}` : "Coding",
            model: modelLabel(item.coding_job.model),
            started_at: item.coding_job.started_at,
            seconds: item.coding_job.started_at ? (Date.now() - Date.parse(item.coding_job.started_at)) / 1000 : null,
            queued: item.coding_job.status === "queued",
            phase: item.coding_job.progress?.phase || "",
            phase_seconds: item.coding_job.progress?.phase_seconds,
            detail: item.coding_job.progress?.detail || "",
            done: item.coding_job.progress?.done || {},
            steps: item.coding_job.progress?.steps || {},
          }}
        />
      )}
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
        {item.stuck_task_id && (
          <button type="button" className="link-button" disabled={retrying} onClick={() => void retry()}>
            {retrying ? "Retrying…" : "Retry"}
          </button>
        )}
        {item.stuck_task_id && <a href={`#/inbox/stuck_step/${item.stuck_task_id}`}>Decide in Inbox</a>}
        {item.program_id && <a href={`#/programs/${item.program_id}`}>Program</a>}
        {item.kind !== "done" && <AmendWorkOrder workflowId={item.workflow_id} label={title} />}
        {item.kind !== "done" && (!item.paused || item.paused.scope === "ticket") && (
          <PauseButton
            scope="ticket"
            targetId={item.workflow_id}
            paused={item.paused?.scope === "ticket"}
            label={title}
            onChanged={() => onChanged?.()}
          />
        )}
        <button type="button" className="link-button" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? "Hide details" : "Details"}
        </button>
      </div>
      {retryError && <div className="error-banner" role="alert">{retryError}</div>}
      {open && item.program_id && <TicketStory programId={item.program_id} workflowId={item.workflow_id} />}
    </li>
  );
}

export type LiveTab = "now" | "files" | "history" | "metrics";

const TAB_LABELS: Record<LiveTab, string> = { now: "Now", files: "Files", history: "History", metrics: "Metrics" };

const TAB_INTROS: Record<LiveTab, string> = {
  now: "Every ticket being built right now, refreshed every 10 seconds.",
  files: "Files the running tickets create, edit, rename or delete, as it happens. Open one for its change.",
  history: "Finished tickets and work orders: merged, accepted, stopped, cancelled or failed.",
  metrics: "Delivery quality over a period: first-try pass rate, revisions, time to merge, review catch rate and cost.",
};

export function LivePage({
  tab = "now",
  onTab,
  fileEventId,
}: {
  tab?: LiveTab;
  onTab?: (tab: LiveTab) => void;
  /** A file change to show (#/live/files/<id>). */
  fileEventId?: string;
}) {
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
    if (tab !== "now") return undefined;
    void load();
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [load, tab]);

  const items = data?.items || [];
  const forYou = items.filter((item) => item.waiting_on === "you").length;
  const paused = items.filter((item) => item.paused).length;
  return (
    <div className="page live-page">
      <header className="page-header">
        <h2>Live</h2>
        <p className="muted">{TAB_INTROS[tab]}</p>
      </header>
      <div className="live-tabs" role="tablist" aria-label="Live views">
        {(["now", "files", "history", "metrics"] as const).map((key) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={tab === key}
            className={tab === key ? "live-tab live-tab-active" : "live-tab"}
            onClick={() => onTab?.(key)}
          >
            {TAB_LABELS[key]}
          </button>
        ))}
      </div>
      {tab === "files" && (fileEventId ? <FileEventPage eventId={fileEventId} /> : <FileFeed all title="File changes in running tickets" />)}
      {tab === "history" && <HistoryView />}
      {tab === "metrics" && <MetricsView />}
      {tab === "now" && error && <div className="error-banner" role="alert">{error}</div>}
      {tab === "now" && data?.paused && (
        <div className="pause-banner" role="status">
          <strong>All coding is paused.</strong> {data.paused.by ? `Paused by ${data.paused.by}` : ""}
          {data.paused.at ? ` at ${new Date(data.paused.at).toLocaleString()}` : ""}
          {data.paused.reason ? ` · ${data.paused.reason}` : ""}. Running steps finish; queued coding jobs wait.
          An admin resumes it on the <a href="#/system">System page</a>.
        </div>
      )}
      {tab === "now" && data && (
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
          {paused > 0 && (
            <span>
              <strong>{paused}</strong> paused
            </span>
          )}
        </section>
      )}
      {tab === "now" && data && !items.length && <p className="muted">Nothing is being built right now.</p>}
      {tab === "now" && (
        <ul className="live-cards">
          {items.map((item) => (
            <LiveCard key={item.workflow_id} item={item} onChanged={() => void load()} />
          ))}
        </ul>
      )}
    </div>
  );
}
