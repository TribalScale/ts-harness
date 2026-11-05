import { useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import { NotesEditor } from "./NotesEditor";

export function OrgNotesPage() {
  const [canWrite, setCanWrite] = useState(false);

  useEffect(() => {
    apiFetch<{ can_write?: boolean }>("/admin/notes")
      .then((out) => setCanWrite(Boolean(out.can_write)))
      .catch(() => setCanWrite(false));
  }, []);

  return (
    <section aria-label="Org notes">
      <h3>Org notes</h3>
      <p className="muted">Rules that apply to every project. Only an admin can change them.</p>
      <NotesEditor listUrl="/admin/notes" canWrite={canWrite} isAdmin={canWrite} showUseCount />
    </section>
  );
}
