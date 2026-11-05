import { useCallback, useEffect, useState } from "react";
import { ApiError, apiFetch } from "../api/client";
import type { KnowledgeNote } from "../api/types";
import { Button } from "../components/Button";
import { PageState } from "../components/PageState";

export interface NoteDraft {
  kind: "avoid" | "howto";
  title: string;
  body: string;
  why: string;
  appliesTo: string;
  exceptionTo: string;
}

const EMPTY: NoteDraft = { kind: "avoid", title: "", body: "", why: "", appliesTo: "", exceptionTo: "" };

function draftFrom(note: KnowledgeNote): NoteDraft {
  return {
    kind: note.kind,
    title: note.title,
    body: note.body,
    why: note.why || "",
    appliesTo: (note.applies_to || []).join(", "),
    exceptionTo: note.exception_to || "",
  };
}

function payload(draft: NoteDraft): Record<string, unknown> {
  return {
    kind: draft.kind,
    title: draft.title.trim(),
    body: draft.body.trim(),
    why: draft.why.trim(),
    applies_to: draft.appliesTo.split(",").map((item) => item.trim()).filter(Boolean),
    ...(draft.exceptionTo.trim() ? { exception_to: draft.exceptionTo.trim() } : {}),
  };
}

export function NotesEditor({
  listUrl,
  canWrite,
  isAdmin = false,
  showUseCount = false,
  allowException = false,
}: {
  listUrl: string;
  canWrite: boolean;
  isAdmin?: boolean;
  showUseCount?: boolean;
  allowException?: boolean;
}) {
  const [notes, setNotes] = useState<KnowledgeNote[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [draft, setDraft] = useState<NoteDraft | null>(null);
  const [editing, setEditing] = useState<KnowledgeNote | null>(null);
  const [history, setHistory] = useState<KnowledgeNote | null>(null);
  const [versions, setVersions] = useState<{ version: number; change_reason: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const [kind, setKind] = useState<"all" | "avoid" | "howto">("all");
  const [status, setStatus] = useState<"all" | KnowledgeNote["status"]>("active");

  const load = useCallback(async () => {
    try {
      const out = await apiFetch<{ notes: KnowledgeNote[] }>(listUrl);
      setNotes(out.notes || []);
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [listUrl]);

  useEffect(() => {
    void load();
  }, [load]);

  function fail(e: unknown) {
    if (e instanceof ApiError && e.status === 409) {
      setConflict(true);
      void load();
      return;
    }
    setError(e instanceof Error ? e.message : String(e));
  }

  async function create() {
    if (!draft) return;
    setBusy(true);
    setConflict(false);
    try {
      await apiFetch(listUrl, { method: "POST", body: JSON.stringify(payload(draft)) });
      setDraft(null);
      await load();
    } catch (e: unknown) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    if (!editing) return;
    setBusy(true);
    setConflict(false);
    try {
      await apiFetch(`/admin/notes/${encodeURIComponent(editing.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ ...payload(draft || draftFrom(editing)), expected_version: editing.version }),
      });
      setEditing(null);
      await load();
    } catch (e: unknown) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }

  async function setNoteStatus(note: KnowledgeNote, next: "retired" | "active") {
    setBusy(true);
    setConflict(false);
    try {
      await apiFetch(`/admin/notes/${encodeURIComponent(note.id)}/status`, {
        method: "POST",
        body: JSON.stringify({ status: next, expected_version: note.version }),
      });
      await load();
    } catch (e: unknown) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }

  async function openHistory(note: KnowledgeNote) {
    setHistory(note);
    try {
      const out = await apiFetch<{ versions: { version: number; change_reason: string }[] }>(
        `/admin/notes/${encodeURIComponent(note.id)}/versions`,
      );
      setVersions(out.versions || []);
    } catch (e: unknown) {
      fail(e);
    }
  }

  async function restore(version: number) {
    if (!history) return;
    setBusy(true);
    setConflict(false);
    try {
      await apiFetch(`/admin/notes/${encodeURIComponent(history.id)}/restore`, {
        method: "POST",
        body: JSON.stringify({ version, expected_version: history.version }),
      });
      setHistory(null);
      await load();
    } catch (e: unknown) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }

  const shown = (notes || []).filter((note) => (kind === "all" || note.kind === kind) && (status === "all" || note.status === status));

  return (
    <section className="notes-editor" aria-label="Knowledge notes">
      <div className="notes-filters">
        <label>
          Kind
          <select aria-label="Note kind filter" value={kind} onChange={(e) => setKind(e.target.value as typeof kind)}>
            <option value="all">All</option>
            <option value="avoid">Avoid</option>
            <option value="howto">How to</option>
          </select>
        </label>
        <label>
          Status
          <select aria-label="Note status filter" value={status} onChange={(e) => setStatus(e.target.value as typeof status)}>
            <option value="active">Active</option>
            <option value="proposed">Proposed</option>
            <option value="retired">Retired</option>
            <option value="rejected">Rejected</option>
            <option value="all">All</option>
          </select>
        </label>
        {canWrite && (
          <Button variant="secondary" onClick={() => { setDraft(EMPTY); setEditing(null); }}>
            New note
          </Button>
        )}
      </div>
      {conflict && <p role="alert">Changed by someone else. The list was reloaded.</p>}
      {error && <p className="field-error" role="alert">{error}</p>}
      {notes === null && <PageState kind="loading" title="Loading notes" />}
      {notes && shown.length === 0 && <PageState kind="empty" title="No notes" detail="Notes that are active are loaded into every run." />}
      <ul>
        {shown.map((note) => (
          <li key={note.id}>
            <strong>[{note.kind}] {note.title}</strong>
            <p>{note.body}</p>
            {note.why && <p className="muted">{note.why}</p>}
            {showUseCount && <p className="muted">Used by {note.use_count} runs</p>}
            {canWrite && (
              <span>
                <Button variant="ghost" onClick={() => { setEditing(note); setDraft(draftFrom(note)); }}>Edit</Button>
                {note.status === "active" && (
                  <Button variant="ghost" onClick={() => void setNoteStatus(note, "retired")}>Retire</Button>
                )}
                <Button variant="ghost" onClick={() => void openHistory(note)}>History</Button>
              </span>
            )}
          </li>
        ))}
      </ul>
      {(draft && !editing) && (
        <NoteForm
          draft={draft}
          busy={busy}
          allowException={allowException && isAdmin}
          onChange={setDraft}
          onSubmit={() => void create()}
          submitLabel="Save note"
        />
      )}
      {editing && draft && (
        <NoteForm
          draft={draft}
          busy={busy}
          allowException={allowException && isAdmin}
          onChange={setDraft}
          onSubmit={() => void save()}
          submitLabel="Save changes"
        />
      )}
      {history && (
        <div aria-label="Version history">
          <h4>History for {history.title}</h4>
          <ul>
            {versions.map((item) => (
              <li key={item.version}>
                Version {item.version} {item.change_reason}
                {canWrite && item.version !== history.version && (
                  <Button variant="ghost" onClick={() => void restore(item.version)}>Restore</Button>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function NoteForm({
  draft,
  busy,
  allowException,
  onChange,
  onSubmit,
  submitLabel,
}: {
  draft: NoteDraft;
  busy: boolean;
  allowException: boolean;
  onChange: (draft: NoteDraft) => void;
  onSubmit: () => void;
  submitLabel: string;
}) {
  return (
    <form
      aria-label="Note form"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <label>
        Kind
        <select aria-label="Kind" value={draft.kind} onChange={(e) => onChange({ ...draft, kind: e.target.value as NoteDraft["kind"] })}>
          <option value="avoid">Avoid</option>
          <option value="howto">How to</option>
        </select>
      </label>
      <label>
        Title
        <input aria-label="Title" maxLength={120} value={draft.title} onChange={(e) => onChange({ ...draft, title: e.target.value })} />
      </label>
      <label>
        Note
        <textarea aria-label="Note" maxLength={300} value={draft.body} onChange={(e) => onChange({ ...draft, body: e.target.value })} />
      </label>
      <label>
        Why
        <input aria-label="Why" maxLength={500} value={draft.why} onChange={(e) => onChange({ ...draft, why: e.target.value })} />
      </label>
      <label>
        Applies to
        <input aria-label="Applies to" value={draft.appliesTo} onChange={(e) => onChange({ ...draft, appliesTo: e.target.value })} />
      </label>
      {allowException && (
        <label>
          Exception to org note
          <input aria-label="Exception to" value={draft.exceptionTo} onChange={(e) => onChange({ ...draft, exceptionTo: e.target.value })} />
        </label>
      )}
      <Button type="submit" busy={busy} disabled={!draft.title.trim() || !draft.body.trim()}>{submitLabel}</Button>
    </form>
  );
}
