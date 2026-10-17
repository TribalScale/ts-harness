import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, apiFetch } from "../api/client";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PageState } from "../components/PageState";
import { SplitView } from "../layout/SplitView";
import { ProgramCanvas } from "./ProgramCanvas";
import { ProgramDefinitionLibrary } from "./ProgramDefinitionLibrary";
import { ProgramInspector, type EditableProgramNode } from "./ProgramInspector";
import { ProgramValidationPanel } from "./ProgramValidationPanel";
import {
  definitionToProgramGraph,
  detectClientProgramGraphIssues,
  graphToProgramDefinition,
  updateEdgeInGraph,
  updateNodeInGraph,
  type ProgramGraph,
} from "./programGraph";
import type {
  ProgramDefinition,
  ProgramDefinitionCatalog,
  ProgramDefinitionSummary,
  ProgramDraft,
  ProgramEdge,
  ProgramEditorMetadata,
  ProgramRoute,
  ValidationIssue,
} from "./types";

const emptyCatalog: ProgramDefinitionCatalog = {
  workflows: [],
  adapters: [],
  transition_modes: ["manual", "automatic"],
  templates: [],
};

function newDefinition(): ProgramDefinition {
  return {
    program_type: "new_program",
    definition_version: "0.1.0",
    display_name: "New program",
    startable_nodes: ["first_node"],
    nodes: [
      {
        id: "first_node",
        default_workflow_type: "",
        workflow_variants: [],
        required_artifacts: [],
        produces_artifacts: [],
        input_bindings: [],
        completion_adapter: "",
        terminal: true,
      },
    ],
    edges: [],
  };
}

function nodeFromPath(definition: ProgramDefinition, path: string): string | undefined {
  const match = path.match(/\/nodes\/(\d+)(?:\/|$)/) || path.match(/(?:^|\.)nodes\[(\d+)\](?:\.|$)/);
  if (!match) return undefined;
  return definition.nodes[Number(match[1])]?.id;
}

function editorMetadataFromGraph(
  graph: ProgramGraph,
  previous?: ProgramEditorMetadata
): ProgramEditorMetadata {
  return { ...previous, layout: Object.fromEntries(graph.nodes.map((node) => [node.id, node.position])) };
}

