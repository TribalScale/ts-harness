import { useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import type { SkillDraftDetail } from "../api/types";

const SOURCE_LABELS: Record<string, string> = {
  learn: "/learn in chat",
  admin_paste: "Pasted in admin",
  admin_upload: "Folder upload",
};

export function SkillDraftProvenance({ detail }: { detail: SkillDraftDetail }) {
  const provenance = detail.provenance || {};
  const createdBy = String(provenance.created_by || provenance.created_by_agent || "");
  const source = String(provenance.source || "");
  if (!createdBy && !source) return null;
  return (
    <section className="detail-card">
      <h3>Created by</h3>
      <p>
        {createdBy || "Unknown"}
        {source ? ` · ${SOURCE_LABELS[source] || source}` : ""}
      </p>
    </section>
  );
}

export function SkillDraftFiles({ skillId, detail }: { skillId: string; detail: SkillDraftDetail }) {
  const files = detail.files || [];
  const [openPath, setOpenPath] = useState<string | null>(null);
  const [content, setContent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setOpenPath(null);
    setContent(null);
    setError(null);
  }, [skillId]);

  async function openFile(path: string) {
    setOpenPath(path);
    setContent(null);
    setError(null);
    try {
      const data = await apiFetch<{ path: string; content: string }>(
        `/admin/skills/drafts/${encodeURIComponent(skillId)}/files/${path
          .split("/")
          .map(encodeURIComponent)
          .join("/")}`
      );
      setContent(data.content);
    } catch (e: any) {
      setError(String(e.message || e));
    }
  }

  if (detail.layout !== "folder") return null;
  return (
    <section className="detail-card">
      <h3>Files ({files.length})</h3>
      <ul aria-label="Draft files" className="mono">
        {files.map((file) => {
          const depth = file.path.split("/").length - 1;
          const name = file.path.split("/").pop();
          return (
            <li key={file.path} style={{ paddingLeft: `${depth * 16}px` }}>
              <button
                type="button"
                className="link-button"
                aria-label={`Open ${file.path}`}
                aria-pressed={openPath === file.path}
                onClick={() => void openFile(file.path)}
              >
                {depth ? `${file.path.split("/").slice(0, -1).join("/")}/` : ""}
                {name}
              </button>{" "}
              <span className="muted">{file.size} B</span>
              {(file.scan_findings || []).length > 0 && (
                <span className="error"> · {(file.scan_findings || []).join("; ")}</span>
              )}
            </li>
          );
        })}
      </ul>
      {openPath && (
        <div aria-label="File viewer">
          <h4 className="mono">{openPath}</h4>
          {error ? (
            <p className="error" role="alert">
              {error}
            </p>
          ) : content === null ? (
            <p className="muted">Loading…</p>
          ) : (
            <pre className="mono">{content}</pre>
          )}
        </div>
      )}
    </section>
  );
}

export function SkillDraftRevisionDiffs({ detail, revisionOf }: { detail: SkillDraftDetail; revisionOf: string }) {
  const diffs = detail.revision_diffs || [];
  if (detail.layout !== "folder") {
    return (
      <section className="detail-card">
        <h3>Revision of {revisionOf}</h3>
        <pre className="mono">{String(detail.revision_diff || "No diff available.")}</pre>
      </section>
    );
  }
  return (
    <section className="detail-card">
      <h3>Revision of {revisionOf}</h3>
      {diffs.length === 0 ? (
        <p className="muted">No file changes.</p>
      ) : (
        diffs.map((item) => (
          <details key={item.path} open={diffs.length <= 3}>
            <summary className="mono">
              {item.path} · {item.status}
            </summary>
            <pre className="mono">{item.diff || ""}</pre>
          </details>
        ))
      )}
    </section>
  );
}
