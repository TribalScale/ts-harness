import { useCallback, useEffect, useState } from "react";
import { ApiError, apiFetch } from "../api/client";
import type {
  KnowledgeEvidence,
  KnowledgeNote,
  MemoryDetailResponse,
  MemoryListResponse,
  MemoryNote,
} from "../api/types";
import { Button } from "../components/Button";
import { PageState } from "../components/PageState";
import { StatusPill, type SemanticStatus } from "../components/StatusPill";
import { SplitView } from "../layout/SplitView";
import { NoteForm, draftFrom, payload, type NoteDraft } from "./NotesEditor";

const SEARCH_DEBOUNCE_MS = 300;
const LIST_LIMIT = 100;
const SUMMARY_CHARS = 140;

type Scope = "" | "org" | "project";
type StatusFilter = "" | KnowledgeNote["status"];
type KindFilter = "" | KnowledgeNote["kind"];

const STATUS_PILL: Record<KnowledgeNote["status"], { status: SemanticStatus; label: string }> = {
  active: { status: "success", label: "Active" },
  proposed: { status: "warning", label: "Proposed" },
  retired: { status: "muted", label: "Retired" },
  rejected: { status: "danger", label: "Rejected" },
};

const KIND_LABEL: Record<KnowledgeNote["kind"], string> = { avoid: "Avoid", howto: "How to" };

