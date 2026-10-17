import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, apiFetch } from "../api/client";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PageState } from "../components/PageState";
import { SplitView } from "../layout/SplitView";
import type { ChildJoinState } from "./CompositionSections";
import {
  addChildJoin,
  definitionToGraph,
  detectClientGraphIssues,
  findChildJoin,
  graphToDefinition,
  removeChildJoin,
  setChildJoinTimeout,
  updateStageInGraph,
  type WorkflowGraph,
} from "./graph";
import { StartWorkflowDialog } from "./StartWorkflowDialog";
import { ValidationPanel } from "./ValidationPanel";
import { WorkflowCanvas } from "./WorkflowCanvas";
import { WorkflowInspector } from "./WorkflowInspector";
import { WorkflowLibrary } from "./WorkflowLibrary";
import type {
  ValidationIssue,
  WorkflowDefinition,
  WorkflowDefinitionCatalog,
  WorkflowDefinitionSummary,
  WorkflowDraft,
  WorkflowEditorMetadata,
  WorkflowRoute,
  WorkflowStage,
} from "./types";

const emptyCatalog: WorkflowDefinitionCatalog = {
  hooks: [],
  agents: [],
  skills: [],
  stage_kinds: ["task", "expand", "compensate"],
  semantic_validators: [],
  present_prompts: ["approval", "inform", "intake", "merge", "none"],
  notification_events: [
    "completed",
    "blocked",
    "failed",
    "awaiting_approval",
    "awaiting_external",
  ],
  templates: [],
};

function newDefinition(): WorkflowDefinition {
  return {
    workflow_type: "new_workflow",
    definition_version: "0.1.0",
    display_name: "New workflow",
    notifications: { bound_channel: true },
    promotion: { intake_stage: "first_stage" },
    stages: [{ name: "first_stage", kind: "task", agent_id: "orchestrator" }],
  };
}

function stageFromPath(definition: WorkflowDefinition, path: string): string | undefined {
  const match = path.match(/\/stages\/(\d+)(?:\/|$)/) || path.match(/(?:^|\.)stages\[(\d+)\](?:\.|$)/);
  if (!match) return undefined;
  return definition.stages[Number(match[1])]?.name;
}

function editorMetadataFromGraph(graph: WorkflowGraph, previous?: WorkflowEditorMetadata): WorkflowEditorMetadata {
  return { ...previous, layout: Object.fromEntries(graph.nodes.map((node) => [node.id, node.position])) };
}

