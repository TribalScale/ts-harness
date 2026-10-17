import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, apiFetch } from "../api/client";
import { hasPermission } from "../auth/session";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PageState } from "../components/PageState";
import { SplitView } from "../layout/SplitView";
import { definitionToGraph, detectClientGraphIssues, graphToDefinition, updateStageInGraph, type WorkflowGraph } from "../workflows/graph";
import type {
  FragmentDefinition,
  FragmentDraft,
  FragmentRoute,
  ValidationIssue,
  WorkflowDefinition,
  WorkflowStage,
} from "../workflows/types";
import { ValidationPanel } from "../workflows/ValidationPanel";
import { WorkflowCanvas } from "../workflows/WorkflowCanvas";
import { FragmentInspector } from "./FragmentInspector";
import { FragmentLibrary } from "./FragmentLibrary";

function newFragment(): FragmentDefinition {
  return {
    fragment_id: "new_fragment",
    definition_version: "0.1.0",
    display_name: "New fragment",
    params: {},
    inputs: {},
    stages: [{ name: "first_stage", kind: "task", agent_id: "orchestrator" }],
  };
}

export function bumpPatch(version: string): string {
  const match = version.match(/^(\d+)\.(\d+)\.(\d+)$/);
  if (!match) return `${version}.1`;
  return `${match[1]}.${match[2]}.${Number(match[3]) + 1}`;
}

function asWorkflow(definition: FragmentDefinition): WorkflowDefinition {
  return definition as unknown as WorkflowDefinition;
}

function stageFromPath(definition: FragmentDefinition, path: string): string | undefined {
  const match = path.match(/\/stages\/(\d+)(?:\/|$)/) || path.match(/(?:^|\.)stages\[(\d+)\](?:\.|$)/);
  if (!match) return undefined;
  return definition.stages[Number(match[1])]?.name;
}

