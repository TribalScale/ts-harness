import type { TimelineBar } from "../api/types";

export const CATEGORY_LABELS: Record<TimelineBar["category"], string> = {
  coding: "Coding",
  ci: "CI checks",
  review: "Code review",
  qa: "QA",
  merge: "Waiting for merge",
  decision: "Waiting for a decision",
  other: "Other",
};

/** "3h 05m", "12m", "40s". */
export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds || 0));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${String(m % 60).padStart(2, "0")}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

/** One horizontal bar per step run, scaled to the ticket's whole span; revisions show as repeat runs. */
export function Timeline({ bars, label }: { bars: TimelineBar[]; label: string }) {
  if (!bars.length) return <p className="muted">Nothing has run yet.</p>;
  const starts = bars.map((b) => Date.parse(b.start)).filter((n) => !Number.isNaN(n));
  const ends = bars.map((b) => Date.parse(b.end)).filter((n) => !Number.isNaN(n));
  const min = Math.min(...starts);
  const span = Math.max(1, Math.max(...ends) - min);
  return (
    <ol className="live-timeline" aria-label={label}>
      {bars.map((bar) => {
        const left = ((Date.parse(bar.start) - min) / span) * 100;
        const width = Math.max(0.8, ((Date.parse(bar.end) - Date.parse(bar.start)) / span) * 100);
        return (
          <li key={bar.task_id} className="live-timeline-row">
            <span className="live-timeline-label">
              {bar.label}
              {bar.run > 1 ? ` (run ${bar.run})` : ""}
            </span>
            <span className="live-timeline-track">
              <span
                className={`live-bar live-bar-${bar.category}${bar.open ? " live-bar-open" : ""}`}
                style={{ left: `${left}%`, width: `${Math.min(100 - left, width)}%` }}
                title={`${CATEGORY_LABELS[bar.category]}: ${formatDuration(bar.seconds)}`}
              />
            </span>
            <span className="live-timeline-duration mono">{formatDuration(bar.seconds)}</span>
          </li>
        );
      })}
    </ol>
  );
}
