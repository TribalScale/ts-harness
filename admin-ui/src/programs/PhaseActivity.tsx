import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "../api/client";
import type {
  CodingJobProgress,
  PhaseActivity,
  WorkflowProgress,
  WorkflowStep,
} from "../api/types";
import { hasPermission } from "../auth/session";
import { Button } from "../components/Button";
import { StatusPill, type SemanticStatus } from "../components/StatusPill";
import { GateReplyCard } from "../inbox/InboxPage";
import { GateReview } from "./GateReview";

const POLL_MS = 15000;

const KIND_STATUS: Record<WorkflowProgress["kind"], SemanticStatus> = {
  running: "neutral",
  waiting: "warning",
  paused: "warning",
  stuck: "danger",
  blocked: "danger",
  done: "success",
  failed: "danger",
};

function humanize(value: string): string {
  return value.replace(/_/g, " ");
}

function prLabel(url: string): string {
  const match = url.match(/\/pull\/(\d+)/);
  return match ? `PR #${match[1]}` : "Pull request";
}

function updatedLabel(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? ""
    : `Updated ${date.toLocaleTimeString()}`;
}

export function codingJobLabel(
  job: CodingJobProgress,
  now: number = Date.now(),
): string {
  if (job.status === "queued")
    return "Queued: waiting for a free coding worker";
  if (job.status === "starting") return "Coding worker starting";
  const started = new Date(job.started_at || job.created_at).getTime();
  if (Number.isNaN(started)) return "Coding in progress";
  const minutes = Math.max(0, Math.floor((now - started) / 60000));
  return `Coding in progress · ${minutes} min`;
}

