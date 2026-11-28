import { useState } from "react";
import { apiFetch } from "../api/client";
import type { WorkOrderAmendments } from "../api/types";
import { hasPermission } from "../auth/session";

/**
 * "Amend work order" on a ticket: append a `## Scope amendment (by <you>, <date>)` to its work
 * order, which coding, code review and QA read from their next run. Lists earlier amendments.
 */
export function AmendWorkOrder({ workflowId, label, onDone }: { workflowId: string; label: string; onDone?: () => void }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [history, setHistory] = useState<WorkOrderAmendments | null>(null);

  async function loadHistory() {
    try {
      setHistory(await apiFetch<WorkOrderAmendments>(`/admin/workflows/${encodeURIComponent(workflowId)}/amendments`));
    } catch {
      setHistory(null);
    }
  }

  function toggle() {
    const next = !open;
    setOpen(next);
    setDone(null);
    setError(null);
    if (next) void loadHistory();
  }

  async function submit() {
    setSaving(true);
    setError(null);
    try {
      await apiFetch("/admin/workflows/amend", {
        method: "POST",
        body: JSON.stringify({ workflow_id: workflowId, text }),
      });
      setText("");
      setDone(`Amended ${label}. Coding, code review and QA read it from their next run.`);
      void loadHistory();
      onDone?.();
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setSaving(false);
    }
  }

  // Same permission as other decisions on a ticket's steps; the server checks it and program access too.
  if (!hasPermission("approve_gate")) return null;
  return (
    <>
      <button type="button" className="link-button" aria-expanded={open} onClick={toggle}>
        Amend work order
      </button>
      {open && (
        <div className="amend-work-order" aria-label={`Amend ${label}`}>
          <label>
            <span className="muted">
              Added to the work order as a scope amendment with your name and today's date. It wins over the rest of the
              work order for coding, code review and QA.
            </span>
            <textarea
              rows={4}
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder="For example: row surfaces (#F3F4F6) belong to PROJ-12; judge only the schedule blocks here."
            />
          </label>
          <button type="button" disabled={saving || !text.trim()} onClick={() => void submit()}>
            {saving ? "Amending…" : "Add amendment"}
          </button>
          {done && <div className="muted" role="status">{done}</div>}
          {error && <div className="error-banner" role="alert">{error}</div>}
          {history && history.amendments.length > 0 && (
            <ol className="amend-history" aria-label={`${label} amendments`}>
              {history.amendments.map((item) => (
                <li key={item.id}>
                  <div className="muted">
                    {item.person} · {item.date}
                    {item.source === "pr_comment" ? " · from a PR comment" : ""}
                  </div>
                  <div>{item.text}</div>
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </>
  );
}
