import { useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import type { KnowledgeNote } from "../api/types";
import { Button } from "../components/Button";
import { NotesEditor } from "../knowledge/NotesEditor";

export function ProjectNotesPanel({
  projectId,
  canManage,
  isAdmin,
}: {
  projectId: string;
  canManage: boolean;
  isAdmin: boolean;
}) {
  const [orgNotes, setOrgNotes] = useState<KnowledgeNote[]>([]);
  const [projectNotes, setProjectNotes] = useState<KnowledgeNote[]>([]);

  useEffect(() => {
    apiFetch<{ notes: KnowledgeNote[] }>("/admin/notes")
      .then((out) => setOrgNotes((out.notes || []).filter((note) => note.status === "active")))
      .catch(() => setOrgNotes([]));
  }, []);

  useEffect(() => {
    apiFetch<{ notes: KnowledgeNote[] }>(`/admin/projects/${encodeURIComponent(projectId)}/notes`)
      .then((out) => setProjectNotes(out.notes || []))
      .catch(() => setProjectNotes([]));
  }, [projectId]);

  async function promote(note: KnowledgeNote) {
    await apiFetch(`/admin/projects/${encodeURIComponent(projectId)}/notes/${encodeURIComponent(note.id)}/promote`, {
      method: "POST",
      body: JSON.stringify({}),
    });
  }

  return (
    <section aria-label="Project notes">
      <h4>Org rules</h4>
      {orgNotes.length === 0 ? (
        <p className="muted">No org rules yet.</p>
      ) : (
        <ul>
          {orgNotes.map((note) => (
            <li key={note.id}>
              [{note.kind}] {note.title}: {note.body}
              {isAdmin && <span className="muted"> Exception id {note.id}</span>}
            </li>
          ))}
        </ul>
      )}
      <h4>Project notes</h4>
      {canManage && projectNotes.filter((note) => note.status === "active").map((note) => (
        <p key={`promote-${note.id}`}>
          <Button variant="ghost" onClick={() => void promote(note)}>Promote {note.title} to org</Button>
        </p>
      ))}
      <NotesEditor
        listUrl={`/admin/projects/${encodeURIComponent(projectId)}/notes`}
        canWrite={canManage}
        isAdmin={isAdmin}
        allowException
      />
    </section>
  );
}
