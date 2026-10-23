import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import type { PhaseActivity, WorkflowProgress, WorkflowStep } from "../api/types";
import { hasPermission } from "../auth/session";
import { Button } from "../components/Button";
import { StatusPill, type SemanticStatus } from "../components/StatusPill";

const POLL_MS = 15000;

const KIND_STATUS: Record<WorkflowProgress["kind"], SemanticStatus> = {
  running: "neutral",
  waiting: "warning",
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
  return Number.isNaN(date.getTime()) ? "" : `Updated ${date.toLocaleTimeString()}`;
}

export function usePhaseActivity(programId: string | undefined, phase: string | undefined, poll: boolean) {
  const [activity, setActivity] = useState<PhaseActivity | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    if (!programId || !phase) return;
    try {
      const data = await apiFetch<PhaseActivity>(
        `/admin/programs/${encodeURIComponent(programId)}/phases/${encodeURIComponent(phase)}/activity`
      );
      setActivity(data);
      setError(null);
    } catch (e: any) {
      setError(String(e?.message || e));
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
            {step.approval_pending ? "waiting for approval" : step.status}
          </span>
          {actions?.(step)}
          {step.error && <span className="muted workflow-step-error">{step.error}</span>}
        </li>
      ))}
    </ol>
  );
}

export function WorkflowRows({ items }: { items: WorkflowProgress[] }) {
  const [open, setOpen] = useState<Record<string, boolean>>({});

  return (
    <ul className="workflow-list">
      {items.map((item) => {
        const expanded = Boolean(open[item.workflow_id]);
        return (
          <li key={item.workflow_id} className="workflow-row">
            <div className="workflow-row-head">
              <button
                type="button"
                className="workflow-toggle"
                aria-expanded={expanded}
                onClick={() => setOpen((prev) => ({ ...prev, [item.workflow_id]: !expanded }))}
              >
                <span aria-hidden="true">{expanded ? "▾" : "▸"}</span>
                <strong>{item.label}</strong>
              </button>
              <StatusPill status={KIND_STATUS[item.kind] || "neutral"} label={item.state} />
              {item.revisions > 0 && (
                <span className="muted">
                  {item.revisions} {item.revisions === 1 ? "revision" : "revisions"}
                </span>
              )}
              {item.pr_url && (
                <a href={item.pr_url} target="_blank" rel="noreferrer">
                  {prLabel(item.pr_url)}
                </a>
              )}
              {item.kind === "stuck" && item.stuck_task_id && (
                <a href={`#/inbox/stuck_step/${item.stuck_task_id}`}>Decide in Inbox</a>
              )}
              <span className="muted workflow-updated">{updatedLabel(item.updated_at)}</span>
            </div>
            {expanded && <StepList steps={item.steps} label={`${item.label} steps`} />}
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
  onChanged,
}: {
  phase: string;
  activity: PhaseActivity | null;
  error?: string | null;
  programClosed?: boolean;
  onChanged?: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const canApprove = hasPermission("approve_gate") && !programClosed;
  const workflow = activity?.workflow || null;
  const children = activity?.children || [];
  const childTitle = children.length && children.every((c) => /^WO-/i.test(c.label)) ? "Work orders" : "Started workflows";

  async function decide(taskId: string, approve: boolean) {
    setBusy(taskId);
    setActionError(null);
    try {
      await apiFetch(approve ? "/admin/workflows/approve" : "/admin/workflows/reject", {
        method: "POST",
        body: JSON.stringify({ id: taskId }),
      });
      onChanged?.();
    } catch (e: any) {
      setActionError(String(e?.message || e));
    } finally {
      setBusy(null);
    }
  }

  function approvalActions(step: WorkflowStep) {
    if (!step.approval_pending || !canApprove) return null;
    return (
      <span className="workflow-step-actions">
        <a className="muted" href={`#/inbox/workflow_gate/${encodeURIComponent(step.task_id)}`}>
          Inbox
        </a>
        <Button variant="danger" size="sm" disabled={busy === step.task_id} onClick={() => void decide(step.task_id, false)}>
          Reject
        </Button>
        <Button variant="primary" size="sm" disabled={busy === step.task_id} onClick={() => void decide(step.task_id, true)}>
          Approve
        </Button>
      </span>
    );
  }

  return (
    <section className="phase-activity" aria-label={`${humanize(phase)} activity`}>
      <div className="phase-activity-head">
        <h3>{humanize(phase)}</h3>
        {workflow && <StatusPill status={KIND_STATUS[workflow.kind] || "neutral"} label={workflow.state} />}
      </div>
      {(error || actionError) && (
        <div className="error" role="alert">
          {actionError || error}
        </div>
      )}
      {activity && !workflow && <p className="muted">This stage has not started.</p>}
      {!activity && !error && <p className="muted">Loading…</p>}
      {workflow && (
        <StepList steps={workflow.steps} label={`${humanize(phase)} steps`} actions={approvalActions} />
      )}
      {children.length > 0 && (
        <>
          <h4>
            {childTitle} ({children.length})
          </h4>
          <WorkflowRows items={children} />
        </>
      )}
    </section>
  );
}