export function FragmentBuilderPage({
  route,
  onNavigate,
  onDirtyChange,
  allowDirtyNavigation,
}: {
  route: FragmentRoute;
  onNavigate: (route: FragmentRoute) => void;
  onDirtyChange?: (dirty: boolean) => void;
  allowDirtyNavigation?: () => void;
}) {
  const canEdit = hasPermission("edit_config");
  const [base, setBase] = useState<FragmentDefinition | null>(null);
  const [graph, setGraph] = useState<WorkflowGraph | null>(null);
  const [draft, setDraft] = useState<FragmentDraft | null>(null);
  const [status, setStatus] = useState("draft");
  const [sourceReadOnly, setSourceReadOnly] = useState(false);
  const [selectedStageId, setSelectedStageId] = useState<string | undefined>();
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [issues, setIssues] = useState<ValidationIssue[]>([]);
  const [valid, setValid] = useState<boolean | undefined>();
  const [confirmActivate, setConfirmActivate] = useState(false);
  const generationRef = useRef(0);

  const readOnly = !canEdit || sourceReadOnly || status !== "draft";
  const current = useMemo<FragmentDefinition | null>(() => {
    if (!base || !graph) return null;
    return graphToDefinition(graph, asWorkflow(base)) as unknown as FragmentDefinition;
  }, [base, graph]);
  const selectedStage = current?.stages.find((stage) => stage.name === selectedStageId);

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  function show(definition: FragmentDefinition, nextStatus: string, layout?: Record<string, { x: number; y: number }>) {
    setBase(definition);
    setGraph(definitionToGraph(asWorkflow(definition), layout));
    setStatus(nextStatus);
    setSelectedStageId(definition.stages[0]?.name);
  }

  useEffect(() => {
    const generation = ++generationRef.current;
    setError(null);
    setIssues([]);
    setValid(undefined);
    setDirty(false);
    setConfirmActivate(false);
    setSourceReadOnly(false);
    if (route.kind === "library") {
      setBase(null);
      setGraph(null);
      setDraft(null);
      return;
    }
    if (route.kind === "new") {
      setDraft(null);
      show(newFragment(), "draft");
      setDirty(true);
      return;
    }
    setLoading(true);
    const request =
      route.kind === "draft"
        ? apiFetch<{ draft: FragmentDraft }>(`/admin/workflow-fragments/drafts/${encodeURIComponent(route.draftId)}`).then(
            (data) => {
              if (generation !== generationRef.current) return;
              setDraft(data.draft);
              show(data.draft.definition, "draft", data.draft.editor_metadata?.layout);
            }
          )
        : apiFetch<{ status: string; read_only: boolean; definition: FragmentDefinition }>(
            `/admin/workflow-fragments/${encodeURIComponent(route.fragmentId)}`
          ).then((data) => {
            if (generation !== generationRef.current) return;
            setDraft(null);
            setSourceReadOnly(Boolean(data.read_only));
            show(data.definition, data.status);
          });
    request
      .catch((e: any) => {
        if (generation === generationRef.current) setError(String(e.message || e));
      })
      .finally(() => {
        if (generation === generationRef.current) setLoading(false);
      });
  }, [route]);

  function updateGraph(next: WorkflowGraph) {
    setGraph(next);
    setDirty(true);
    setValid(undefined);
  }

  function updateDefinition(next: FragmentDefinition) {
    if (!graph) return;
    setBase(graphToDefinition(graph, asWorkflow(next)) as unknown as FragmentDefinition);
    setDirty(true);
    setValid(undefined);
  }

  function updateStage(stage: WorkflowStage) {
    if (!graph || !selectedStageId) return;
    try {
      updateGraph(updateStageInGraph(graph, selectedStageId, stage));
      setSelectedStageId(stage.name);
      setError(null);
    } catch (e: any) {
      setError(String(e.message || e));
    }
  }

  async function saveDraft(): Promise<FragmentDraft | null> {
    if (!current || !graph) return null;
    const clientIssues = detectClientGraphIssues(graph);
    if (clientIssues.length) {
      setIssues(clientIssues);
      setValid(false);
      return null;
    }
    const editor_metadata = { layout: Object.fromEntries(graph.nodes.map((node) => [node.id, node.position])) };
    setBusy("save");
    setError(null);
    try {
      const data = draft
        ? await apiFetch<{ draft: FragmentDraft }>(`/admin/workflow-fragments/drafts/${encodeURIComponent(draft.id)}`, {
            method: "PUT",
            body: JSON.stringify({ definition: current, expected_revision: draft.revision, editor_metadata }),
          })
        : await apiFetch<{ draft: FragmentDraft }>("/admin/workflow-fragments/drafts", {
            method: "POST",
            body: JSON.stringify({ definition: current, editor_metadata }),
          });
      setDraft(data.draft);
      setDirty(false);
      if (!draft) {
        allowDirtyNavigation?.();
        onNavigate({ kind: "draft", draftId: data.draft.id });
      }
      return data.draft;
    } catch (e: any) {
      setError(e instanceof ApiError && e.status === 409 ? `Conflict: ${e.message}` : String(e.message || e));
      return null;
    } finally {
      setBusy(null);
    }
  }

  async function newVersionDraft() {
    if (!current) return;
    setBusy("clone");
    try {
      const data = await apiFetch<{ draft: FragmentDraft }>("/admin/workflow-fragments/drafts", {
        method: "POST",
        body: JSON.stringify({ definition: { ...current, definition_version: bumpPatch(current.definition_version) } }),
      });
      onNavigate({ kind: "draft", draftId: data.draft.id });
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(null);
    }
  }

  async function validate() {
    const saved = dirty || !draft ? await saveDraft() : draft;
    if (!saved) return;
    setBusy("validate");
    try {
      const data = await apiFetch<{ valid: boolean; issues: ValidationIssue[] }>(
        `/admin/workflow-fragments/drafts/${encodeURIComponent(saved.id)}/validate`,
        { method: "POST", body: JSON.stringify({ definition: current || saved.definition }) }
      );
      setIssues(data.issues || []);
      setValid(data.valid);
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(null);
    }
  }

  async function activate() {
    if (!draft || valid !== true) return;
    setBusy("activate");
    try {
      await apiFetch(`/admin/workflow-fragments/drafts/${encodeURIComponent(draft.id)}/activate`, {
        method: "POST",
        body: JSON.stringify({ expected_revision: draft.revision }),
      });
      setConfirmActivate(false);
      setDirty(false);
      allowDirtyNavigation?.();
      onNavigate({ kind: "fragment", fragmentId: draft.fragment_id });
    } catch (e: any) {
      setError(e instanceof ApiError && e.status === 409 ? `Conflict: ${e.message}` : String(e.message || e));
    } finally {
      setBusy(null);
    }
  }

  const list = (
    <FragmentLibrary
      selected={route}
      canEdit={canEdit}
      onNew={() => onNavigate({ kind: "new" })}
      onSelect={onNavigate}
    />
  );

  let detail;
  if (route.kind === "library") {
    detail = <div className="empty">Select a fragment or create a draft.</div>;
  } else if (loading || !current || !graph) {
    detail = error ? (
      <PageState kind="error" title="Could not load fragment" detail={error} />
    ) : (
      <PageState kind="loading" title="Loading fragment" />
    );
  } else {
    detail = (
      <div className="workflow-builder">
        <header className="workflow-builder-header">
          <div>
            <h2>{current.display_name || current.fragment_id}</h2>
            <p className="muted">
              {status} · {current.fragment_id} · {current.definition_version}
            </p>
          </div>
          <div className="program-actions">
            {!readOnly && (
              <>
                <Button variant="primary" busy={busy === "save"} onClick={() => void saveDraft()}>
                  Save draft
                </Button>
                <Button variant="secondary" busy={busy === "validate"} onClick={() => void validate()}>
                  Validate
                </Button>
                <Button variant="danger" disabled={!draft || valid !== true} onClick={() => setConfirmActivate(true)}>
                  Activate
                </Button>
              </>
            )}
            {canEdit && status === "active" && (
              <Button variant="secondary" busy={busy === "clone"} onClick={() => void newVersionDraft()}>
                New version draft
              </Button>
            )}
          </div>
        </header>
        {error && (
          <div className="error" role="alert">
            {error}
          </div>
        )}
        <ValidationPanel
          valid={valid}
          issues={issues}
          canFocusPath={(path) => Boolean(stageFromPath(current, path))}
          onFocusPath={(path) => setSelectedStageId(stageFromPath(current, path))}
        />
        <div className="workflow-builder-grid">
          <WorkflowCanvas
            graph={graph}
            selectedStageId={selectedStageId}
            readOnly={readOnly}
            onGraphChange={updateGraph}
            onSelectStage={setSelectedStageId}
            allowFragments={false}
          />
          <FragmentInspector
            definition={current}
            selectedStage={selectedStage}
            readOnly={readOnly}
            onChange={updateDefinition}
            onStageChange={updateStage}
          />
        </div>
        <ConfirmDialog
          open={confirmActivate}
          title="Activate fragment?"
          description="Workflows can then include this fragment version. Workflows pinned to other versions are not changed."
          confirmLabel="Activate fragment"
          busy={busy === "activate"}
          onCancel={() => setConfirmActivate(false)}
          onConfirm={() => void activate()}
        />
      </div>
    );
  }

  return (
    <SplitView
      list={list}
      detail={detail}
      detailOpen={route.kind !== "library"}
      onCloseDetail={() => onNavigate({ kind: "library" })}
    />
  );
}
