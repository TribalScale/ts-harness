import { useEffect, useMemo, useState } from "react";
import { apiFetch } from "../api/client";
import { Button } from "../components/Button";
import { PlusIcon } from "../components/PlusIcon";
import { PageState } from "../components/PageState";
import { Tabs, tabButtonId, tabPanelId } from "../components/Tabs";
import {
  LIBRARY_STATUS_LABELS,
  LIBRARY_STATUSES,
  defaultLibraryStatus,
  type LibraryStatus,
} from "./libraryStatus";
import type { WorkflowDefinitionSummary, WorkflowRoute } from "./types";

function labelFor(item: WorkflowDefinitionSummary): string {
  return item.display_name || item.workflow_type;
}

export function WorkflowLibrary({
  selected,
  onNew,
  onSelect,
  onClone,
}: {
  selected: WorkflowRoute;
  onNew: () => void;
  onSelect: (route: WorkflowRoute) => void;
  onClone: (item: WorkflowDefinitionSummary) => void;
}) {
  const [definitions, setDefinitions] = useState<WorkflowDefinitionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusTab, setStatusTab] = useState<LibraryStatus>("built_in");

  async function load() {
    setLoading(true);
    try {
      const data = await apiFetch<{ definitions: WorkflowDefinitionSummary[] }>(
        "/admin/workflow-definitions"
      );
      setDefinitions(data.definitions || []);
      setError(null);
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const groups = {
    draft: definitions.filter((item) => item.status === "draft"),
    active: definitions.filter((item) => item.status === "active"),
    built_in: definitions.filter((item) => item.status === "built_in"),
  };
  const selectedStatus = useMemo<LibraryStatus | undefined>(() => {
    if (selected.kind === "draft") return "draft";
    if (selected.kind === "workflow") {
      const match = definitions.find((item) => item.workflow_type === selected.workflowType);
      if (match?.status === "active" || match?.status === "built_in" || match?.status === "draft") {
        return match.status;
      }
    }
    return undefined;
  }, [definitions, selected]);

  useEffect(() => {
    if (!definitions.length) return;
    setStatusTab(defaultLibraryStatus(groups, selectedStatus));
  }, [definitions.length, selectedStatus]);

  return (
    <div className="workflow-library panel-pad">
      <div className="workflow-library-header">
        <div>
          <h2>Workflows</h2>
          <p className="muted">Create drafts, clone managed definitions, and view built-ins.</p>
        </div>
        <Button variant="primary" type="button" icon={<PlusIcon />} onClick={onNew}>
          New workflow
        </Button>
      </div>
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
      {loading && !definitions.length && <PageState kind="loading" title="Loading workflows" />}
      {!loading && !definitions.length && (
        <PageState kind="empty" title="No workflow definitions" detail="Create a workflow draft to begin." />
      )}
      {Boolean(definitions.length) && (
        <>
          <Tabs
            idBase="workflow-library-status"
            ariaLabel="Workflow definition status"
            active={statusTab}
            onChange={(id) => setStatusTab(id as LibraryStatus)}
            tabs={LIBRARY_STATUSES.map((status) => ({
              id: status,
              label: LIBRARY_STATUS_LABELS[status],
              count: groups[status].length,
            }))}
          />
          {LIBRARY_STATUSES.map((status) => (
            <section
              key={status}
              id={tabPanelId("workflow-library-status", status)}
              role="tabpanel"
              hidden={statusTab !== status}
              aria-labelledby={tabButtonId("workflow-library-status", status)}
              className="workflow-library-section"
              aria-label={`${status} workflows`}
            >
              {groups[status].map((item) => {
                const route =
                  item.status === "draft" && item.id
                    ? ({ kind: "draft", draftId: item.id } as WorkflowRoute)
                    : ({ kind: "workflow", workflowType: item.workflow_type } as WorkflowRoute);
                const selectedClass =
                  (selected.kind === "draft" && selected.draftId === item.id) ||
                  (selected.kind === "workflow" && selected.workflowType === item.workflow_type)
                    ? "selected"
                    : "";
                return (
                  <article
                    key={`${item.status}:${item.id || item.workflow_type}`}
                    className={`workflow-card ${selectedClass}`}
                  >
                    <button className="workflow-card-main" onClick={() => onSelect(route)}>
                      <span className="row-title">{labelFor(item)}</span>
                      <span className="row-meta">
                        <span className="mono">{item.workflow_type}</span>
                        <span>{item.definition_version || "draft"}</span>
                      </span>
                    </button>
                    <div className="row-actions">
                      {item.status === "draft" && item.id ? (
                        <Button
                          variant="secondary"
                          size="sm"
                          type="button"
                          onClick={() => onSelect({ kind: "draft", draftId: item.id! })}
                        >
                          Edit
                        </Button>
                      ) : (
                        <Button variant="secondary" size="sm" type="button" onClick={() => onSelect(route)}>
                          View
                        </Button>
                      )}
                      <Button
                        variant="secondary"
                        size="sm"
                        type="button"
                        aria-label={`Clone ${item.workflow_type}`}
                        onClick={() => onClone(item)}
                      >
                        Clone
                      </Button>
                    </div>
                  </article>
                );
              })}
            </section>
          ))}
        </>
      )}
    </div>
  );
}