function clearAttachQuery() {
  const raw = window.location.hash.replace(/^#/, "");
  const [path] = raw.split("?");
  if (raw.includes("?")) {
    history.replaceState(null, "", `#${path}`);
  }
}

export function WorkflowBuilderPage({
  route,
  onNavigate,
  onDirtyChange,
  allowDirtyNavigation,
  attachSkill,
  attachStage,
}: {
  route: WorkflowRoute;
  onNavigate: (route: WorkflowRoute) => void;
  onDirtyChange?: (dirty: boolean) => void;
  allowDirtyNavigation?: () => void;
  attachSkill?: string;
  attachStage?: string;
}) {
  const [catalog, setCatalog] = useState<WorkflowDefinitionCatalog>(emptyCatalog);
  const [baseDefinition, setBaseDefinition] = useState<WorkflowDefinition | null>(null);
  const [graph, setGraph] = useState<WorkflowGraph | null>(null);
  const [draft, setDraft] = useState<WorkflowDraft | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [readOnly, setReadOnly] = useState(false);
  const [selectedStageId, setSelectedStageId] = useState<string | undefined>();
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [validationIssues, setValidationIssues] = useState<ValidationIssue[]>([]);
  const [validationValid, setValidationValid] = useState<boolean | undefined>();
  const [confirmActivate, setConfirmActivate] = useState(false);
  const [startOpen, setStartOpen] = useState(false);
  const routeGenerationRef = useRef(0);
  const editGenerationRef = useRef(0);
  const preserveLocalDraftIdRef = useRef<string | null>(null);
  const pendingNoticeRef = useRef<string | null>(null);
  const routeRef = useRef(route);
  routeRef.current = route;
  const attachAppliedRef = useRef<string | null>(null);
  const [catalogReady, setCatalogReady] = useState(false);

  const currentDefinition = useMemo(() => {
    if (!baseDefinition || !graph) return null;
    return graphToDefinition(graph, baseDefinition);
  }, [baseDefinition, graph]);

  const selectedStage = currentDefinition?.stages.find((stage) => stage.name === selectedStageId);

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  const guardedNavigate = useCallback(
    (next: WorkflowRoute) => {
      onNavigate(next);
    },
    [onNavigate]
  );

  useEffect(() => {
    const handler = (event: BeforeUnloadEvent) => {
      if (!dirty) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);

  useEffect(() => {
    apiFetch<WorkflowDefinitionCatalog>("/admin/workflow-definitions/catalog")
      .then((data) => {
        setCatalog(data);
        setCatalogReady(true);
      })
      .catch(() => {
        setCatalog(emptyCatalog);
        setCatalogReady(true);
      });
  }, []);

  useEffect(() => {
    let cancelled = false;
    if (
      route.kind === "draft" &&
      preserveLocalDraftIdRef.current &&
      preserveLocalDraftIdRef.current === route.draftId
    ) {
      preserveLocalDraftIdRef.current = null;
      // Invalidate in-flight ops from the previous route, but keep local edits.
      routeGenerationRef.current += 1;
      setLoading(false);
      setBusy(null);
      setStatus("draft");
      setReadOnly(false);
      return;
    }
    const generation = ++routeGenerationRef.current;
    editGenerationRef.current += 1;
    async function load() {
      setLoading(true);
      setBusy(null);
      setError(null);
      setConflict(null);
      setNotice(null);
      setValidationIssues([]);
      setValidationValid(undefined);
      setConfirmActivate(false);
      setStartOpen(false);
      setDirty(false);
      const pendingNotice = pendingNoticeRef.current;
      pendingNoticeRef.current = null;
      try {
        if (route.kind === "library") {
          setBaseDefinition(null);
          setGraph(null);
          setDraft(null);
          setReadOnly(false);
          if (pendingNotice) setNotice(pendingNotice);
          return;
        }
        if (route.kind === "new") {
          const definition = newDefinition();
          if (cancelled || generation !== routeGenerationRef.current) return;
          setBaseDefinition(definition);
          setGraph(definitionToGraph(definition));
          setDraft(null);
          setStatus("draft");
          setReadOnly(false);
          setSelectedStageId(definition.stages[0]?.name);
          setDirty(true);
          if (pendingNotice) setNotice(pendingNotice);
          return;
        }
        if (route.kind === "draft") {
          const data = await apiFetch<{ draft: WorkflowDraft }>(
            `/admin/workflow-definitions/drafts/${encodeURIComponent(route.draftId)}`
          );
          if (cancelled || generation !== routeGenerationRef.current) return;
          setDraft(data.draft);
          setBaseDefinition(data.draft.definition);
          setGraph(definitionToGraph(data.draft.definition, data.draft.editor_metadata?.layout));
          setStatus("draft");
          setReadOnly(false);
          setSelectedStageId(data.draft.definition.stages[0]?.name);
          setDirty(false);
          if (pendingNotice) setNotice(pendingNotice);
          return;
        }
        if (route.kind === "workflow") {
          const data = await apiFetch<{
            status: string;
            read_only: boolean;
            definition: WorkflowDefinition;
          }>(`/admin/workflow-definitions/${encodeURIComponent(route.workflowType)}`);
          if (cancelled || generation !== routeGenerationRef.current) return;
          setDraft(null);
          setBaseDefinition(data.definition);
          setGraph(definitionToGraph(data.definition));
          setStatus(data.status);
          setReadOnly(Boolean(data.read_only));
          setSelectedStageId(data.definition.stages[0]?.name);
          setDirty(false);
          if (pendingNotice) setNotice(pendingNotice);
        }
      } catch (e: any) {
        if (!cancelled && generation === routeGenerationRef.current) setError(String(e.message || e));
      } finally {
        if (!cancelled && generation === routeGenerationRef.current) setLoading(false);
      }
    }
    void load();
    return () => {
      cancelled = true;
      routeGenerationRef.current += 1;
    };
  }, [route]);

  useEffect(() => {
    if (!currentDefinition || !graph) return;
    if (!attachSkill && !attachStage) return;
    if (attachSkill && !catalogReady) return;
    const token = `${route.kind}:${attachSkill || ""}:${attachStage || ""}:${currentDefinition.workflow_type}`;
    if (attachAppliedRef.current === token) return;
    attachAppliedRef.current = token;
    if (!attachSkill) {
      const stageName =
        attachStage && currentDefinition.stages.some((stage) => stage.name === attachStage)
          ? attachStage
          : undefined;
      if (!stageName) {
        setNotice(`Unknown workflow stage: ${attachStage}`);
        clearAttachQuery();
        return;
      }
      setSelectedStageId(stageName);
      clearAttachQuery();
      return;
    }
    const approved = catalog.skills.some((skill) => skill.id === attachSkill);
    if (!approved) {
      setNotice(`Cannot attach ${attachSkill}: missing or unapproved.`);
      clearAttachQuery();
      return;
    }
    const stageName =
      (attachStage && currentDefinition.stages.some((stage) => stage.name === attachStage) && attachStage) ||
      currentDefinition.stages[0]?.name;
    if (!stageName) {
      setNotice(`Cannot attach ${attachSkill}: no workflow stages.`);
      clearAttachQuery();
      return;
    }
    const stage = currentDefinition.stages.find((item) => item.name === stageName);
    if (!stage) return;
    const hints = stage.skill_hints || [];
    if (!hints.includes(attachSkill)) {
      updateGraph(updateStageInGraph(graph, stageName, { ...stage, skill_hints: [...hints, attachSkill] }));
    }
    setSelectedStageId(stageName);
    clearAttachQuery();
  }, [attachSkill, attachStage, catalog.skills, catalogReady, currentDefinition, graph, route.kind]);

  function updateGraph(next: WorkflowGraph) {
    editGenerationRef.current += 1;
    setGraph(next);
    setDirty(true);
    setValidationValid(undefined);
  }

  function updateWorkflow(next: WorkflowDefinition) {
    if (!graph) return;
    editGenerationRef.current += 1;
    const staged = graphToDefinition(graph, next);
    setBaseDefinition(staged);
    setDirty(true);
    setValidationValid(undefined);
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

  function updateChildJoin(forkStage: string, join: ChildJoinState) {
    if (!graph) return;
    try {
      const existing = findChildJoin(graph.nodes.map((node) => node.data), forkStage);
      let next = graph;
      if (!join.enabled) next = removeChildJoin(graph, forkStage);
      else if (!existing) next = addChildJoin(graph, forkStage, join.timeoutSeconds);
      else next = setChildJoinTimeout(graph, forkStage, join.timeoutSeconds);
      updateGraph(next);
      setError(null);
    } catch (e: any) {
      setError(String(e.message || e));
    }
  }

  async function saveDraft(): Promise<WorkflowDraft | null> {
    if (!currentDefinition) return null;
    const routeGeneration = routeGenerationRef.current;
    const editGeneration = editGenerationRef.current;
    const savedRoute = routeRef.current;
    const savedDraftId = draft?.id;
    setBusy("save");
    setError(null);
    setConflict(null);
    try {
      const issues = detectClientGraphIssues(graph!);
      if (issues.some((issue) => issue.severity === "error")) {
        setValidationIssues(issues);
        setValidationValid(false);
        return null;
      }
      if (draft) {
        const editor_metadata = editorMetadataFromGraph(graph!, draft.editor_metadata);
        const data = await apiFetch<{ draft: WorkflowDraft }>(
          `/admin/workflow-definitions/drafts/${encodeURIComponent(draft.id)}`,
          {
            method: "PUT",
            body: JSON.stringify({ definition: currentDefinition, expected_revision: draft.revision, editor_metadata }),
          }
        );
        if (
          routeGeneration !== routeGenerationRef.current ||
          routeRef.current.kind !== "draft" ||
          routeRef.current.draftId !== savedDraftId
        ) {
          return null;
        }
        setDraft(data.draft);
        if (editGeneration !== editGenerationRef.current) {
          // Keep newer local edits; refresh revision for the next optimistic save.
          return data.draft;
        }
        setBaseDefinition(data.draft.definition);
        setGraph(definitionToGraph(data.draft.definition, data.draft.editor_metadata?.layout || editor_metadata.layout));
        setDirty(false);
        return data.draft;
      }
      const editor_metadata = editorMetadataFromGraph(graph!);
      const data = await apiFetch<{ draft: WorkflowDraft }>("/admin/workflow-definitions/drafts", {
        method: "POST",
        body: JSON.stringify({ definition: currentDefinition, editor_metadata }),
      });
      if (
        routeGeneration !== routeGenerationRef.current ||
        savedRoute.kind !== "new" ||
        routeRef.current.kind !== "new"
      ) {
        return null;
      }
      setDraft(data.draft);
      if (editGeneration !== editGenerationRef.current) {
        // Keep newer local edits, but bind the URL/draft id so the next save updates.
        preserveLocalDraftIdRef.current = data.draft.id;
      } else {
        setBaseDefinition(data.draft.definition);
        setGraph(definitionToGraph(data.draft.definition, data.draft.editor_metadata?.layout || editor_metadata.layout));
        setDirty(false);
      }
      // Always leave #/workflows/new after the first create so the next Save is a PUT.
      allowDirtyNavigation?.();
      onNavigate({ kind: "draft", draftId: data.draft.id });
      return data.draft;
    } catch (e: any) {
      if (routeGeneration !== routeGenerationRef.current) return null;
      if (e instanceof ApiError && e.status === 409) setConflict(String(e.message || e));
      else setError(String(e.message || e));
      return null;
    } finally {
      setBusy(null);
    }
  }

  async function cloneWorkflow(item: WorkflowDefinitionSummary) {
    if (dirty && !window.confirm("Discard unsaved workflow changes?")) return;
    const routeGeneration = routeGenerationRef.current;
    const wasDirty = dirty;
    setBusy("clone");
    try {
      const data =
        item.status === "draft" && item.id
          ? await apiFetch<{ draft: WorkflowDraft }>(
              `/admin/workflow-definitions/drafts/${encodeURIComponent(item.id)}`
            )
          : await apiFetch<{ definition: WorkflowDefinition }>(
              `/admin/workflow-definitions/${encodeURIComponent(item.workflow_type)}`
            );
      const sourceDefinition = "draft" in data ? data.draft.definition : data.definition;
      const definition = {
        ...sourceDefinition,
        workflow_type: `${sourceDefinition.workflow_type}_custom`,
        definition_version: "0.1.0",
        display_name: `${sourceDefinition.display_name || sourceDefinition.workflow_type} copy`,
      };
      const created = await apiFetch<{ draft: WorkflowDraft }>("/admin/workflow-definitions/drafts", {
        method: "POST",
        body: JSON.stringify({
          definition,
          ...("draft" in data && data.draft.editor_metadata ? { editor_metadata: data.draft.editor_metadata } : {}),
        }),
      });
      if (routeGeneration !== routeGenerationRef.current) return;
      setDirty(false);
      onNavigate({ kind: "draft", draftId: created.draft.id });
    } catch (e: any) {
      if (routeGeneration !== routeGenerationRef.current) return;
      if (wasDirty) setDirty(true);
      setError(String(e.message || e));
    } finally {
      setBusy(null);
    }
  }

  async function validateDraft() {
    const routeGeneration = routeGenerationRef.current;
    const savedDraftId = draft?.id;
    const saved = dirty || !draft ? await saveDraft() : draft;
    if (!saved) return;
    if (
      routeGeneration !== routeGenerationRef.current ||
      (savedDraftId && routeRef.current.kind === "draft" && routeRef.current.draftId !== savedDraftId)
    ) {
      return;
    }
    setBusy("validate");
    try {
      const data = await apiFetch<{
        valid: boolean;
        issues: ValidationIssue[];
        normalized_definition?: WorkflowDefinition | null;
      }>(`/admin/workflow-definitions/drafts/${encodeURIComponent(saved.id)}/validate`, {
        method: "POST",
        body: JSON.stringify({ definition: currentDefinition || saved.definition }),
      });
      if (
        routeGeneration !== routeGenerationRef.current ||
        (routeRef.current.kind === "draft" && routeRef.current.draftId !== saved.id)
      ) {
        return;
      }
      setValidationIssues(data.issues || []);
      setValidationValid(data.valid);
    } catch (e: any) {
      if (routeGeneration !== routeGenerationRef.current) return;
      setError(String(e.message || e));
    } finally {
      setBusy(null);
    }
  }

  async function activateDraft() {
    if (validationValid !== true) return;
    const routeGeneration = routeGenerationRef.current;
    const saved = dirty || !draft ? await saveDraft() : draft;
    if (!saved) return;
    if (
      routeGeneration !== routeGenerationRef.current ||
      (routeRef.current.kind === "draft" && routeRef.current.draftId !== saved.id)
    ) {
      return;
    }
    setBusy("activate");
    setConflict(null);
    try {
      const result = await apiFetch<{
        reload_ok?: boolean;
        warning?: string;
        active?: { workflow_type?: string };
      }>(`/admin/workflow-definitions/drafts/${encodeURIComponent(saved.id)}/activate`, {
        method: "POST",
        body: JSON.stringify({ expected_revision: saved.revision }),
      });
      if (routeGeneration !== routeGenerationRef.current) return;
      setDirty(false);
      setConfirmActivate(false);
      if (result.reload_ok === false || result.warning) {
        pendingNoticeRef.current =
          result.warning || "Workflow definition activated but runtime reload failed.";
      } else {
        pendingNoticeRef.current = null;
      }
      allowDirtyNavigation?.();
      onNavigate({ kind: "workflow", workflowType: saved.definition.workflow_type });
    } catch (e: any) {
      if (routeGeneration !== routeGenerationRef.current) return;
      if (e instanceof ApiError && e.status === 409) setConflict(String(e.message || e));
      else setError(String(e.message || e));
    } finally {
      setBusy(null);
    }
  }

  const list = (
    <WorkflowLibrary
      selected={route}
      onNew={() => guardedNavigate({ kind: "new" })}
      onSelect={guardedNavigate}
      onClone={(item) => void cloneWorkflow(item)}
    />
  );

  let detail;
  if (route.kind === "library") {
    detail = <div className="empty">Select a workflow definition or create a draft.</div>;
  } else if (loading || !currentDefinition || !graph) {
    detail = error ? (
      <PageState kind="error" title="Could not load workflow" detail={error} />
    ) : (
      <PageState kind="loading" title="Loading workflow" />
    );
  } else {
    detail = (
      <div className="workflow-builder">
        <header className="workflow-builder-header">
          <div>
            <h2>{currentDefinition.display_name || currentDefinition.workflow_type}</h2>
            <p className="muted">
              {status || "draft"} · {currentDefinition.workflow_type} · {currentDefinition.definition_version}
            </p>
          </div>
          <div className="program-actions">
            {!readOnly && (
              <>
                <Button variant="primary" busy={busy === "save"} onClick={() => void saveDraft()}>
                  Save draft
                </Button>
                <Button variant="secondary" busy={busy === "validate"} onClick={() => void validateDraft()}>
                  Validate
                </Button>
                <Button
                  variant="danger"
                  disabled={(!draft && route.kind === "new") || validationValid !== true}
                  onClick={() => setConfirmActivate(true)}
                >
                  Activate
                </Button>
              </>
            )}
            {status === "active" && (
              <Button variant="primary" onClick={() => setStartOpen(true)}>
                Start workflow
              </Button>
            )}
          </div>
        </header>
        {conflict && (
          <div className="warning" role="alert">
            Conflict: {conflict}. Reload the draft before saving again.
          </div>
        )}
        {notice && (
          <div className="warning" role="status">
            {notice}{" "}
            <Button variant="ghost" onClick={() => setNotice(null)}>
              Dismiss
            </Button>
          </div>
        )}
        {error && (
          <div className="error" role="alert">
            {error}
          </div>
        )}
        <ValidationPanel
          valid={validationValid}
          issues={validationIssues}
          canFocusPath={(path) => Boolean(stageFromPath(currentDefinition, path))}
          onFocusPath={(path) => {
            const stageId = stageFromPath(currentDefinition, path);
            if (stageId) setSelectedStageId(stageId);
          }}
        />
        <div className="workflow-builder-grid">
          <WorkflowCanvas
            graph={graph}
            selectedStageId={selectedStageId}
            readOnly={readOnly}
            onGraphChange={updateGraph}
            onSelectStage={setSelectedStageId}
          />
          <WorkflowInspector
            definition={currentDefinition}
            selectedStage={selectedStage}
            catalog={catalog}
            readOnly={readOnly}
            onWorkflowChange={updateWorkflow}
            onStageChange={updateStage}
            onChildJoinChange={updateChildJoin}
          />
        </div>
        <ConfirmDialog
          open={confirmActivate}
          title="Activate draft?"
          description="This makes the draft available for new workflow starts. Existing runs are not changed."
          confirmLabel="Activate draft"
          busy={busy === "activate"}
          onCancel={() => setConfirmActivate(false)}
          onConfirm={() => void activateDraft()}
        />
        <StartWorkflowDialog
          open={startOpen}
          definition={currentDefinition}
          onClose={() => setStartOpen(false)}
        />
      </div>
    );
  }

  return <SplitView list={list} detail={detail} detailOpen={route.kind !== "library"} onCloseDetail={() => guardedNavigate({ kind: "library" })} />;
}
