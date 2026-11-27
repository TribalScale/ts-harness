import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "../api/client";
import type { FileEvent, FileEventList } from "../api/types";

/** How often the feed asks for new file changes. */
export const FILE_FEED_POLL_MS = 5000;
const MAX_SHOWN = 200;

export const KIND_LABELS: Record<FileEvent["kind"], string> = {
  created: "Created",
  edited: "Edited",
  deleted: "Deleted",
  renamed: "Renamed",
  restored: "Restored",
  bulk: "Many files",
};

export function eventTime(at: string | null | undefined): string {
  if (!at) return "";
  const date = new Date(at);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** "edited `src/a.ts`", "formatted 68 files in docs", "renamed a.ts → b.ts". */
export function eventSummary(event: FileEvent): string {
  if (event.kind === "bulk") {
    const what = (event.bulk_kind && KIND_LABELS[event.bulk_kind as FileEvent["kind"]]?.toLowerCase()) || "changed";
    return `${what} ${event.file_count} files in ${event.path}`;
  }
  if (event.kind === "renamed" && event.from_path) return `${event.from_path} → ${event.path}`;
  return event.path;
}

function FileEventRow({ event, showTicket }: { event: FileEvent; showTicket: boolean }) {
  return (
    <li className={`file-event file-event-${event.kind}`}>
      <a href={`#/live/files/${event.id}`} className="file-event-link">
        <span className="muted mono file-event-time">{eventTime(event.at)}</span>
        <span className={`file-event-kind file-event-kind-${event.kind}`}>{KIND_LABELS[event.kind]}</span>
        <span className="mono file-event-path">{eventSummary(event)}</span>
        {(event.additions > 0 || event.deletions > 0) && (
          <span className="mono file-event-counts">
            <span className="diff-add">+{event.additions}</span> <span className="diff-del">−{event.deletions}</span>
          </span>
        )}
        {event.actor === "harness" && <span className="muted file-event-actor">by the harness</span>}
        {showTicket && event.work_order_id && <span className="muted file-event-ticket">{event.work_order_id}</span>}
        {event.step && <span className="muted file-event-step">{event.step}</span>}
      </a>
    </li>
  );
}

/**
 * File changes as they happen: one run's (``jobId``), or every running ticket's (``all``). New
 * changes appear at the top; each opens its detail page.
 */
export function FileFeed({ jobId, all = false, title = "Files" }: { jobId?: string; all?: boolean; title?: string }) {
  const [events, setEvents] = useState<FileEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const after = useRef(0);
  const url = all ? "/admin/live/files?active=1" : jobId ? `/admin/coding-jobs/${encodeURIComponent(jobId)}/files` : "";

  const load = useCallback(async () => {
    if (!url) return;
    try {
      const sep = url.includes("?") ? "&" : "?";
      const out = await apiFetch<FileEventList>(`${url}${sep}after=${after.current}`);
      const fresh = out.events || [];
      after.current = Math.max(after.current, out.next_after || 0);
      if (fresh.length) {
        // A run's events come oldest first, the global feed newest first: show newest first.
        const ordered = [...fresh].sort((a, b) => b.id - a.id);
        setEvents((current) => [...ordered, ...current.filter((e) => !ordered.some((n) => n.id === e.id))].slice(0, MAX_SHOWN));
      }
      setError(null);
    } catch (e: any) {
      setError(String(e.message || e));
    }
  }, [url]);

  useEffect(() => {
    after.current = 0;
    setEvents([]);
    load();
    const timer = window.setInterval(load, FILE_FEED_POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  if (!url) return null;
  return (
    <section className="file-feed" aria-label={title}>
      <h4>{title}</h4>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {!error && !events.length && (
        <p className="muted">{all ? "No file changes in running tickets yet." : "No file changes yet."}</p>
      )}
      {events.length > 0 && (
        <ol className="file-event-list">
          {events.map((event) => (
            <FileEventRow key={event.id} event={event} showTicket={all} />
          ))}
        </ol>
      )}
    </section>
  );
}
