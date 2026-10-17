import React, { useMemo, useState } from "react";
import { apiFetch } from "../api/client";
import type { WorkflowTask } from "../api/types";
import { Button } from "../components/Button";
import { useMediaQuery } from "../layout/useMediaQuery";

type Col = "pending" | "running" | "blocked" | "done";

function columnFor(task: WorkflowTask): Col {
  const status = String(task.status || "").toLowerCase();
  if (
    task.approval_required &&
    task.approval_status === "pending" &&
    ["pending", "blocked", "running"].includes(status)
  ) {
    return "blocked";
  }
  if (status === "blocked") return "blocked";
  if (status === "running") return "running";
  if (status === "succeeded" || status === "cancelled" || status === "failed") return "done";
  return "pending";
}

const COLS: { key: Col; label: string }[] = [
  { key: "pending", label: "Pending" },
  { key: "running", label: "Running" },
  { key: "blocked", label: "Blocked / waiting" },
  { key: "done", label: "Done" },
];

export function TaskKanban({
  tasks,
  programStatus,
}: {
  tasks: WorkflowTask[];
  programStatus?: string;
}) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [mobileCol, setMobileCol] = useState<Col>("blocked");
  const isMobile = !useMediaQuery("(min-width: 1024px)");

  const grouped = useMemo(() => {
    const g: Record<Col, WorkflowTask[]> = {
      pending: [],
      running: [],
      blocked: [],
      done: [],
    };
    for (const task of tasks) g[columnFor(task)].push(task);
    return g;
  }, [tasks]);

  async function decide(taskId: string, approve: boolean) {
    setBusy(taskId);
    setError(null);
    try {
      await apiFetch(approve ? "/admin/workflows/approve" : "/admin/workflows/reject", {
        method: "POST",
        body: JSON.stringify({ id: taskId }),
      });
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(null);
    }
  }

  function renderCard(task: WorkflowTask) {
    const id = String(task.id || "");
    const terminal = ["succeeded", "cancelled", "failed"].includes(
      String(task.status || "").toLowerCase()
    );
    const programClosed = ["succeeded", "cancelled", "failed"].includes(
      String(programStatus || "").toLowerCase()
    );
    const needsApproval =
      task.approval_required &&
      task.approval_status === "pending" &&
      !terminal &&
      !programClosed;
    return (
      <div key={id} className="kanban-card">
        <strong>{task.step_name || id}</strong>
        <div className="muted">{task.agent_id || "—"}</div>
        <div className="mono muted">{task.status}</div>
        {needsApproval && (
          <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
            <a className="muted" href={`#/inbox/workflow_gate/${encodeURIComponent(id)}`}>
              Inbox
            </a>
            <Button
              type="button"
              variant="danger"
              disabled={busy === id}
              onClick={() => decide(id, false)}
            >
              Reject
            </Button>
            <Button
              type="button"
              variant="primary"
              disabled={busy === id}
              onClick={() => decide(id, true)}
            >
              Approve
            </Button>
          </div>
        )}
      </div>
    );
  }

  function renderColumnTasks(col: Col) {
    if (!grouped[col].length) return <div className="kanban-empty">No tasks</div>;
    return grouped[col].map(renderCard);
  }

  return (
    <div>
      <h3>Active phase tasks</h3>
      {error && <div className="error">{error}</div>}
      {isMobile ? (
        <>
          <div className="filters">
            {COLS.map((c) => (
              <button
                key={c.key}
                type="button"
                aria-pressed={mobileCol === c.key}
                className={mobileCol === c.key ? "active" : ""}
                onClick={() => setMobileCol(c.key)}
              >
                {c.label} ({grouped[c.key].length})
              </button>
            ))}
          </div>
          <div className="kanban-col">{renderColumnTasks(mobileCol)}</div>
        </>
      ) : (
        <div className="kanban">
          {COLS.map((c) => (
            <div key={c.key} className="kanban-col">
              <h4>
                {c.label} ({grouped[c.key].length})
              </h4>
              {renderColumnTasks(c.key)}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
