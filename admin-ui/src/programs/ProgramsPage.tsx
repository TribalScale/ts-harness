import React, { useCallback, useEffect, useState } from "react";
import { apiDownload, apiFetch } from "../api/client";
import { artifactRows } from "../api/normalizers";
import type {
  PinnedWorkflowVersion,
  PortfolioFilters,
  ProgramBudget,
  ProgramEvidenceExport,
  ProgramStatus,
  ProgramSummary,
  ProgramTopology,
} from "../api/types";
import { hasPermission } from "../auth/session";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PageState } from "../components/PageState";
import { StatusPill, type SemanticStatus } from "../components/StatusPill";
import { Tabs, tabButtonId, tabPanelId } from "../components/Tabs";
import { SplitView } from "../layout/SplitView";
import { PhaseStrip } from "./PhaseStrip";
import { PhaseActivityPanel, usePhaseActivity } from "./PhaseActivity";
import { ShareDialog } from "./ShareDialog";

type ProgramTab = "overview" | "artifacts";
type ProgramAction = "advance" | "cancel";

const TABS_ID = "program-detail";
const STATUS_OPTIONS = ["", "running", "blocked", "failed", "succeeded", "cancelled"];
const EMPTY_FILTERS: PortfolioFilters = { status: "", phase: "", owner: "", waitingHours: "" };

export function portfolioUrl(filters: PortfolioFilters): string {
  const params = new URLSearchParams({ portfolio: "1" });
  if (filters.status) params.set("status", filters.status);
  if (filters.phase.trim()) params.set("phase", filters.phase.trim());
  if (filters.owner.trim()) params.set("owner", filters.owner.trim());
  if (Number(filters.waitingHours) > 0) params.set("waiting_hours", filters.waitingHours.trim());
  return `/admin/programs?${params.toString()}`;
}

export function formatAge(seconds?: number | null): string | undefined {
  if (seconds == null || !Number.isFinite(seconds)) return undefined;
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

function humanize(value?: string): string {
  if (!value) return "Unknown";
  return value.replace(/_/g, " ");
}

function parseJsonObject<T extends Record<string, unknown>>(value: unknown): T | undefined {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as T;
  }
  if (typeof value === "string") {
    const text = value.trim();
    if (!text) return undefined;
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed as T;
      }
    } catch {
      return undefined;
    }
  }
  return undefined;
}

function programDefinitionHref(programType?: string): string | undefined {
  const type = String(programType || "").trim();
  if (!type) return undefined;
  return `#/workflows/programs/${encodeURIComponent(type)}`;
}

function workflowDefinitionHref(workflowType?: string): string | undefined {
  const type = String(workflowType || "").trim();
  if (!type) return undefined;
  return `#/workflows/${encodeURIComponent(type)}`;
}

function programStatus(status?: string): SemanticStatus {
  switch (String(status || "").toLowerCase()) {
    case "waiting":
      return "warning";
    case "succeeded":
      return "success";
    case "failed":
      return "danger";
    case "cancelled":
    case "canceled":
      return "muted";
    case "running":
    default:
      return "neutral";
  }
}

function linkLabel(key: string): string {
  if (key.includes("pull_request") || key === "pr_url") return "Pull request";
  if (key.includes("jira")) return "Jira";
  if (key.includes("doc")) return "Document";
  return humanize(key);
}

const ACCESS_RANK: Record<string, number> = { none: 0, read: 1, write: 2, approve: 3, admin: 4 };

function VisibilityPill({ visibility }: { visibility: string }) {
  const isPublic = visibility === "public";
  return (
    <StatusPill
      status={isPublic ? "neutral" : "muted"}
      label={isPublic ? "Public" : "Private"}
    />
  );
}