export function ProgramDefinitionBuilderPage({
  route,
  onNavigate,
  onDirtyChange,
  allowDirtyNavigation,
  onOpenWorkflow,
}: {
  route: ProgramRoute;
  onNavigate: (route: ProgramRoute) => void;
  onDirtyChange?: (dirty: boolean) => void;
  allowDirtyNavigation?: () => void;
  onOpenWorkflow?: (workflowType: string) => void;
}) {
  const [catalog, setCatalog] = useState<ProgramDefinitionCatalog>(emptyCatalog);
  const [baseDefinition, setBaseDefinition] = useState<ProgramDefinition | null>(null);
  const [graph, setGraph] = useState<ProgramGraph | null>(null);
  const [draft, setDraft] = useState<ProgramDraft | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [readOnly, setReadOnly] = useState(false);
  const [selectedNodeId, setSelectedNodeId] = useState<string | undefined>();
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | undefined>();
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [validationIssues, setValidationIssues] = useState<ValidationIssue[]>([]);
  const [validationValid, setValidationValid] = useState<boolean | undefined>();
  const [confirmActivate, setConfirmActivate] = useState(false);
  const routeGenerationRef = useRef(0);
  const editGenerationRef = useRef(0);
  const preserveLocalDraftIdRef = useRef<string | null>(null);
  const pendingNoticeRef = useRef<string | null>(null);
  const routeRef = useRef(route);
  routeRef.current = route;

  const currentDefinition = useMemo(() => {
    if (!baseDefinition || !graph) return null;
    return graphToProgramDefinition(graph, baseDefinition);
  }, [baseDefinition, graph]);

  const selectedNode = useMemo(() => {
    if (!currentDefinition || !selectedNodeId || !graph) return undefined;
    const graphNode = graph.nodes.find((node) => node.id === selectedNodeId);
    const node = currentDefinition.nodes.find(
      (entry) => entry.id === (graphNode?.data.nodeId || selectedNodeId)
    );
    if (!node || !graphNode) return undefined;
    return { ...node, startable: graphNode.data.startable } as EditableProgramNode;
  }, [currentDefinition, graph, selectedNodeId]);

  const selectedEdge = useMemo(() => {
    if (!graph || !selectedEdgeId) return undefined;
    const edge = graph.edges.find((entry) => entry.id === selectedEdgeId);
    if (!edge) return undefined;
    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      mode: edge.data?.mode || "manual",
      confirm_required: Boolean(edge.data?.confirm_required),
      signals: [...(edge.data?.signals || [])],
      artifact_patch: { ...(edge.data?.artifact_patch || {}) },
    } as ProgramEdge & { id: string };
  }, [graph, selectedEdgeId]);

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  const guardedNavigate = useCallback(
    (next: ProgramRoute) => {
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
    apiFetch<ProgramDefinitionCatalog>("/admin/program-definitions/catalog")
      .then(setCatalog)
      .catch(() => setCatalog(emptyCatalog));
  }, []);

  useEffect(() => {
    let cancelled = false;
    if (
      route.kind === "draft" &&
      preserveLocalDraftIdRef.current &&
      preserveLocalDraftIdRef.current === route.draftId
    ) {
      preserveLocalDraftIdRef.current = null;
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
      setDirty(false);
      setSelectedEdgeId(undefined);
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
          setGraph(definitionToProgramGraph(definition));
          setDraft(null);
          setStatus("draft");
          setReadOnly(false);
          setSelectedNodeId(definition.nodes[0]?.id);
          setDirty(true);
          if (pendingNotice) setNotice(pendingNotice);
          return;
        }
        if (route.kind === "draft") {
          const data = await apiFetch<{ draft: ProgramDraft }>(
            `/admin/program-definitions/drafts/${encodeURIComponent(route.draftId)}`
          );
          if (cancelled || generation !== routeGenerationRef.current) return;
          setDraft(data.draft);
          setBaseDefinition(data.draft.definition);
          setGraph(definitionToProgramGraph(data.draft.definition, data.draft.editor_metadata?.layout));
          setStatus("draft");
          setReadOnly(false);
          setSelectedNodeId(data.draft.definition.nodes[0]?.id);
          setDirty(false);
          if (pendingNotice) setNotice(pendingNotice);
          return;
        }
        if (route.kind === "program") {
          const data = await apiFetch<{
            status: string;
            read_only: boolean;
            definition: ProgramDefinition;
          }>(`/admin/program-definitions/${encodeURIComponent(route.programType)}`);
          if (cancelled || generation !== routeGenerationRef.current) return;
          setDraft(null);
          setBaseDefinition(data.definition);
          setGraph(definitionToProgramGraph(data.definition));
          setStatus(data.status);
          setReadOnly(Boolean(data.read_only || data.status === "built_in" || data.status === "active"));
          setSelectedNodeId(data.definition.nodes[0]?.id);
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

  function updateGraph(next: ProgramGraph) {
    editGenerationRef.current += 1;
    setGraph(next);
    setDirty(true);
    setValidationValid(undefined);
  }

  function updateProgram(next: ProgramDefinition) {
    if (!graph) return;
    editGenerationRef.current += 1;
    const staged = graphToProgramDefinition(graph, next);
    setBaseDefinition(staged);
    setDirty(true);
    setValidationValid(undefined);
  }

  function updateNode(node: EditableProgramNode) {
    if (!graph || !selectedNodeId) return;
    try {
      updateGraph(updateNodeInGraph(graph, selectedNodeId, node));
      setSelectedNodeId(node.id);
      setError(null);
    } catch (e: any) {
      setError(String(e.message || e));
    }
  }

  function updateEdge(edge: ProgramEdge & { id: string }) {
    if (!graph || !selectedEdgeId) return;
    const next = updateEdgeInGraph(graph, selectedEdgeId, edge);
    const updated = next.edges.find(
      (entry) => entry.source === edge.source && entry.target === edge.target && entry.data?.mode === edge.mode
    );
    updateGraph(next);
    setSelectedEdgeId(updated?.id || edge.id);
  }

  async function saveDraft(): Promise<ProgramDraft | null> {
    if (!currentDefinition) return null;
    const routeGeneration = routeGenerationRef.current;
    const editGeneration = editGenerationRef.current;
    const savedRoute = routeRef.current;
    const savedDraftId = draft?.id;
    setBusy("save");
    setError(null);
    setConflict(null);
    try {
      const issues = detectClientProgramGraphIssues(graph!);
      if (issues.some((issue) => issue.severity === "error")) {
        setValidationIssues(issues);
        setValidationValid(false);
        return null;
      }
      if (draft) {
        const editor_metadata = editorMetadataFromGraph(graph!, draft.editor_metadata);
        const data = await apiFetch<{ draft: ProgramDraft }>(
          `/admin/program-definitions/drafts/${encodeURIComponent(draft.id)}`,
          {
            method: "PUT",
            body: JSON.stringify({
              definition: currentDefinition,
              expected_revision: draft.revision,
              editor_metadata,
            }),
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
          return data.draft;
        }
        setBaseDefinition(data.draft.definition);
        setGraph(
          definitionToProgramGraph(
            data.draft.definition,
            data.draft.editor_metadata?.layout || editor_metadata.layout
          )
        );
        setDirty(false);
        return data.draft;
      }
      const editor_metadata = editorMetadataFromGraph(graph!);
      const data = await apiFetch<{ draft: ProgramDraft }>("/admin/program-definitions/drafts", {
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
        preserveLocalDraftIdRef.current = data.draft.id;
      } else {
        setBaseDefinition(data.draft.definition);
        setGraph(
          definitionToProgramGraph(
            data.draft.definition,
            data.draft.editor_metadata?.layout || editor_metadata.layout
          )
        );
        setDirty(false);
      }
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

  async function cloneProgram(item: ProgramDefinitionSummary) {
    if (dirty && !window.confirm("Discard unsaved program changes?")) return;
    const routeGeneration = routeGenerationRef.current;
    const wasDirty = dirty;
    setBusy("clone");
    try {
      const data =
        item.status === "draft" && item.id
          ? await apiFetch<{ draft: ProgramDraft }>(
              `/admin/program-definitions/drafts/${encodeURIComponent(item.id)}`
            )
          : await apiFetch<{ definition: ProgramDefinition }>(
              `/admin/program-definitions/${encodeURIComponent(item.program_type)}`
            );
      const sourceDefinition = "draft" in data ? data.draft.definition : data.definition;
      const definition = {
        ...sourceDefinition,
        program_type: `${sourceDefinition.program_type}_custom`,
        definition_version: "0.1.0",
        display_name: `${sourceDefinition.display_name || sourceDefinition.program_type} copy`,
      };
      const created = await apiFetch<{ draft: ProgramDraft }>("/admin/program-definitions/drafts", {
        method: "POST",
        body: JSON.stringify({
          definition,
          ...("draft" in data && data.draft.editor_metadata
            ? { editor_metadata: data.draft.editor_metadata }
            : {}),
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
        normalized_definition?: ProgramDefinition | null;
      }>(`/admin/program-definitions/drafts/${encodeURIComponent(saved.id)}/validate`, {
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
        active?: { program_type?: string };
      }>(`/admin/program-definitions/drafts/${encodeURIComponent(saved.id)}/activate`, {
        method: "POST",
        body: JSON.stringify({ expected_revision: saved.revision }),
      });
      if (routeGeneration !== routeGenerationRef.current) return;
      setDirty(false);
      setConfirmActivate(false);
      if (result.reload_ok === false || result.warning) {
        pendingNoticeRef.current =
          result.warning || "Program definition activated but runtime reload failed.";
      } else {
        pendingNoticeRef.current = null;
      }
      allowDirtyNavigation?.();
      onNavigate({ kind: "program", programType: saved.definition.program_type });
    } catch (e: any) {
      if (routeGeneration !== routeGenerationRef.current) return;
      if (e instanceof ApiError && e.status === 409) setConflict(String(e.message || e));
      else setError(String(e.message || e));
    } finally {
      setBusy(null);
    }
  }

  const list = (
    <ProgramDefinitionLibrary
      selected={route}
      onNew={() => guardedNavigate({ kind: "new" })}
      onBuildWithAgent={() => guardedNavigate({ kind: "build" })}
      onSelect={guardedNavigate}
      onClone={(item) => void cloneProgram(item)}
    />
  );

  let detail;
  if (route.kind === "library") {
    detail = (
      <div className="empty">
        Select a program definition, clone a built-in map, or create a draft.
        {notice && (
          <p className="muted" role="status">
            {notice}
          </p>
        )}
      </div>
    );
  } else if (loading || !currentDefinition || !graph) {
    detail = error ? (
      <PageState kind="error" title="Could not load program definition" detail={error} />
    ) : (
      <PageState kind="loading" title="Loading program definition" />
    );
  } else {
    detail = (
      <div className="workflow-builder">
        <header className="workflow-builder-header">
          <div>
            <h2>{currentDefinition.display_name || currentDefinition.program_type}</h2>
            <p className="muted">
              {status || "draft"} · {currentDefinition.program_type} ·{" "}
              {currentDefinition.definition_version}
              {readOnly ? " · read-only map" : ""}
            </p>
          </div>
          {!readOnly && (
            <div className="program-actions">
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
            </div>
          )}
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
        <ProgramValidationPanel
          valid={validationValid}
          issues={validationIssues}
          canFocusPath={(path) => Boolean(nodeFromPath(currentDefinition, path))}
          onFocusPath={(path) => {
            const nodeId = nodeFromPath(currentDefinition, path);
            if (nodeId) {
              setSelectedNodeId(nodeId);
              setSelectedEdgeId(undefined);
            }
          }}
        />
        <div className="workflow-builder-grid">
          <ProgramCanvas
            graph={graph}
            selectedNodeId={selectedNodeId}
            selectedEdgeId={selectedEdgeId}
            readOnly={readOnly}
            onGraphChange={updateGraph}
            onSelectNode={setSelectedNodeId}
            onSelectEdge={setSelectedEdgeId}
          />
          <ProgramInspector
            definition={currentDefinition}
            selectedNode={selectedNode}
            selectedEdge={selectedEdge}
            catalog={catalog}
            readOnly={readOnly}
            onProgramChange={updateProgram}
            onNodeChange={updateNode}
            onEdgeChange={updateEdge}
            onOpenWorkflow={onOpenWorkflow}
          />
        </div>
        <ConfirmDialog
          open={confirmActivate}
          title="Activate draft?"
          description="This makes the draft available for new program starts. Existing runs keep their pinned topology."
          confirmLabel="Activate draft"
          busy={busy === "activate"}
          onCancel={() => setConfirmActivate(false)}
          onConfirm={() => void activateDraft()}
        />
      </div>
    );
  }

  return (
    <SplitView
      list={list}
      detail={detail}
      detailOpen={route.kind !== "library"}
      onCloseDetail={() => guardedNavigate({ kind: "library" })}
    />
  );
}