function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function formatTime(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function summary(body: string): string {
  const text = (body || "").replace(/\s+/g, " ").trim();
  return text.length > SUMMARY_CHARS ? `${text.slice(0, SUMMARY_CHARS - 1)}…` : text;
}

function scopeLabel(note: { scope: KnowledgeNote["scope"]; project_key?: string | null }): string {
  if (note.scope === "org") return "Org";
  return note.project_key ? `Project ${note.project_key}` : "Project";
}

export function memoryListUrl(filters: {
  q: string;
  scope: Scope;
  projectId: string;
  status: StatusFilter;
  kind: KindFilter;
}): string {
  const params = new URLSearchParams();
  if (filters.q.trim()) params.set("q", filters.q.trim());
  if (filters.scope) params.set("scope", filters.scope);
  if (filters.projectId) params.set("project_id", filters.projectId);
  if (filters.status) params.set("status", filters.status);
  if (filters.kind) params.set("kind", filters.kind);
  params.set("limit", String(LIST_LIMIT));
  return `/admin/knowledge/notes?${params.toString()}`;
}

function EvidenceLink({ item }: { item: KnowledgeEvidence }) {
  if (!item.url) return null;
  if (item.url.startsWith("http")) {
    return (
      <a href={item.url} target="_blank" rel="noreferrer">
        {item.url}
      </a>
    );
  }
  return <span className="mono">{item.url}</span>;
}

export function MemoryPage({
  noteId,
  onSelect,
}: {
  noteId?: string;
  onSelect: (id: string | null) => void;
}) {
  const [query, setQuery] = useState("");
  const [q, setQ] = useState("");
  const [scope, setScope] = useState<Scope>("");
  const [projectId, setProjectId] = useState("");
  const [status, setStatus] = useState<StatusFilter>("");
  const [kind, setKind] = useState<KindFilter>("");
  const [list, setList] = useState<MemoryListResponse | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [listLoading, setListLoading] = useState(false);
  const [detail, setDetail] = useState<MemoryDetailResponse | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState<NoteDraft | null>(null);

  useEffect(() => {
    const timer = setTimeout(() => setQ(query), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [query]);

  const listUrl = memoryListUrl({ q, scope, projectId, status, kind });

  const loadList = useCallback(async () => {
    setListLoading(true);
    try {
      const out = await apiFetch<MemoryListResponse>(listUrl);
      setList({ ...out, notes: out.notes || [], projects: out.projects || [] });
      setListError(null);
    } catch (e: unknown) {
      setListError(errorText(e));
    } finally {
      setListLoading(false);
    }
  }, [listUrl]);

  const loadDetail = useCallback(async () => {
    if (!noteId) {
      setDetail(null);
      return;
    }
    setDetailLoading(true);
    try {
      const out = await apiFetch<MemoryDetailResponse>(`/admin/knowledge/notes/${encodeURIComponent(noteId)}`);
      setDetail(out);
      setDetailError(null);
    } catch (e: unknown) {
      setDetail(null);
      setDetailError(errorText(e));
    } finally {
      setDetailLoading(false);
    }
  }, [noteId]);

  useEffect(() => {
    void loadList();
  }, [loadList]);

  useEffect(() => {
    setDraft(null);
    setNotice(null);
    setActionError(null);
    void loadDetail();
  }, [loadDetail]);

  async function act(run: () => Promise<unknown>) {
    setBusy(true);
    setNotice(null);
    setActionError(null);
    try {
      await run();
      setDraft(null);
    } catch (e: unknown) {
      if (e instanceof ApiError && e.status === 409) {
        setDraft(null);
        setNotice("Changed by someone else. The note was reloaded; check it and try again.");
      } else {
        setActionError(errorText(e));
      }
    } finally {
      setBusy(false);
      await Promise.all([loadList(), loadDetail()]);
    }
  }

  function setNoteStatus(note: MemoryNote, next: "active" | "rejected" | "retired") {
    void act(() =>
      apiFetch(`/admin/notes/${encodeURIComponent(note.id)}/status`, {
        method: "POST",
        body: JSON.stringify({ status: next, expected_version: note.version }),
      }),
    );
  }

  function saveEdit(note: MemoryNote, value: NoteDraft) {
    const { kind: nextKind, title, body, why, applies_to } = payload(value);
    void act(() =>
      apiFetch(`/admin/notes/${encodeURIComponent(note.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ expected_version: note.version, title, body, why, kind: nextKind, applies_to }),
      }),
    );
  }

  const notes = list?.notes || [];
  const projects = list?.projects || [];

  const listPane = (
    <div className="panel-pad memory-list">
      <h2>Memory</h2>
      <p className="muted">Reviewed notes that every run for a project sees: org rules and project notes.</p>
      <label className="memory-search">
        Search
        <input
          type="search"
          aria-label="Search notes"
          placeholder="Search notes"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </label>
      <div className="notes-filters">
        <label>
          Scope
          <select aria-label="Scope filter" value={scope} onChange={(e) => setScope(e.target.value as Scope)}>
            <option value="">All</option>
            <option value="org">Org</option>
            <option value="project">Project</option>
          </select>
        </label>
        <label>
          Project
          <select aria-label="Project filter" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
            <option value="">All projects</option>
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.key} · {project.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Status
          <select aria-label="Status filter" value={status} onChange={(e) => setStatus(e.target.value as StatusFilter)}>
            <option value="">All</option>
            <option value="active">Active</option>
            <option value="proposed">Proposed</option>
            <option value="retired">Retired</option>
            <option value="rejected">Rejected</option>
          </select>
        </label>
        <label>
          Kind
          <select aria-label="Kind filter" value={kind} onChange={(e) => setKind(e.target.value as KindFilter)}>
            <option value="">All</option>
            <option value="avoid">Avoid</option>
            <option value="howto">How to</option>
          </select>
        </label>
      </div>
      {list?.mode === "text" && (
        <p className="muted memory-hint" role="note">
          Semantic search is unavailable; results are full-text matches only.
        </p>
      )}
      {listError && list && (
        <PageState kind="stale" title="Notes may be stale" detail={listError} onRetry={() => void loadList()} />
      )}
      {!list && listLoading ? (
        <PageState kind="loading" title="Loading notes" />
      ) : !list && listError ? (
        <PageState kind="error" title="Could not load notes" detail={listError} onRetry={() => void loadList()} />
      ) : list && notes.length === 0 ? (
        <PageState kind="empty" title="No notes" detail={q ? "Nothing matches this search." : "No notes match these filters."} />
      ) : (
        <ul className="memory-rows" aria-label="Notes">
          {notes.map((note) => {
            const pill = STATUS_PILL[note.status];
            return (
              <li key={note.id}>
                <button
                  className={`row-card ${noteId === note.id ? "selected" : ""}`}
                  title={note.title}
                  onClick={() => onSelect(note.id)}
                >
                  <span className="row-title">{note.title}</span>
                  <span className="memory-summary muted">{summary(note.body)}</span>
                  <span className="row-meta">
                    <span className="proj-tag">{KIND_LABEL[note.kind] || note.kind}</span>
                    <span className="row-subtitle">{scopeLabel(note)}</span>
                    {pill && <StatusPill status={pill.status} label={pill.label} />}
                    {note.source === "drafted" && <span className="proj-tag memory-drafted">Drafted from a run</span>}
                  </span>
                  <span className="row-meta muted memory-row-facts">
                    <span>Used by {note.use_count || 0} runs</span>
                    {note.updated_at && <span>Updated {formatTime(note.updated_at)}</span>}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );

  function renderDetail() {
    if (!noteId) {
      return <PageState kind="empty" title="Select a note" detail="Choose a note to see its evidence and history." />;
    }
    if (detailLoading && !detail) return <PageState kind="loading" title="Loading note" />;
    if (detailError || !detail) {
      return (
        <PageState
          kind="error"
          title="Could not load note"
          detail={detailError || "Note not found."}
          onRetry={() => void loadDetail()}
        />
      );
    }
    const note = detail.note;
    const pill = STATUS_PILL[note.status];
    const canWrite = Boolean(detail.can_write && note.can_write !== false);
    const evidence = note.evidence || [];
    const draftedFrom = detail.drafted_from?.length
      ? detail.drafted_from
      : evidence.filter((item) => item.type === "drafted_from");
    const projectKey = detail.project?.key || note.project_key;
    return (
      <section className="inbox-detail memory-detail" aria-label="Note detail">
        <header className="detail-header">
          <div>
            <h2>{note.title}</h2>
            <p className="mono muted">{note.id} · version {note.version}</p>
          </div>
          {pill && <StatusPill status={pill.status} label={pill.label} />}
        </header>
        {notice && (
          <p className="memory-notice" role="alert">
            {notice}
          </p>
        )}
        {actionError && (
          <p className="field-error" role="alert">
            {actionError}
          </p>
        )}
        <div className="detail-sections">
          <section className="detail-card" aria-label="Note">
            <h3>{KIND_LABEL[note.kind] || note.kind}</h3>
            <p>{note.body}</p>
            {note.why && (
              <p className="muted">
                <strong>Why:</strong> {note.why}
              </p>
            )}
            <dl className="memory-facts">
              <dt>Scope</dt>
              <dd>{note.scope === "org" ? "Org" : "Project"}</dd>
              {note.scope === "project" && (
                <>
                  <dt>Project</dt>
                  <dd>{detail.project ? `${detail.project.key} · ${detail.project.name}` : projectKey || note.project_id}</dd>
                </>
              )}
              <dt>Source</dt>
              <dd>{note.source}</dd>
              <dt>Used by</dt>
              <dd>{note.use_count || 0} runs</dd>
              {note.proposed_by && (
                <>
                  <dt>Proposed by</dt>
                  <dd>{note.proposed_by}</dd>
                </>
              )}
              {note.approved_by && (
                <>
                  <dt>Approved by</dt>
                  <dd>{note.approved_by}</dd>
                </>
              )}
              {note.created_at && (
                <>
                  <dt>Created</dt>
                  <dd>{formatTime(note.created_at)}</dd>
                </>
              )}
              {note.updated_at && (
                <>
                  <dt>Updated</dt>
                  <dd>{formatTime(note.updated_at)}</dd>
                </>
              )}
            </dl>
            {(note.applies_to || []).length > 0 && (
              <ul className="memory-chips" aria-label="Applies to">
                {note.applies_to.map((item) => (
                  <li key={item} className="proj-tag">
                    {item}
                  </li>
                ))}
              </ul>
            )}
          </section>
          {draftedFrom.length > 0 && (
            <section className="detail-card" aria-label="Drafted from">
              <h3>Drafted from</h3>
              <ul>
                {draftedFrom.map((item, index) => (
                  <li key={`${item.ref || ""}-${index}`}>
                    {item.ref && <span className="mono">{item.ref}</span>} <EvidenceLink item={item} />
                    {item.excerpt && <p className="muted">{item.excerpt}</p>}
                  </li>
                ))}
              </ul>
            </section>
          )}
          <section className="detail-card" aria-label="Evidence">
            <h3>Evidence</h3>
            {evidence.length === 0 ? (
              <p className="muted">No evidence recorded.</p>
            ) : (
              <ul>
                {evidence.map((item, index) => (
                  <li key={`${item.type || ""}-${item.ref || ""}-${index}`}>
                    <span className="proj-tag">{item.type || "evidence"}</span>{" "}
                    {item.ref && <span className="mono">{item.ref}</span>} <EvidenceLink item={item} />
                    {item.excerpt && <p className="muted">{item.excerpt}</p>}
                  </li>
                ))}
              </ul>
            )}
          </section>
          <section className="detail-card" aria-label="Version history">
            <h3>Version history</h3>
            {(detail.versions || []).length === 0 ? (
              <p className="muted">No earlier versions.</p>
            ) : (
              <ul>
                {detail.versions.map((item) => (
                  <li key={item.version}>
                    <strong>Version {item.version}</strong>
                    {item.changed_by && <> · {item.changed_by}</>}
                    {item.change_reason && <> · {item.change_reason}</>}
                    {item.created_at && <span className="muted"> · {formatTime(item.created_at)}</span>}
                  </li>
                ))}
              </ul>
            )}
          </section>
          {draft && canWrite && note.status === "active" && (
            <section className="detail-card" aria-label="Edit note">
              <h3>Edit note</h3>
              <NoteForm
                draft={draft}
                busy={busy}
                allowException={false}
                onChange={setDraft}
                onSubmit={() => saveEdit(note, draft)}
                submitLabel="Save changes"
              />
              <Button variant="ghost" onClick={() => setDraft(null)}>
                Cancel
              </Button>
            </section>
          )}
        </div>
        {canWrite && (note.status === "proposed" || (note.status === "active" && !draft)) && (
          <footer className="detail-actions" aria-label="Note actions">
            {note.status === "proposed" && (
              <>
                <Button variant="danger" busy={busy} onClick={() => setNoteStatus(note, "rejected")}>
                  Reject
                </Button>
                <Button variant="primary" busy={busy} onClick={() => setNoteStatus(note, "active")}>
                  Approve
                </Button>
              </>
            )}
            {note.status === "active" && (
              <>
                <Button variant="danger" busy={busy} onClick={() => setNoteStatus(note, "retired")}>
                  Archive
                </Button>
                <Button variant="secondary" busy={busy} onClick={() => setDraft(draftFrom(note))}>
                  Edit
                </Button>
              </>
            )}
          </footer>
        )}
      </section>
    );
  }

  return (
    <SplitView
      className="memory-split"
      list={listPane}
      detail={renderDetail()}
      detailOpen={Boolean(noteId)}
      onCloseDetail={() => onSelect(null)}
    />
  );
}
