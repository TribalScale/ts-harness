import { useEffect, useId, useRef, useState } from "react";
import { apiFetch } from "../api/client";
import type { DirectorySearch, ProgramAccessList, ProgramGrantLevel } from "../api/types";
import { Button } from "../components/Button";

const LEVELS: Array<{ id: ProgramGrantLevel; label: string }> = [
  { id: "read", label: "Can view" },
  { id: "write", label: "Can contribute" },
  { id: "approve", label: "Can approve" },
  { id: "admin", label: "Can manage" },
];

type Candidate = { type: "user" | "team"; id: string; label: string; detail?: string };

function levelLabel(level: string): string {
  return LEVELS.find((item) => item.id === level)?.label || level;
}

function candidates(result: DirectorySearch | null): Candidate[] {
  if (!result) return [];
  const teams = (result.teams || []).map((team) => ({
    type: "team" as const,
    id: team.slug,
    label: team.display_name || team.slug,
    detail: `Team · ${team.member_count ?? 0} members`,
  }));
  const users = (result.users || []).map((user) => ({
    type: "user" as const,
    id: user.subject,
    label: user.display_name || user.email || user.subject,
    detail: user.email || undefined,
  }));
  return [...teams, ...users];
}

export function ShareDialog({
  open,
  programId,
  onClose,
  onChanged,
}: {
  open: boolean;
  programId: string;
  onClose: () => void;
  onChanged?: (access: ProgramAccessList) => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleId = `${useId()}-share-title`;
  const [access, setAccess] = useState<ProgramAccessList | null>(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<DirectorySearch | null>(null);
  const [selected, setSelected] = useState<Candidate | null>(null);
  const [level, setLevel] = useState<ProgramGrantLevel>("read");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const base = `/admin/programs/${encodeURIComponent(programId)}`;

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

  useEffect(() => {
    if (!open) return;
    setError(null);
    apiFetch<ProgramAccessList>(`${base}/access`)
      .then(setAccess)
      .catch((e) => setError(String(e?.message || e)));
  }, [open, base]);

  useEffect(() => {
    if (!open || selected) return;
    const q = query.trim();
    if (!q) {
      setResults(null);
      return;
    }
    const timer = setTimeout(() => {
      apiFetch<DirectorySearch>(`/admin/directory/search?q=${encodeURIComponent(q)}`)
        .then(setResults)
        .catch((e) => setError(String(e?.message || e)));
    }, 250);
    return () => clearTimeout(timer);
  }, [open, query, selected]);

  async function run(request: Promise<ProgramAccessList>) {
    setBusy(true);
    setError(null);
    try {
      const next = await request;
      setAccess(next);
      onChanged?.(next);
      return true;
    } catch (e: any) {
      setError(String(e?.message || e));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function addGrant() {
    if (!selected) return;
    const ok = await run(
      apiFetch<ProgramAccessList>(`${base}/access`, {
        method: "POST",
        body: JSON.stringify({ principal_type: selected.type, principal_id: selected.id, level }),
      })
    );
    if (ok) {
      setSelected(null);
      setQuery("");
      setResults(null);
    }
  }

  const canManage = access?.access_level === "admin";
  const options = candidates(results);

  return (
    <dialog
      ref={dialogRef}
      aria-labelledby={titleId}
      className="dialog astryx-dialog share-dialog"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <h2 id={titleId}>Share program</h2>
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
      {access?.inherited && (
        <p className="muted">Access is shared with the whole program tree; changes apply to the parent program.</p>
      )}
      <fieldset className="share-visibility" disabled={!canManage || busy}>
        <legend>Who can see it</legend>
        <label>
          <input
            type="radio"
            name="visibility"
            checked={access?.visibility !== "public"}
            onChange={() =>
              void run(
                apiFetch<ProgramAccessList>(`${base}/visibility`, {
                  method: "PATCH",
                  body: JSON.stringify({ visibility: "private" }),
                })
              )
            }
          />
          Private: only the owner and the people and teams below
        </label>
        <label>
          <input
            type="radio"
            name="visibility"
            checked={access?.visibility === "public"}
            onChange={() =>
              void run(
                apiFetch<ProgramAccessList>(`${base}/visibility`, {
                  method: "PATCH",
                  body: JSON.stringify({ visibility: "public" }),
                })
              )
            }
          />
          Public: everyone in the company can view
        </label>
      </fieldset>

      <h3>People and teams</h3>
      <ul className="share-grants" aria-label="Program access">
        {access?.owner && (
          <li>
            <span>{access.owner.display_name || access.owner.email || access.owner.subject}</span>
            <span className="muted">Owner</span>
          </li>
        )}
        {(access?.grants || []).map((grant) => (
          <li key={`${grant.principal_type}:${grant.principal_id}`}>
            <span>
              {grant.principal_type === "team" ? `Team ${grant.principal_id}` : grant.display_name || grant.email || grant.principal_id}
            </span>
            <span className="muted">{levelLabel(grant.level)}</span>
            {canManage && (
              <Button
                variant="ghost"
                size="sm"
                disabled={busy}
                aria-label={`Remove ${grant.principal_id}`}
                onClick={() =>
                  void run(
                    apiFetch<ProgramAccessList>(
                      `${base}/access/${encodeURIComponent(grant.principal_type)}/${encodeURIComponent(grant.principal_id)}`,
                      { method: "DELETE" }
                    )
                  )
                }
              >
                Remove
              </Button>
            )}
          </li>
        ))}
      </ul>

      {canManage && (
        <div className="share-add">
          <label>
            Add a person or team
            <input
              aria-label="Search people and teams"
              value={selected ? selected.label : query}
              onChange={(event) => {
                setSelected(null);
                setQuery(event.target.value);
              }}
              placeholder="Name, email or team"
            />
          </label>
          {!selected && options.length > 0 && (
            <ul className="share-results" role="listbox" aria-label="Matches">
              {options.map((option) => (
                <li key={`${option.type}:${option.id}`}>
                  <button
                    type="button"
                    role="option"
                    aria-selected={false}
                    onClick={() => setSelected(option)}
                  >
                    <span>{option.label}</span>
                    {option.detail && <span className="muted"> {option.detail}</span>}
                  </button>
                </li>
              ))}
            </ul>
          )}
          <label>
            Access
            <select
              aria-label="Access level"
              value={level}
              onChange={(event) => setLevel(event.target.value as ProgramGrantLevel)}
            >
              {LEVELS.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <Button variant="primary" busy={busy} disabled={!selected} onClick={() => void addGrant()}>
            Add
          </Button>
        </div>
      )}
      <div className="dialog-actions">
        <Button variant="secondary" onClick={onClose}>
          Done
        </Button>
      </div>
    </dialog>
  );
}
