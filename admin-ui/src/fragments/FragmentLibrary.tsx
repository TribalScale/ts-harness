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
import type { FragmentRoute, FragmentSummary } from "../workflows/types";

export function FragmentLibrary({
  selected,
  canEdit,
  onNew,
  onSelect,
}: {
  selected: FragmentRoute;
  canEdit: boolean;
  onNew: () => void;
  onSelect: (route: FragmentRoute) => void;
}) {
  const [fragments, setFragments] = useState<FragmentSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusTab, setStatusTab] = useState<LibraryStatus>("built_in");

  useEffect(() => {
    let cancelled = false;
    apiFetch<{ fragments: FragmentSummary[] }>("/admin/workflow-fragments")
      .then((data) => {
        if (cancelled) return;
        setFragments(data.fragments || []);
        setError(null);
      })
      .catch((e: any) => {
        if (!cancelled) setError(String(e.message || e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selected.kind]);

  const groups = {
    draft: fragments.filter((item) => item.status === "draft"),
    active: fragments.filter((item) => item.status === "active"),
    built_in: fragments.filter((item) => item.status === "built_in"),
  };
  const selectedStatus = useMemo<LibraryStatus | undefined>(() => {
    if (selected.kind === "draft") return "draft";
    if (selected.kind === "fragment") {
      const match = fragments.find((item) => item.fragment_id === selected.fragmentId);
      if (match?.status === "active" || match?.status === "built_in" || match?.status === "draft") {
        return match.status;
      }
    }
    return undefined;
  }, [fragments, selected]);

  useEffect(() => {
    if (!fragments.length) return;
    setStatusTab(defaultLibraryStatus(groups, selectedStatus));
  }, [fragments.length, selectedStatus]);

  return (
    <div className="workflow-library panel-pad">
      <div className="workflow-library-header">
        <div>
          <h2>Fragments</h2>
          <p className="muted">Reusable stage groups that workflows include by id and version.</p>
        </div>
        {canEdit && (
          <Button variant="primary" type="button" icon={<PlusIcon />} onClick={onNew}>
            New fragment
          </Button>
        )}
      </div>
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
      {loading && !fragments.length && <PageState kind="loading" title="Loading fragments" />}
      {!loading && !fragments.length && !error && (
        <PageState kind="empty" title="No fragments" detail="Create a fragment draft to begin." />
      )}
      {Boolean(fragments.length) && (
        <>
          <Tabs
            idBase="fragment-library-status"
            ariaLabel="Fragment status"
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
              id={tabPanelId("fragment-library-status", status)}
              role="tabpanel"
              hidden={statusTab !== status}
              aria-labelledby={tabButtonId("fragment-library-status", status)}
              className="workflow-library-section"
              aria-label={`${status} fragments`}
            >
              {groups[status].map((item) => {
                const route: FragmentRoute =
                  item.status === "draft" && item.id
                    ? { kind: "draft", draftId: item.id }
                    : { kind: "fragment", fragmentId: item.fragment_id };
                const isSelected =
                  (selected.kind === "draft" && route.kind === "draft" && selected.draftId === route.draftId) ||
                  (selected.kind === "fragment" &&
                    route.kind === "fragment" &&
                    selected.fragmentId === route.fragmentId);
                return (
                  <article
                    key={`${item.status}:${item.id || item.fragment_id}`}
                    className={`workflow-card ${isSelected ? "selected" : ""}`}
                  >
                    <button className="workflow-card-main" onClick={() => onSelect(route)}>
                      <span className="row-title">{item.display_name || item.fragment_id}</span>
                      <span className="row-meta">
                        <span className="mono">{item.fragment_id}</span>
                        <span>{item.definition_version || "draft"}</span>
                        {item.stage_names && <span>{item.stage_names.length} stages</span>}
                      </span>
                    </button>
                    <div className="row-actions">
                      <Button variant="secondary" size="sm" type="button" onClick={() => onSelect(route)}>
                        {item.status === "draft" ? "Edit" : "View"}
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
