import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import type { BoardItem, BoardResponse } from "../api/types";
import { StatusPill } from "../components/StatusPill";
import { WAITING_LABELS, WAITING_STATUS } from "./LivePage";
import { OUTCOME_LABELS } from "./HistoryView";
import { CATEGORY_LABELS, Timeline, formatDuration } from "./Timeline";
import { TicketStory } from "./TicketStory";
import { AmendWorkOrder } from "./AmendWorkOrder";
import { PauseBadge, PauseButton } from "./PauseControls";

const POLL_MS = 15000;

export const COLUMN_LABELS: Record<string, string> = {
  waiting_on_blockers: "Waiting on blockers",
  ready: "Ready",
  coding: "Coding",
  in_review: "In review",
  waiting_for_you: "Waiting for you",
  paused: "Paused",
  done: "Done",
  stopped: "Stopped",
};

function BoardCard({ entry, onOpen, open }: { entry: BoardItem; onOpen: () => void; open: boolean }) {
  const item = entry.item;
  return (
    <li className="board-card">
      <button type="button" className="board-card-button" aria-expanded={open} onClick={onOpen}>
        <strong>{entry.key}</strong>
        {entry.title && <span className="board-card-title">{entry.title}</span>}
      </button>
      {entry.blocked_by.length > 0 && entry.column !== "done" && (
        <div className="board-chips" aria-label={`${entry.key} blocked by`}>
          {entry.blocked_by.map((key) => (
            <span key={key} className="board-chip">
              blocked by {key}
            </span>
          ))}
        </div>
      )}
      {item && entry.column !== "done" && (
        <div className="board-card-meta muted">
          {item.state} · {formatDuration(item.in_step_seconds)}
        </div>
      )}
      {item?.paused && entry.column !== "done" && entry.column !== "stopped" && (
        <div className="board-chips">
          <span className="board-chip board-chip-paused">paused by {item.paused.by || "someone"}</span>
        </div>
      )}
      {item && entry.column === "done" && (
        <div className="board-card-meta muted">took {formatDuration(item.elapsed_seconds)}</div>
      )}
    </li>
  );
}

/** The program's tickets as a board, each ticket's timeline, and where the time went. */
export function ProgramBoard({ programId }: { programId: string }) {
  const [data, setData] = useState<BoardResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openKey, setOpenKey] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await apiFetch<BoardResponse>(`/admin/programs/${encodeURIComponent(programId)}/board`));
      setError(null);
    } catch (e: any) {
      setError(String(e?.message || e));
    }
  }, [programId]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  if (error) return <div className="error-banner" role="alert">{error}</div>;
  if (!data) return null;
  const programPaused = data.paused?.scope === "program" && data.paused.target_id === programId;
  const pauseRow = (
    <div className="board-pause">
      {data.paused && <PauseBadge entry={data.paused} own={programPaused} />}
      {(!data.paused || programPaused) && (
        <PauseButton scope="program" targetId={programId} paused={programPaused} label="this program" onChanged={() => void load()} />
      )}
    </div>
  );
  if (!data.items.length) {
    return data.paused ? (
      <section className="detail-card program-board" aria-label="Ticket board">
        {pauseRow}
      </section>
    ) : null;
  }
  const opened = data.items.find((entry) => entry.key === openKey);
  const columns = data.columns.filter((column) => column !== "stopped" || data.items.some((e) => e.column === "stopped"));
  return (
    <section className="detail-card program-board" aria-label="Ticket board">
      <h3>
        Tickets
        {data.parallel_limit && data.parallel_limit > 1 ? (
          <span className="muted"> · up to {data.parallel_limit} at a time</span>
        ) : null}
      </h3>
      {pauseRow}
      <div className="board-columns">
        {columns.map((column) => {
          const entries = data.items.filter((entry) => entry.column === column);
          return (
            <div key={column} className={`board-column board-column-${column}`}>
              <h4>
                {COLUMN_LABELS[column] || column} <span className="muted">{entries.length}</span>
              </h4>
              <ul aria-label={COLUMN_LABELS[column] || column}>
                {entries.map((entry) => (
                  <BoardCard
                    key={entry.key}
                    entry={entry}
                    open={openKey === entry.key}
                    onOpen={() => setOpenKey(openKey === entry.key ? null : entry.key)}
                  />
                ))}
              </ul>
            </div>
          );
        })}
      </div>
      {opened?.item && (
        <div className="board-detail">
          <div className="live-card-head">
            <strong>{opened.key}</strong>
            <StatusPill
              status={WAITING_STATUS[opened.item.waiting_on]}
              label={WAITING_LABELS[opened.item.waiting_on]}
            />
          </div>
          {opened.item.paused && opened.column !== "done" && opened.column !== "stopped" && (
            <PauseBadge entry={opened.item.paused} own={opened.item.paused.scope === "ticket"} />
          )}
          {opened.column !== "done" && opened.column !== "stopped" && (
            <div className="live-card-links">
              <AmendWorkOrder workflowId={opened.item.workflow_id} label={opened.key} />
              {(!opened.item.paused || opened.item.paused.scope === "ticket") && (
                <PauseButton
                  scope="ticket"
                  targetId={opened.item.workflow_id}
                  paused={opened.item.paused?.scope === "ticket"}
                  label={opened.key}
                  onChanged={() => void load()}
                />
              )}
            </div>
          )}
          <TicketStory programId={programId} workflowId={opened.item.workflow_id} />
          {(opened.attempts || []).length > 0 && (
            <details className="board-attempts">
              <summary>All attempts ({(opened.attempts || []).length + 1})</summary>
              <ol aria-label={`${opened.key} earlier attempts`}>
                {(opened.attempts || []).map((attempt, index) => (
                  <li key={attempt.workflow_id}>
                    <div className="muted">
                      Attempt {(opened.attempts || []).length - index}:{" "}
                      {OUTCOME_LABELS[attempt.outcome || ""] || attempt.state}
                      {attempt.decided_by ? ` by ${attempt.decided_by}` : ""} · took{" "}
                      {formatDuration(attempt.elapsed_seconds)}
                    </div>
                    <Timeline bars={attempt.timeline} label={`${opened.key} attempt ${(opened.attempts || []).length - index} timeline`} />
                  </li>
                ))}
              </ol>
            </details>
          )}
        </div>
      )}
      {data.totals.length > 0 && (
        <div className="board-totals" aria-label="Where the time went">
          <h4>Where the time went</h4>
          <div className="board-totals-bar">
            {data.totals.map((total) => (
              <span
                key={total.category}
                className={`live-bar-${total.category}`}
                style={{ width: `${Math.max(1, total.share * 100)}%` }}
                title={`${total.label}: ${formatDuration(total.seconds)}`}
              />
            ))}
          </div>
          <ul className="board-totals-legend">
            {data.totals.map((total) => (
              <li key={total.category}>
                <span className={`board-swatch live-bar-${total.category}`} />
                {CATEGORY_LABELS[total.category as keyof typeof CATEGORY_LABELS] || total.label}:{" "}
                {formatDuration(total.seconds)} ({Math.round(total.share * 100)}%)
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
