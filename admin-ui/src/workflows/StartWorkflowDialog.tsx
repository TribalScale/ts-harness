import { useEffect, useId, useRef, useState } from "react";
import { apiFetch } from "../api/client";
import { Button } from "../components/Button";
import type { WorkflowDefinition } from "./types";

export function StartWorkflowDialog({
  open,
  definition,
  onClose,
}: {
  open: boolean;
  definition: WorkflowDefinition;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const [summary, setSummary] = useState("");
  const [input, setInput] = useState("{}");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ workflow_id?: string; session_id?: string } | null>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    } else if (!open && dialog.open) {
      if (typeof dialog.close === "function") dialog.close();
      else dialog.removeAttribute("open");
    }
  }, [open]);

  async function start() {
    setBusy(true);
    setError(null);
    try {
      const parsed = input.trim() ? JSON.parse(input) : {};
      const data = await apiFetch<{ workflow_id?: string; session_id?: string }>(
        `/admin/workflow-definitions/${encodeURIComponent(definition.workflow_type)}/start`,
        {
          method: "POST",
          body: JSON.stringify({ summary, input: parsed }),
        }
      );
      setResult(data);
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <dialog
      ref={dialogRef}
      className="dialog dialog-form start-workflow-dialog"
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <h2 id={titleId}>Start {definition.display_name || definition.workflow_type}</h2>
      <label className="form-field">
        <span>Summary</span>
        <input aria-label="Workflow summary" value={summary} onChange={(e) => setSummary(e.target.value)} />
      </label>
      <label className="form-field">
        <span>Optional JSON input</span>
        <textarea aria-label="Workflow input JSON" value={input} onChange={(e) => setInput(e.target.value)} />
      </label>
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
      {result && (
        <div className="success" role="status">
          Started workflow{" "}
          <a href="#/programs">{result.workflow_id || result.session_id || "open programs"}</a>
        </div>
      )}
      <div className="dialog-actions">
        <Button variant="secondary" onClick={onClose}>
          Close
        </Button>
        <Button variant="primary" busy={busy} disabled={!summary.trim()} onClick={start}>
          Start workflow
        </Button>
      </div>
    </dialog>
  );
}
