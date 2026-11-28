import { useState } from "react";
import { apiFetch } from "../api/client";
import type { PauseEntry, PauseResult, PauseScope } from "../api/types";

const SCOPE_TEXT: Record<string, string> = {
  ticket: "this ticket",
  queue: "its ticket queue",
  program: "its program",
  global: "all coding",
};

/** "Paused (its program) by alice@example.com at 2026-11-28 10:15". */
export function pauseText(entry: PauseEntry, opts: { own?: boolean } = {}): string {
  const when = entry.at ? new Date(entry.at).toLocaleString() : "";
  const scope = opts.own ? "" : ` (${SCOPE_TEXT[entry.scope || ""] || entry.scope})`;
  return `Paused${scope} by ${entry.by || "someone"}${when ? ` at ${when}` : ""}`;
}

/** Who paused it and when, with the reason when one was given. */
export function PauseBadge({ entry, own = false }: { entry: PauseEntry; own?: boolean }) {
  return (
    <div className="pause-badge" role="status">
      <strong>{pauseText(entry, { own })}</strong>
      {entry.reason && <span className="muted"> · {entry.reason}</span>}
    </div>
  );
}

/**
 * Pause or resume a ticket (or ticket queue), a program, or all coding. Steps already running
 * finish; the next coding run waits. Pausing twice or resuming something not paused is a no-op.
 */
export function PauseButton({
  scope,
  targetId = "",
  paused,
  label,
  onChanged,
}: {
  scope: PauseScope;
  targetId?: string;
  paused: boolean;
  label: string;
  onChanged?: (result: PauseResult) => void;
}) {
  const [asking, setAsking] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function act(action: "pause" | "resume") {
    setBusy(true);
    setError(null);
    try {
      const body: Record<string, string> = { scope, reason: action === "pause" ? reason.trim() : "" };
      if (scope === "ticket") body.workflow_id = targetId;
      if (scope === "program") body.program_id = targetId;
      const result = await apiFetch<PauseResult>(`/admin/${action}`, { method: "POST", body: JSON.stringify(body) });
      setMessage(result.message || null);
      setAsking(false);
      setReason("");
      onChanged?.(result);
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  }

  const noun = scope === "global" ? "all coding" : label || "it";
  return (
    <span className="pause-control">
      {paused ? (
        <button type="button" className="link-button" disabled={busy} onClick={() => void act("resume")}>
          {busy ? "Resuming…" : `Resume ${noun}`}
        </button>
      ) : asking ? (
        <span className="pause-ask">
          <input
            type="text"
            aria-label={`Reason for pausing ${noun} (optional)`}
            placeholder="Reason (optional)"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
          <button type="button" className="link-button" disabled={busy} onClick={() => void act("pause")}>
            {busy ? "Pausing…" : "Confirm pause"}
          </button>
          <button type="button" className="link-button" disabled={busy} onClick={() => setAsking(false)}>
            Cancel
          </button>
        </span>
      ) : (
        <button type="button" className="link-button" onClick={() => setAsking(true)}>
          {`Pause ${noun}`}
        </button>
      )}
      {message && (
        <span className="muted pause-message" role="status">
          {message}
        </span>
      )}
      {error && (
        <span className="error-banner" role="alert">
          {error}
        </span>
      )}
    </span>
  );
}
