import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import type { ProjectChecklist, ProjectChecklistItem } from "../api/types";
import { Button } from "../components/Button";

const STATUS_LABEL: Record<ProjectChecklistItem["status"], string> = {
  ok: "Ready",
  warning: "Warning",
  failing: "Failing",
};

function ChecklistRow({ item }: { item: ProjectChecklistItem }) {
  return (
    <li className={`proj-check proj-check-${item.status}`} data-check={item.id}>
      <span className={`proj-check-status proj-check-status-${item.status}`}>
        {STATUS_LABEL[item.status]}
      </span>
      <div className="proj-check-body">
        <strong>{item.title}</strong>
        <p className="proj-check-summary">{item.summary}</p>
        {item.details.length > 0 && (
          <ul className="proj-check-details">
            {item.details.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        )}
        {item.status !== "ok" && item.fix && (
          <p className="proj-check-fix">
            <span className="muted">How to fix: </span>
            {item.fix}
            {item.fix_link && (
              <>
                {" "}
                <a href={item.fix_link}>Open</a>
              </>
            )}
          </p>
        )}
      </div>
    </li>
  );
}

export function ProjectChecklistPanel({ projectId }: { projectId: string }) {
  const url = `/admin/projects/${encodeURIComponent(projectId)}/checklist`;
  const [checklist, setChecklist] = useState<ProjectChecklist | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);

  const load = useCallback(async () => {
    setChecking(true);
    try {
      const out = await apiFetch<ProjectChecklist>(url);
      if (!Array.isArray(out?.items)) {
        throw new Error("The checklist response was not understood.");
      }
      setChecklist(out);
      setError(null);
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setChecking(false);
    }
  }, [url]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section aria-label="Setup checklist" className="proj-card">
      <header className="proj-card-head">
        <div>
          <h3>Setup checklist</h3>
          <p className="muted">
            What this project needs before work can flow, checked live against
            Jira, GitHub and the settings.
            {checklist && (
              <>
                {" "}
                <strong className="proj-check-total">
                  {checklist.ready} of {checklist.total} ready
                </strong>
              </>
            )}
          </p>
        </div>
        <Button
          variant="secondary"
          size="sm"
          disabled={checking}
          onClick={() => void load()}
        >
          {checking ? "Checking…" : "Re-check"}
        </Button>
      </header>
      {error && (
        <p className="field-error" role="alert">
          {error}
        </p>
      )}
      {!checklist && !error && <p className="muted">Checking…</p>}
      {checklist && (
        <ul className="proj-checklist">
          {checklist.items.map((item) => (
            <ChecklistRow key={item.id} item={item} />
          ))}
        </ul>
      )}
    </section>
  );
}