export function ProgramsPage({
  programId,
  onSelect,
}: {
  programId?: string;
  onSelect: (id: string) => void;
}) {
  const [programs, setPrograms] = useState<ProgramSummary[]>([]);
  const [status, setStatus] = useState<ProgramStatus | null>(null);
  const [budget, setBudget] = useState<ProgramBudget["team"]>(null);
  const [selectedPhase, setSelectedPhase] = useState<string | undefined>();
  const [tab, setTab] = useState<ProgramTab>("overview");
  const [pendingAction, setPendingAction] = useState<ProgramAction | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [listLoading, setListLoading] = useState(true);
  const [projectDraft, setProjectDraft] = useState("");
  const [linkDraft, setLinkDraft] = useState("");
  const [filters, setFilters] = useState<PortfolioFilters>(EMPTY_FILTERS);
  const [evidence, setEvidence] = useState<ProgramEvidenceExport | null>(null);
  const [exporting, setExporting] = useState(false);
  const [sharing, setSharing] = useState(false);
  const canExportEvidence = hasPermission("edit_config");
  const programClosed = ["succeeded", "cancelled", "failed"].includes(String(status?.status || "").toLowerCase());
  const accessRank = ACCESS_RANK[status?.access_level || "admin"] ?? 0;
  const canWrite = accessRank >= ACCESS_RANK.write;
  const canApproveProgram = accessRank >= ACCESS_RANK.approve;
  const canManage = accessRank >= ACCESS_RANK.admin;
  const activityPhase = selectedPhase || status?.current_phase;
  const phaseActivity = usePhaseActivity(
    programId,
    status ? activityPhase : undefined,
    tab === "overview" && !programClosed
  );

  async function loadList() {
    setListLoading(true);
    try {
      const data = await apiFetch<{ programs: ProgramSummary[] }>(portfolioUrl(filters));
      setPrograms(data.programs || []);
      if (!(programId && !status)) {
        setError(null);
      }
    } catch (e: any) {
      setPrograms([]);
      setError(String(e.message || e));
    } finally {
      setListLoading(false);
    }
  }

  async function loadStatus(id: string) {
    return apiFetch<ProgramStatus>(`/admin/programs/${encodeURIComponent(id)}/status`);
  }

  const refreshStatus = useCallback(async (id: string) => {
    setError(null);
    try {
      const next = await loadStatus(id);
      setStatus(next);
      setSelectedPhase(next.current_phase);
      setProjectDraft(next.project_id || "");
    } catch (e: any) {
      setError(String(e.message || e));
    }
  }, []);

  useEffect(() => {
    loadList();
  }, [filters]);

  useEffect(() => {
    if (!programId) {
      setStatus(null);
      setError(null);
      return;
    }
    setTab("overview");
    setEvidence(null);
    void refreshStatus(programId);
    setBudget(null);
    let active = true;
    apiFetch<ProgramBudget>(`/admin/budgets/programs/${encodeURIComponent(programId)}`)
      .then((data) => {
        if (active) setBudget(data.team);
      })
      .catch(() => {
        if (active) setBudget(null);
      });
    return () => {
      active = false;
    };
  }, [programId, refreshStatus]);

  async function mutateProgram(action: ProgramAction) {
    if (!programId) return;
    setBusy(true);
    try {
      await apiFetch(`/admin/programs/${encodeURIComponent(programId)}/${action}`, {
        method: "POST",
        body: JSON.stringify(
          action === "cancel" ? { reason: "cancelled from admin console" } : {}
        ),
      });
      const [nextStatus] = await Promise.all([loadStatus(programId), loadList()]);
      setStatus(nextStatus);
      setSelectedPhase(nextStatus.current_phase);
    } catch (e: any) {
      // Keep the selected program and its last known status so the failure stays
      // visible; clearing selection here used to hide the error banner entirely.
      setError(String(e.message || e));
    } finally {
      setPendingAction(null);
      setBusy(false);
    }
  }

  async function downloadEvidence() {
    if (!programId) return;
    setExporting(true);
    try {
      const { blob, headers } = await apiDownload(
        `/admin/programs/${encodeURIComponent(programId)}/evidence`
      );
      const filename = `program-${programId}-evidence.json`;
      const href = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = href;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(href);
      setEvidence({ filename, sha256: headers.get("X-Evidence-SHA256") || "" });
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setExporting(false);
    }
  }

  const list = (
    <div className="panel-pad">
      <h2>Programs</h2>
      <form
        className="portfolio-filters"
        aria-label="Portfolio filters"
        onSubmit={(event) => {
          event.preventDefault();
          const data = new FormData(event.currentTarget);
          setFilters({
            status: String(data.get("status") || ""),
            phase: String(data.get("phase") || ""),
            owner: String(data.get("owner") || ""),
            waitingHours: String(data.get("waitingHours") || ""),
          });
        }}
      >
        <label>
          Status
          <select name="status" aria-label="Filter by status" defaultValue={filters.status}>
            {STATUS_OPTIONS.map((option) => (
              <option key={option || "all"} value={option}>
                {option ? humanize(option) : "All"}
              </option>
            ))}
          </select>
        </label>
        <label>
          Phase
          <input name="phase" aria-label="Filter by phase" defaultValue={filters.phase} />
        </label>
        <label>
          Owner
          <input name="owner" aria-label="Filter by owner" defaultValue={filters.owner} />
        </label>
        <label>
          Waiting more than (hours)
          <input
            name="waitingHours"
            type="number"
            min="0"
            aria-label="Waiting more than hours"
            defaultValue={filters.waitingHours}
          />
        </label>
        <Button variant="ghost" type="submit">
          Apply filters
        </Button>
        <Button
          variant="ghost"
          type="button"
          busy={listLoading && programs.length > 0}
          onClick={() => void loadList()}
        >
          Refresh
        </Button>
      </form>
      {listLoading && !programs.length && <PageState kind="loading" title="Loading programs" />}
      {!listLoading && error && !programs.length && (
        <PageState
          kind="error"
          title="Unable to load programs"
          detail={error}
          onRetry={() => void loadList()}
        />
      )}
      {programs.map((p) => {
        const title = p.display_name || p.summary || p.id;
        const age = formatAge(p.wait_age_seconds);
        return (
          <button
            key={p.id}
            className={`row-card ${programId === p.id ? "selected" : ""}`}
            onClick={() => onSelect(p.id)}
          >
            <span className="row-title" title={title}>
              {title}
            </span>
            <span className="row-meta">
              <StatusPill status={programStatus(p.status)} label={humanize(p.status)} />
              {p.visibility && <VisibilityPill visibility={p.visibility} />}
              <span>{humanize(p.current_phase)}</span>
              {p.owner && <span>Owner {p.owner}</span>}
              {Boolean(p.child_count) && <span>{p.child_count} child programs</span>}
              {p.waiting_on_kind && (
                <span>
                  Waiting on {humanize(p.waiting_on_kind)}
                  {age ? ` for ${age}` : ""}
                </span>
              )}
              {p.escalated && <StatusPill status="warning" label="Escalated" />}
              {p.project_id && <span>Project {p.project_id}</span>}
              <span className="short-id">{p.id.slice(0, 8)}</span>
            </span>
            {p.recommended_action && p.recommended_action !== "none" && (
              <span className="row-meta">Next: {humanize(p.recommended_action)}</span>
            )}
          </button>
        );
      })}
      {!listLoading && !error && !programs.length && <div className="empty">No delivery programs</div>}
    </div>
  );

  const rows = artifactRows(status?.artifacts || {});
  const compensation = (status?.compensation ||
    status?.artifacts?.compensation) as ProgramStatus["compensation"];
  const links = rows.filter((row) => row.href);
  const confirmation =
    pendingAction === "cancel"
      ? {
          title: "Cancel program?",
          description: "This stops future phases for the selected delivery program.",
          confirmLabel: "Cancel program",
          danger: true,
        }
      : {
          title: "Advance program?",
          description: "This moves the selected delivery program to the next phase.",
          confirmLabel: "Advance program",
          danger: false,
        };

  function tabPanel(children: React.ReactNode) {
    return (
      <div
        role="tabpanel"
        id={tabPanelId(TABS_ID, tab)}
        aria-labelledby={tabButtonId(TABS_ID, tab)}
        tabIndex={0}
      >
        {children}
      </div>
    );
  }

  let detail: React.ReactNode;
  if (!programId) {
    detail = <div className="empty">Select a program</div>;
  } else if (!status && !error) {
    detail = <PageState kind="loading" title="Loading program" />;
  } else if (!status) {
    detail = (
      <PageState
        kind="error"
        title="Could not load program"
        detail={error || "This program is unavailable."}
        onRetry={() => void refreshStatus(programId)}
      />
    );
  } else {
    const topology =
      status.topology ||
      parseJsonObject<ProgramTopology>(status.program_definition_snapshot);
    const resolved = parseJsonObject<Record<string, PinnedWorkflowVersion>>(
      status.resolved_workflow_versions
    );
    const definitionHref = programDefinitionHref(status.program_type || topology?.program_type);
    const pinnedWorkflowLinks = Object.entries(resolved || {})
      .map(([phase, pin]) => {
        const workflowType = pin?.workflow_type || (status.phases || {})[phase]?.workflow_type;
        const href = workflowDefinitionHref(workflowType);
        if (!workflowType || !href) return null;
        const version =
          pin?.definition_version ||
          (status.phases || {})[phase]?.workflow_definition_version;
        return { phase, workflowType, version, href };
      })
      .filter(Boolean) as Array<{
      phase: string;
      workflowType: string;
      version?: string;
      href: string;
    }>;

    detail = (
      <div className="program-detail">
        <section className="program-header" aria-label="Program detail header">
          <div className="program-header-main">
            <h2>{status.summary || status.program_id}</h2>
            <div className="program-header-meta">
              <StatusPill
                status={programStatus(status.status)}
                label={humanize(status.status)}
              />
              {status.visibility && <VisibilityPill visibility={status.visibility} />}
              {(status.program_type ||
                status.program_definition_version ||
                topology?.program_type) && (
                <span className="program-pin" aria-label="Pinned program definition">
                  {definitionHref ? (
                    <a
                      href={definitionHref}
                      title="Open pinned program definition (read-only history when available)"
                    >
                      {status.program_type || topology?.program_type || "program"}
                      {status.program_definition_version || topology?.definition_version
                        ? `@${status.program_definition_version || topology?.definition_version}`
                        : ""}
                    </a>
                  ) : (
                    <span>
                      {status.program_type || topology?.program_type || "program"}
                      {status.program_definition_version || topology?.definition_version
                        ? `@${status.program_definition_version || topology?.definition_version}`
                        : ""}
                    </span>
                  )}
                  {status.program_definition_revision && (
                    <span className="muted mono"> rev {status.program_definition_revision}</span>
                  )}
                </span>
              )}
              {status.waiting_on && (
                <span className="program-waiting">
                  Waiting on {humanize(status.waiting_on.kind)}:{" "}
                  {status.waiting_on.match?.step_name || "-"}
                </span>
              )}
            </div>
          </div>
          <div className="program-actions" role="group" aria-label="Program actions">
            {canWrite && (
              <Button variant="primary" busy={busy} onClick={() => setPendingAction("advance")}>
                Advance
              </Button>
            )}
            {canManage && (
              <Button variant="danger" busy={busy} onClick={() => setPendingAction("cancel")}>
                Cancel
              </Button>
            )}
            {canManage && (
              <Button variant="ghost" onClick={() => setSharing(true)}>
                Share
              </Button>
            )}
            {canExportEvidence && (
              <Button variant="ghost" busy={exporting} onClick={() => void downloadEvidence()}>
                Download evidence
              </Button>
            )}
            {evidence?.sha256 && (
              <span
                className="muted mono"
                title={`${evidence.filename} sha256 ${evidence.sha256}`}
                aria-label="Evidence hash"
              >
                sha256 {evidence.sha256.slice(0, 12)}
              </span>
            )}
          </div>
        </section>
        {error && (
          <div className="error" role="alert">
            {error}
          </div>
        )}
        {budget && budget.status !== "ok" && (
          <div className="callout budget-banner" role="status">
            <strong>
              {budget.status === "reached"
                ? `${budget.display_name || budget.slug} reached its monthly LLM budget`
                : `${budget.display_name || budget.slug} has used ${budget.percent}% of its monthly LLM budget`}
            </strong>
            {budget.status === "reached"
              ? `Agent and coding steps wait until a manager or admin raises the budget${
                  budget.held_tasks ? ` (${budget.held_tasks} waiting)` : ""
                }. `
              : "Work continues; at 100% agent and coding steps wait. "}
            <a href="#/system/budgets">Open budgets</a>
          </div>
        )}
        <Tabs
          idBase={TABS_ID}
          ariaLabel="Program detail"
          active={tab}
          onChange={(id) => setTab(id as ProgramTab)}
          tabs={[
            { id: "overview", label: "Overview" },
            { id: "artifacts", label: "Artifacts", count: rows.length },
          ]}
        />
        {tab === "overview" &&
          tabPanel(
            <section className="program-panel" aria-label="Program overview">
              <PhaseStrip
                phases={status.phases || {}}
                phaseOrder={status.phase_order}
                topology={topology}
                resolvedWorkflowVersions={resolved}
                currentPhase={status.current_phase}
                selectedPhase={selectedPhase}
                onSelect={setSelectedPhase}
              >
                {activityPhase && (
                  <PhaseActivityPanel
                    phase={activityPhase}
                    activity={phaseActivity.activity}
                    error={phaseActivity.error}
                    programClosed={programClosed}
                    canApproveProgram={canApproveProgram}
                    onChanged={() => void phaseActivity.reload()}
                  />
                )}
              </PhaseStrip>
              {pinnedWorkflowLinks.length > 0 && (
                <div className="key-links" aria-label="Pinned workflow definitions">
                  {pinnedWorkflowLinks.map((item) => (
                    <a
                      key={`${item.phase}-${item.workflowType}`}
                      href={item.href}
                      title={`Open ${item.workflowType} definition (read-only history when available)`}
                    >
                      {humanize(item.phase)}: {item.workflowType}
                      {item.version ? `@${item.version}` : ""}
                    </a>
                  ))}
                </div>
              )}
              <div className="callout" aria-label="Project memory">
                <strong>Project memory</strong>
                <div>
                  Project {status.project_id || "unset"}
                  {status.project_id_source ? ` (${status.project_id_source})` : ""}
                  {" · "}
                  Auto-share {status.excluded_from_autoshare ? "excluded" : "on"}
                  {" · "}
                  Links out {status.outbound_link_count || 0} / in {status.inbound_link_count || 0}
                </div>
                {canManage && (
                <div className="program-actions" style={{ marginTop: 8 }}>
                  <label>
                    Project
                    <input
                      aria-label="Program project"
                      value={projectDraft}
                      onChange={(event) => setProjectDraft(event.target.value)}
                    />
                  </label>
                  <Button
                    variant="ghost"
                    busy={busy}
                    onClick={() =>
                      void (async () => {
                        if (!programId) return;
                        setBusy(true);
                        try {
                          await apiFetch(`/admin/programs/${encodeURIComponent(programId)}`, {
                            method: "PATCH",
                            body: JSON.stringify({ project_id: projectDraft }),
                          });
                          await refreshStatus(programId);
                        } catch (e: any) {
                          setError(String(e.message || e));
                        } finally {
                          setBusy(false);
                        }
                      })()
                    }
                  >
                    Save project
                  </Button>
                  <Button
                    variant="ghost"
                    busy={busy}
                    onClick={() =>
                      void (async () => {
                        if (!programId) return;
                        setBusy(true);
                        try {
                          await apiFetch(`/admin/programs/${encodeURIComponent(programId)}`, {
                            method: "PATCH",
                            body: JSON.stringify({
                              excluded_from_autoshare: !status.excluded_from_autoshare,
                            }),
                          });
                          await refreshStatus(programId);
                        } catch (e: any) {
                          setError(String(e.message || e));
                        } finally {
                          setBusy(false);
                        }
                      })()
                    }
                  >
                    {status.excluded_from_autoshare ? "Include in auto-share" : "Exclude from auto-share"}
                  </Button>
                </div>
                )}
                {canManage && (
                <div className="program-actions" style={{ marginTop: 8 }}>
                  <label>
                    Link to program
                    <input
                      aria-label="Link target program"
                      value={linkDraft}
                      onChange={(event) => setLinkDraft(event.target.value)}
                    />
                  </label>
                  <Button
                    variant="ghost"
                    busy={busy}
                    onClick={() =>
                      void (async () => {
                        if (!programId || !linkDraft.trim()) return;
                        setBusy(true);
                        try {
                          await apiFetch(`/admin/programs/${encodeURIComponent(programId)}/links`, {
                            method: "POST",
                            body: JSON.stringify({ to_program_id: linkDraft.trim() }),
                          });
                          setLinkDraft("");
                          await refreshStatus(programId);
                        } catch (e: any) {
                          setError(String(e.message || e));
                        } finally {
                          setBusy(false);
                        }
                      })()
                    }
                  >
                    Add link
                  </Button>
                </div>
                )}
                {(status.links?.outbound || []).map((link) => (
                  <div key={link.to_program_id} className="program-actions">
                    <span>Reads {link.to_program_id}</span>
                    {canManage && (
                    <Button
                      variant="ghost"
                      busy={busy}
                      onClick={() =>
                        void (async () => {
                          if (!programId || !link.to_program_id) return;
                          setBusy(true);
                          try {
                            await apiFetch(
                              `/admin/programs/${encodeURIComponent(programId)}/links/${encodeURIComponent(link.to_program_id)}`,
                              { method: "DELETE" }
                            );
                            await refreshStatus(programId);
                          } catch (e: any) {
                            setError(String(e.message || e));
                          } finally {
                            setBusy(false);
                          }
                        })()
                      }
                    >
                      Unlink
                    </Button>
                    )}
                  </div>
                ))}
              </div>
              {status.recommended_action && (
                <div className="callout">
                  <strong>Recommended action</strong>
                  <div>{status.recommended_action}</div>
                </div>
              )}
              {compensation && (
                <div className="callout" aria-label="Cancel cleanup">
                  <strong>Cancel cleanup: {humanize(compensation.status)}</strong>
                  {(compensation.failures || []).map((item, index) => (
                    <div key={`${item.ref}-${index}`}>
                      {humanize(item.kind)} {item.ref}: {item.detail || "failed"}
                    </div>
                  ))}
                </div>
              )}
              {links.length > 0 && (
                <div className="key-links" aria-label="Key links">
                  {links.map((row) => (
                    <a key={row.key} href={row.href} target="_blank" rel="noreferrer">
                      {linkLabel(row.key)}
                    </a>
                  ))}
                </div>
              )}
            </section>
          )}
        {tab === "artifacts" &&
          tabPanel(
            <dl className="artifact-list">
              {rows.map((row) => (
                <div key={row.key} className="artifact-row">
                  <dt>{row.label}</dt>
                  <dd>
                    {row.href ? (
                      <a href={row.href} target="_blank" rel="noreferrer">
                        {linkLabel(row.key)}
                      </a>
                    ) : row.expandable ? (
                      <details>
                        <summary>View value</summary>
                        <pre>{row.value}</pre>
                      </details>
                    ) : (
                      row.value
                    )}
                  </dd>
                </div>
              ))}
              {!rows.length && <div className="empty">No artifacts</div>}
            </dl>
          )}
        <ConfirmDialog
          open={Boolean(pendingAction)}
          title={confirmation.title}
          description={confirmation.description}
          confirmLabel={confirmation.confirmLabel}
          danger={confirmation.danger}
          busy={busy}
          onConfirm={() => pendingAction && mutateProgram(pendingAction)}
          onCancel={() => setPendingAction(null)}
        />
        {programId && (
          <ShareDialog
            open={sharing}
            programId={programId}
            onClose={() => setSharing(false)}
            onChanged={(access) =>
              setStatus((current) => (current ? { ...current, visibility: access.visibility } : current))
            }
          />
        )}
      </div>
    );
  }

  return (
    <SplitView
      className="programs-split"
      list={list}
      detail={detail}
      detailOpen={Boolean(programId)}
      onCloseDetail={() => onSelect("")}
    />
  );
}
