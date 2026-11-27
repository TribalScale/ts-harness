import { useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import type { FileEventDetail } from "../api/types";
import { KIND_LABELS, eventSummary, eventTime } from "./FileFeed";

function DiffExcerpt({ text }: { text: string }) {
  return (
    <pre className="diff-excerpt mono" aria-label="Change">
      {text.split("\n").map((line, index) => {
        const tone = line.startsWith("+++") || line.startsWith("---")
          ? "diff-file"
          : line.startsWith("@@")
            ? "diff-hunk"
            : line.startsWith("+")
              ? "diff-add"
              : line.startsWith("-")
                ? "diff-del"
                : "";
        return (
          <span key={index} className={tone}>
            {line}
            {"\n"}
          </span>
        );
      })}
    </pre>
  );
}

/** One file change: the diff, where it happened, the file's history in the run, checks that failed on it. */
export function FileEventPage({ eventId }: { eventId: string }) {
  const [detail, setDetail] = useState<FileEventDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDetail(null);
    setError(null);
    apiFetch<FileEventDetail>(`/admin/live/files/${encodeURIComponent(eventId)}`)
      .then(setDetail)
      .catch((e: any) => setError(String(e.message || e)));
  }, [eventId]);

  if (error)
    return (
      <div className="error-banner" role="alert">
        {error}
      </div>
    );
  if (!detail) return <p className="muted">Loading…</p>;
  const { event, history, failed_checks: failed } = detail;
  return (
    <article className="file-event-page">
      <p>
        <a href="#/live/files">← All file changes</a>
      </p>
      <h2>
        <span className={`file-event-kind file-event-kind-${event.kind}`}>{KIND_LABELS[event.kind]}</span>{" "}
        <span className="mono">{eventSummary(event)}</span>
      </h2>
      <dl className="file-event-facts">
        <dt>When</dt>
        <dd>{eventTime(event.at)}</dd>
        {event.work_order_id && (
          <>
            <dt>Ticket</dt>
            <dd>
              {event.program_id ? (
                <a href={`#/programs/${event.program_id}`}>{event.work_order_id}</a>
              ) : (
                event.work_order_id
              )}
            </dd>
          </>
        )}
        <dt>Step</dt>
        <dd>{event.step || "—"}</dd>
        <dt>By</dt>
        <dd>{event.actor === "harness" ? "The harness (a revert or a base merge)" : "The coding agent"}</dd>
        {(event.additions > 0 || event.deletions > 0) && (
          <>
            <dt>Lines</dt>
            <dd className="mono">
              <span className="diff-add">+{event.additions}</span> <span className="diff-del">−{event.deletions}</span>
            </dd>
          </>
        )}
        {detail.pr_url && (
          <>
            <dt>Pull request</dt>
            <dd>
              <a href={detail.pr_url} target="_blank" rel="noreferrer">
                {detail.pr_url}
              </a>
            </dd>
          </>
        )}
      </dl>

      {event.kind === "bulk" && event.paths && event.paths.length > 0 && (
        <section className="detail-card">
          <h3>Files ({event.file_count})</h3>
          <ul className="mono file-event-paths">
            {event.paths.map((path) => (
              <li key={path}>{path}</li>
            ))}
          </ul>
          {event.file_count > event.paths.length && <p className="muted">and {event.file_count - event.paths.length} more</p>}
        </section>
      )}

      {event.kind !== "bulk" && (
        <section className="detail-card">
          <h3>Change</h3>
          {event.binary ? (
            <p className="muted">A binary or very large file: no excerpt.</p>
          ) : event.excerpt ? (
            <DiffExcerpt text={event.excerpt} />
          ) : (
            <p className="muted">No line changes{event.kind === "renamed" ? " (renamed only)" : ""}.</p>
          )}
          {event.truncated && <p className="muted">The excerpt shows the first 200 lines of the change.</p>}
          {event.masked && <p className="muted">Values that looked like secrets are shown as ***.</p>}
        </section>
      )}

      {failed.length > 0 && (
        <section className="detail-card">
          <h3>Checks that failed on this file</h3>
          {failed.map((check, index) => (
            <div key={index}>
              <p>
                <strong>{check.check}</strong> <span className="muted mono">{check.command}</span>
              </p>
              <pre className="mono diff-excerpt">{check.output_tail}</pre>
            </div>
          ))}
        </section>
      )}

      <section className="detail-card">
        <h3>This file in the run</h3>
        <ol className="file-event-list">
          {history.map((item) => (
            <li key={item.id} className={item.id === event.id ? "file-event file-event-current" : "file-event"}>
              <a href={`#/live/files/${item.id}`} className="file-event-link">
                <span className="muted mono file-event-time">{eventTime(item.at)}</span>
                <span className={`file-event-kind file-event-kind-${item.kind}`}>{KIND_LABELS[item.kind]}</span>
                {(item.additions > 0 || item.deletions > 0) && (
                  <span className="mono file-event-counts">
                    <span className="diff-add">+{item.additions}</span> <span className="diff-del">−{item.deletions}</span>
                  </span>
                )}
                {item.step && <span className="muted file-event-step">{item.step}</span>}
              </a>
            </li>
          ))}
        </ol>
      </section>
    </article>
  );
}
