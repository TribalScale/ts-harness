import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import type { CurrentRun, StepTiming, StoryEvent, StoryFinding, TicketStoryResponse } from "../api/types";
import { FileFeed } from "./FileFeed";

const POLL_MS = 10000;
const FINDINGS_SHOWN = 2;

export const PHASES: Array<{ key: string; label: string }> = [
  { key: "setup", label: "Setup" },
  { key: "coding", label: "Coding" },
  { key: "verification", label: "Running tests" },
  { key: "review", label: "Pre-push review" },
  { key: "push", label: "Push" },
];

/** "40 s", "5 m 50 s", "2 h 03 m". */
export function preciseDuration(seconds: number | null | undefined): string {
  const s = Math.max(0, Math.round(seconds || 0));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} m ${String(s % 60).padStart(2, "0")} s`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} m`;
}

/** Local time; the date too when it is not today. */
export function localTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const time = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const today = new Date().toDateString() === date.toDateString();
  return today ? time : `${date.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

function money(value: number | null | undefined): string {
  return value == null ? "" : `$${value.toFixed(2)}`;
}

function phaseLine(phases: Record<string, number>): string {
  return PHASES.filter((p) => phases[p.key] != null)
    .map((p) => `${p.label} ${preciseDuration(phases[p.key])}`)
    .join(" · ");
}

/** Steps worth listing under a phase: more than one, or one inside the running phase. */
function stepsFor(steps: Record<string, StepTiming[]> | null | undefined, phase: string, active: boolean): StepTiming[] {
  const list = steps?.[phase] || [];
  return list.length > 1 || (active && list.length > 0) ? list : [];
}

/** "Pre-push review: reviewing (round 1) 1 m 40 s · fixing 2 review findings 9 m 00 s". */
function stepLines(steps: Record<string, StepTiming[]> | null | undefined): string[] {
  return PHASES.map((p) => ({ label: p.label, list: stepsFor(steps, p.key, false) }))
    .filter((entry) => entry.list.length > 0)
    .map((entry) => `${entry.label}: ${entry.list.map((s) => `${s.label} ${preciseDuration(s.seconds)}`).join(" · ")}`);
}

function reviewLine(event: StoryEvent): string {
  const review = event.review;
  if (!review || !review.status) return "";
  const open = review.open_blocking?.length || 0;
  const fixed = review.fixed || 0;
  if (review.status === "no_review") return "Pre-push review: not available";
  if (review.status === "pass")
    return fixed ? `Pre-push review: found and fixed ${fixed} blocking issue${fixed === 1 ? "" : "s"}` : "Pre-push review: passed";
  if (review.status === "fix_failed") return "Pre-push review: a fix broke the tests and was dropped";
  if (review.status === "out_of_time")
    return `Pre-push review: fixed ${fixed} blocking issue${fixed === 1 ? "" : "s"}, then stopped at its time limit; the code review gate checks the rest`;
  return `Pre-push review: ${open} blocking issue${open === 1 ? "" : "s"} left for the gate`;
}

function FindingItem({ finding }: { finding: StoryFinding }) {
  const where = finding.file ? `${finding.file}${finding.line ? `:${finding.line}` : ""}` : "";
  return (
    <li className="story-finding">
      <div className="story-finding-head">
        {finding.severity && (
          <span className={`story-severity story-severity-${finding.severity}`}>{finding.severity}</span>
        )}
        {finding.category && <span className="muted">{finding.category}</span>}
        {where && <code className="story-finding-where">{where}</code>}
      </div>
      <p className="story-finding-message">{finding.message}</p>
    </li>
  );
}

function Findings({ findings, others }: { findings: StoryFinding[]; others?: number }) {
  const [all, setAll] = useState(false);
  if (!findings.length) return null;
  const shown = all ? findings : findings.slice(0, FINDINGS_SHOWN);
  const hidden = findings.length - shown.length;
  return (
    <div className="story-findings">
      <ul>
        {shown.map((finding, index) => (
          <FindingItem key={index} finding={finding} />
        ))}
      </ul>
      {hidden > 0 && (
        <button type="button" className="link-button" onClick={() => setAll(true)}>
          Show {hidden} more
        </button>
      )}
      {!!others && <p className="muted">Plus {others} non-blocking finding{others === 1 ? "" : "s"} on the PR.</p>}
    </div>
  );
}

/** The running coding job: each phase done, the one in progress, and those still to come. */
export function CurrentRunBreakdown({ run }: { run: CurrentRun }) {
  const current = run.phase;
  const currentIndex = PHASES.findIndex((p) => p.key === current);
  return (
    <section className="story-current" aria-label="Current coding run">
      <div className="story-current-head">
        <strong>{run.title}</strong>
        {run.model && <span> on {run.model}</span>}
        <span className="muted">
          {" "}
          · {run.queued ? "queued for a coding slot" : `running ${preciseDuration(run.seconds)}`}
        </span>
      </div>
      {!run.queued && (
        <ol className="story-phases">
          {PHASES.map((phase, index) => {
            const done = run.done?.[phase.key];
            const active = phase.key === current;
            const state = active ? "active" : done != null || (currentIndex >= 0 && index < currentIndex) ? "done" : "todo";
            const steps = stepsFor(run.steps, phase.key, active);
            return (
              <li key={phase.key} className={`story-phase story-phase-${state}`}>
                <span className="story-phase-mark" aria-hidden="true">
                  {state === "done" ? "✓" : state === "active" ? "●" : "○"}
                </span>
                <span className="story-phase-label">{phase.label}</span>
                <span className="story-phase-time">
                  {active
                    ? `${preciseDuration(run.phase_seconds)}${run.detail && !steps.length ? ` · ${run.detail}` : ""}`
                    : done != null
                      ? preciseDuration(done)
                      : ""}
                </span>
                {steps.length > 0 && (
                  <ul className="story-steps">
                    {steps.map((step, i) => (
                      <li key={`${step.label}-${i}`} className={step.active ? "story-step story-step-active" : "story-step"}>
                        <span className="story-step-label">{step.label}</span>
                        <span className="story-step-time">{preciseDuration(step.seconds)}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            );
          })}
        </ol>
      )}
      {!run.queued && !current && <p className="muted">Waiting for the worker's first progress report.</p>}
      {!run.queued && run.job_id && <FileFeed jobId={run.job_id} title="File changes" />}
    </section>
  );
}

function EventRow({ event }: { event: StoryEvent }) {
  const meta = [
    event.model,
    event.seconds != null && event.kind !== "decision" ? preciseDuration(event.seconds) : "",
    event.queued_seconds && event.queued_seconds >= 60 ? `queued ${preciseDuration(event.queued_seconds)}` : "",
    money(event.cost_usd),
  ].filter(Boolean);
  const review = reviewLine(event);
  return (
    <li className={`story-event story-event-${event.status} story-kind-${event.kind}`}>
      <span className="story-time">{localTime(event.at)}</span>
      <span className="story-dot" aria-hidden="true" />
      <div className="story-body">
        <div className="story-title">
          <strong>{event.title}</strong>
          {event.subtitle && event.kind !== "decision" && event.kind !== "paused" && (
            <span className="muted"> · {event.subtitle}</span>
          )}
          {meta.length > 0 && <span className="story-meta">{meta.join(" · ")}</span>}
        </div>
        {(event.kind === "decision" || event.kind === "paused") && event.subtitle && (
          <p className="story-guidance">{event.subtitle}</p>
        )}
        {event.phases && <p className="story-detail">{phaseLine(event.phases)}</p>}
        {stepLines(event.steps).map((line) => (
          <p key={line} className="story-detail muted">
            {line}
          </p>
        ))}
        {review && <p className="story-detail">{review}</p>}
        {event.outcome && <p className={event.status === "failed" ? "story-detail story-bad" : "story-detail"}>{event.outcome}</p>}
        {event.findings && <Findings findings={event.findings} others={event.other_findings} />}
      </div>
    </li>
  );
}

/** One ticket's story: where the time went, the current run's phases, and every event in order. */
export function TicketStory({ programId, workflowId }: { programId: string; workflowId: string }) {
  const [data, setData] = useState<TicketStoryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(
        await apiFetch<TicketStoryResponse>(
          `/admin/programs/${encodeURIComponent(programId)}/tickets/${encodeURIComponent(workflowId)}/story`
        )
      );
      setError(null);
    } catch (e: any) {
      setError(String(e?.message || e));
    }
  }, [programId, workflowId]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  if (error) return <div className="error-banner" role="alert">{error}</div>;
  if (!data) return <p className="muted">Loading…</p>;
  const totals = data.totals || [];
  const events = data.events || [];
  const total = totals.reduce((sum, t) => sum + t.seconds, 0);
  return (
    <div className="ticket-story">
      {totals.length > 0 && (
        <section className="story-totals" aria-label="Where the time went">
          <div className="board-totals-bar">
            {totals.map((t) => (
              <span
                key={t.category}
                className={`story-bar-${t.category}`}
                style={{ width: `${Math.max(1, t.share * 100)}%` }}
                title={`${t.label}: ${preciseDuration(t.seconds)}`}
              />
            ))}
          </div>
          <p className="story-totals-legend">
            {totals.map((t) => `${t.label} ${preciseDuration(t.seconds)}`).join(" · ")}
            {` · total ${preciseDuration(total)}`}
            {data.cost_usd ? ` · coding cost ${money(data.cost_usd)}` : ""}
          </p>
        </section>
      )}
      {data.current_run && <CurrentRunBreakdown run={data.current_run} />}
      <ol className="story-events" aria-label="What happened">
        {events.map((event, index) => (
          <EventRow key={`${event.at}-${index}`} event={event} />
        ))}
      </ol>
      <p className="muted story-footnote">Times are in your local time.</p>
    </div>
  );
}
