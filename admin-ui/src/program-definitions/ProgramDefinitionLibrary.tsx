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
} from "../workflows/libraryStatus";
import type { ProgramDefinitionSummary, ProgramRoute } from "./types";

function labelFor(item: ProgramDefinitionSummary): string {
  return item.display_name || item.program_type;
}

export function ProgramDefinitionLibrary({
  selected,
  onNew,
  onBuildWithAgent,
  onSelect,
  onClone,
}: {
  selected: ProgramRoute;
  onNew?: () => void;
  onBuildWithAgent?: () => void;
  onSelect: (route: ProgramRoute) => void;
  onClone?: (item: ProgramDefinitionSummary) => void;
}) {
  const [definitions, setDefinitions] = useState<ProgramDefinitionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusTab, setStatusTab] = useState<LibraryStatus>("built_in");

  async function load() {
    setLoading(true);
    try {
      const data = await apiFetch<{ definitions: ProgramDefinitionSummary[] }>(
        "/admin/program-definitions"
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
    if (selected.kind === "program") {
      const match = definitions.find((item) => item.program_type === selected.programType);
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
          <h2>Program definitions</h2>
          <p className="muted">
            Create drafts, clone managed definitions, and inspect built-in topology maps.
          </p>
        </div>
        <div className="workflow-library-actions">
          {onBuildWithAgent && (
            <Button variant="secondary" type="button" onClick={onBuildWithAgent}>
              Build with agent
            </Button>
          )}
          {onNew && (
            <Button variant="primary" type="button" icon={<PlusIcon />} onClick={onNew}>
              New program
            </Button>
          )}
        </div>
      </div>
      {loading && !definitions.length && <PageState kind="loading" title="Loading program definitions" />}
      {!loading && !definitions.length && !error && (
        <PageState
          kind="empty"
          title="No program definitions"
          detail="Create a program draft or clone a built-in map to begin."
        />
      )}
      {!loading && error && !definitions.length && (
        <PageState kind="error" title="Could not load program definitions" detail={error} onRetry={() => void load()} />
      )}
      {Boolean(definitions.length) && (
        <>
          <Tabs
            idBase="program-library-status"
            ariaLabel="Program definition status"
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
              id={tabPanelId("program-library-status", status)}
              role="tabpanel"
              hidden={statusTab !== status}
              aria-labelledby={tabButtonId("program-library-status", status)}
              className="workflow-library-section"
              aria-label={`${status} program definitions`}
            >
              {groups[status].map((item) => {
                const route =
                  item.status === "draft" && item.id
                    ? ({ kind: "draft", draftId: item.id } as ProgramRoute)
                    : ({ kind: "program", programType: item.program_type } as ProgramRoute);
                const selectedClass =
                  (selected.kind === "draft" && selected.draftId === item.id) ||
                  (selected.kind === "program" && selected.programType === item.program_type)
                    ? "selected"
                    : "";
                return (
                  <article
                    key={`${item.status}:${item.id || item.program_type}`}
                    className={`workflow-card ${selectedClass}`}
                  >
                    <button className="workflow-card-main" onClick={() => onSelect(route)}>
                      <span className="row-title">{labelFor(item)}</span>
                      <span className="row-meta">
                        <span className="mono">{item.program_type}</span>
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
                      {onClone && (
                        <Button
                          variant="secondary"
                          size="sm"
                          type="button"
                          aria-label={`Clone ${item.program_type}`}
                          onClick={() => onClone(item)}
                        >
                          Clone
                        </Button>
                      )}
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