export function usePhaseActivity(
  programId: string | undefined,
  phase: string | undefined,
  poll: boolean,
) {
  const [activity, setActivity] = useState<PhaseActivity | null>(null);
  const [error, setError] = useState<string | null>(null);
  // A response that lands after the panel unmounted (or the program changed) is dropped.
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const reload = useCallback(async () => {
    if (!programId || !phase) return;
    try {
      const data = await apiFetch<PhaseActivity>(
        `/admin/programs/${encodeURIComponent(programId)}/phases/${encodeURIComponent(phase)}/activity`,
      );
      if (!mounted.current) return;
      setActivity(data);
      setError(null);
    } catch (e: any) {
      if (mounted.current) setError(String(e?.message || e));
    }
  }, [programId, phase]);

  useEffect(() => {
    setActivity(null);
    setError(null);
    void reload();
  }, [reload]);

  useEffect(() => {
    if (!poll) return;
    const timer = window.setInterval(() => void reload(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [poll, reload]);

  return { activity, error, reload };
}

function StepList({
  steps,
  label,
  actions,
}: {
  steps: WorkflowStep[];
  label: string;
  actions?: (step: WorkflowStep) => React.ReactNode;
}) {
  return (
    <ol className="workflow-steps" aria-label={label}>
      {steps.map((step) => (
        <li key={step.task_id || step.step_name}>
          <span className="workflow-step-label">{step.label}</span>
          <span className="mono muted">{step.step_name}</span>
          <span className={`workflow-step-status step-${step.status}`}>
            {step.approval_pending
              ? "waiting for approval"
              : step.status === "coding"
                ? "coding in progress"
                : step.status}
          </span>
          {actions?.(step)}
          {step.error && (
            <span className="muted workflow-step-error">{step.error}</span>
          )}
        </li>
      ))}
    </ol>
  );
}

export function WorkflowRows({
  items,
  actions,
}: {
  items: WorkflowProgress[];
  // Approve/Reject for gates inside started workflows (a release track's promote gates).
  actions?: (step: WorkflowStep) => React.ReactNode;
}) {
  const [open, setOpen] = useState<Record<string, boolean>>({});

  return (
    <ul className="workflow-list">
      {items.map((item) => {
        // A row with a gate waiting for approval opens by default, so its Approve button shows.
        const waiting =
          Boolean(actions) && item.steps.some((step) => step.approval_pending);
        const expanded = open[item.workflow_id] ?? waiting;
        return (
          <li key={item.workflow_id} className="workflow-row">
            <div className="workflow-row-head">
              <button
                type="button"
                className="workflow-toggle"
                aria-expanded={expanded}
                onClick={() =>
                  setOpen((prev) => ({
                    ...prev,
                    [item.workflow_id]: !expanded,
                  }))
                }
              >
                <span aria-hidden="true">{expanded ? "▾" : "▸"}</span>
                <strong>{item.label}</strong>
              </button>
              <StatusPill
                status={KIND_STATUS[item.kind] || "neutral"}
                label={item.state}
              />
              {item.revisions > 0 && (
                <span className="muted">
                  {item.revisions}{" "}
                  {item.revisions === 1 ? "revision" : "revisions"}
                </span>
              )}
              {item.pr_url && (
                <a href={item.pr_url} target="_blank" rel="noreferrer">
                  {prLabel(item.pr_url)}
                </a>
              )}
              {item.kind === "stuck" && item.stuck_task_id && (
                <a href={`#/inbox/stuck_step/${item.stuck_task_id}`}>
                  Decide in Inbox
                </a>
              )}
              <span className="muted workflow-updated">
                {updatedLabel(item.updated_at)}
              </span>
            </div>
            {item.coding_job && (
              <div className="workflow-coding-job muted">
                <span>{codingJobLabel(item.coding_job)}</span>
                {item.coding_job.last_output && (
                  <code>{item.coding_job.last_output}</code>
                )}
              </div>
            )}
            {expanded && (
              <StepList
                steps={item.steps}
                label={`${item.label} steps`}
                actions={actions}
              />
            )}
          </li>
        );
      })}
    </ul>
  );
}

export function PhaseActivityPanel({
  phase,
  activity,
  error,
  programClosed,
  canApproveProgram = true,
  onChanged,
}: {
  phase: string;
  activity: PhaseActivity | null;
  error?: string | null;
  programClosed?: boolean;
  canApproveProgram?: boolean;
  onChanged?: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  // Gates decided here: their buttons stay gone even if a refresh still shows them pending.
  const [decided, setDecided] = useState<Record<string, boolean>>({});
  const [actionError, setActionError] = useState<string | null>(null);
  // Gates whose review the user closed; a waiting gate shows what it approves by default.
  const [hidden, setHidden] = useState<Record<string, boolean>>({});
  // The answer typed for a gate (a repo, intake answers or feedback), sent with the decision.
  const [replies, setReplies] = useState<Record<string, string>>({});
  const canApprove =
    hasPermission("approve_gate") && canApproveProgram && !programClosed;
  const workflow = activity?.workflow || null;
  const children = activity?.children || [];
  const childTitle =
    children.length && children.every((c) => /^WO-/i.test(c.label))
      ? "Work orders"
      : "Started workflows";

  async function decide(taskId: string, approve: boolean) {
    if (decided[taskId] || busy === taskId) return;
    setBusy(taskId);
    setActionError(null);
    try {
      await apiFetch(
        approve ? "/admin/workflows/approve" : "/admin/workflows/reject",
        {
          method: "POST",
          body: JSON.stringify({
            id: taskId,
            ...((replies[taskId] || "").trim() ? { comments: (replies[taskId] || "").trim() } : {}),
          }),
        },
      );
      setDecided((prev) => ({ ...prev, [taskId]: true }));
      onChanged?.();
    } catch (e: any) {
      setActionError(String(e?.message || e));
    } finally {
      setBusy(null);
    }
  }

  function approvalActions(step: WorkflowStep) {
    if (!step.approval_pending || !canApprove) return null;
    if (decided[step.task_id]) return <span className="muted">Decided; the next step is starting…</span>;
    const reviewing = !hidden[step.task_id];
    return (
      <>
        <span className="workflow-step-actions">
          <button
            type="button"
            className="link-button"
            aria-expanded={reviewing}
            onClick={() =>
              setHidden((prev) => ({ ...prev, [step.task_id]: reviewing }))
            }
          >
            {reviewing ? "Hide review" : "Review"}
          </button>
          <a
            className="muted"
            href={`#/inbox/workflow_gate/${encodeURIComponent(step.task_id)}`}
          >
            Inbox
          </a>
          <Button
            variant="danger"
            size="sm"
            disabled={busy === step.task_id}
            onClick={() => void decide(step.task_id, false)}
          >
            Reject
          </Button>
          <Button
            variant="primary"
            size="sm"
            disabled={busy === step.task_id}
            onClick={() => void decide(step.task_id, true)}
          >
            Approve
          </Button>
        </span>
        {reviewing && (
          <div className="workflow-step-review">
            <GateReview taskId={step.task_id} />
          </div>
        )}
        {step.reply && (
          <div className="workflow-step-review">
            <GateReplyCard
              reply={step.reply}
              value={replies[step.task_id] || ""}
              onChange={(value) => setReplies((prev) => ({ ...prev, [step.task_id]: value }))}
            />
          </div>
        )}
      </>
    );
  }

  return (
    <section
      className="phase-activity"
      aria-label={`${humanize(phase)} activity`}
    >
      <div className="phase-activity-head">
        <h3>{humanize(phase)}</h3>
        {workflow && (
          <StatusPill
            status={KIND_STATUS[workflow.kind] || "neutral"}
            label={workflow.state}
          />
        )}
      </div>
      {(error || actionError) && (
        <div className="error" role="alert">
          {actionError || error}
        </div>
      )}
      {activity && !workflow && (
        <p className="muted">This stage has not started.</p>
      )}
      {!activity && !error && <p className="muted">Loading…</p>}
      {workflow && (
        <StepList
          steps={workflow.steps}
          label={`${humanize(phase)} steps`}
          actions={approvalActions}
        />
      )}
      {children.length > 0 && (
        <>
          <h4>
            {childTitle} ({children.length})
          </h4>
          <WorkflowRows items={children} actions={approvalActions} />
        </>
      )}
    </section>
  );
}
