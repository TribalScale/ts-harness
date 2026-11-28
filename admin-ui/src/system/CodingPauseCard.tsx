import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import type { PauseState } from "../api/types";
import { PauseBadge, PauseButton } from "../live/PauseControls";

const KIND_TEXT: Record<string, string> = {
  revision: "revision waiting",
  first_run: "first coding run waiting",
  refresh: "branch refresh waiting",
  retry: "retry waiting",
  queue_start: "new tickets waiting",
};

/**
 * Pause all coding (admins), and every ticket, ticket queue and program that is paused now.
 * Pausing everything stops the coding launcher from starting queued jobs; running jobs, code
 * review and QA finish. It replaces stopping the launcher container by hand.
 */
export function CodingPauseCard() {
  const [state, setState] = useState<PauseState | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const raw = await apiFetch<Partial<PauseState>>("/admin/pause");
      setState({
        global: raw.global || null,
        programs: Array.isArray(raw.programs) ? raw.programs : [],
        workflows: (Array.isArray(raw.workflows) ? raw.workflows : []).map((w) => ({
          ...w,
          held: Array.isArray(w.held) ? w.held : [],
        })),
        held: Array.isArray(raw.held) ? raw.held : [],
        can_pause_global: raw.can_pause_global === true,
      });
      setError(null);
    } catch (e: any) {
      setError(String(e?.message || e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (!state) {
    return error ? <p className="muted">Coding pause state unavailable: {error}</p> : null;
  }
  const paused = Boolean(state.global);
  return (
    <section className="detail-card coding-pause" aria-label="Coding pause">
      <h3>Coding</h3>
      {paused && state.global ? (
        <PauseBadge entry={state.global} own />
      ) : (
        <p className="muted">
          Coding runs start as usual. Pausing all coding stops the launcher from starting queued jobs; running jobs, code
          reviews and QA finish, and nothing queued is lost.
        </p>
      )}
      {state.can_pause_global ? (
        <PauseButton scope="global" paused={paused} label="all coding" onChanged={() => void load()} />
      ) : (
        <p className="muted">Only an admin can pause or resume all coding.</p>
      )}
      {(state.programs.length > 0 || state.workflows.length > 0) && (
        <>
          <h4>Paused now</h4>
          <ul className="coding-pause-list">
            {state.programs.map((program) => (
              <li key={`p-${program.program_id}`}>
                <a href={`#/programs/${program.program_id}`}>Program {program.label || program.program_id}</a>{" "}
                <span className="muted">
                  by {program.by || "someone"}
                  {program.at ? ` at ${new Date(program.at).toLocaleString()}` : ""}
                </span>{" "}
                <PauseButton scope="program" targetId={program.program_id} paused label={program.label || "program"} onChanged={() => void load()} />
              </li>
            ))}
            {state.workflows.map((workflow) => (
              <li key={`w-${workflow.workflow_id}`}>
                {workflow.kind === "queue" ? "Ticket queue" : "Ticket"} {workflow.label || workflow.workflow_id}{" "}
                <span className="muted">
                  by {workflow.by || "someone"}
                  {workflow.at ? ` at ${new Date(workflow.at).toLocaleString()}` : ""}
                  {workflow.held.length > 0
                    ? ` · ${workflow.held.map((t) => KIND_TEXT[t.hold.kind || ""] || "held").join(", ")}`
                    : ""}
                </span>{" "}
                <PauseButton scope="ticket" targetId={workflow.workflow_id} paused label={workflow.label || "ticket"} onChanged={() => void load()} />
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
