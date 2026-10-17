import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, apiFetch } from "../api/client";
import type { InboxItem, InboxType, SkillDraftDetail } from "../api/types";
import { canManageSkills } from "../auth/session";
import { SkillDraftFiles, SkillDraftProvenance, SkillDraftRevisionDiffs } from "./SkillDraftFiles";
import { Button } from "../components/Button";
import { PageState } from "../components/PageState";
import { StatusPill, type SemanticStatus } from "../components/StatusPill";
import { SplitView } from "../layout/SplitView";
import { useMediaQuery } from "../layout/useMediaQuery";

const FILTERS: { key: "all" | InboxType; label: string }[] = [
  { key: "all", label: "All for me" },
  { key: "skill_draft", label: "Skills" },
  { key: "tool_approval", label: "Approvals" },
  { key: "workflow_gate", label: "Gates" },
];

function badgeClass(type: InboxType): string {
  if (type === "skill_draft") return "badge badge-skill";
  if (type === "tool_approval") return "badge badge-tool";
  return "badge badge-gate";
}

function typeLabel(type: InboxType): string {
  if (type === "skill_draft") return "Skill draft";
  if (type === "tool_approval") return "Tool approval";
  return "Workflow gate";
}

const STATUS_SEMANTICS: Record<string, SemanticStatus> = {
  approved: "success",
  succeeded: "success",
  complete: "success",
  completed: "success",
  published: "success",
  rejected: "danger",
  failed: "danger",
  error: "danger",
  blocked: "warning",
  waiting: "warning",
  pending: "warning",
  awaiting_approval: "warning",
  cancelled: "muted",
  canceled: "muted",
};

/**
 * Item type carries no health meaning, so a status pill is only rendered when the
 * backend reports an actual lifecycle status we can map to a semantic colour.
 */
function statusFromMeta(
  meta: Record<string, unknown> | null | undefined
): { status: SemanticStatus; label: string } | null {
  const raw = meta?.status ?? meta?.state ?? meta?.approval_status;
  if (typeof raw !== "string" || !raw.trim()) return null;
  const status = STATUS_SEMANTICS[raw.trim().toLowerCase()];
  if (!status) return null;
  return { status, label: raw.replace(/_/g, " ") };
}

function stringifyValue(value: unknown): string {
  if (value === undefined || value === null || value === "") return "Not provided";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value, null, 2);
}

function detailValue(detail: Record<string, unknown> | null, keys: string[]): string {
  if (!detail) return "Not provided";
  const key = keys.find((candidate) => detail[candidate] !== undefined && detail[candidate] !== null);
  return key ? stringifyValue(detail[key]) : "Not provided";
}

function itemRecord(item: InboxItem): Record<string, unknown> {
  return { ...item };
}

function revisionOfValue(detail: Record<string, unknown> | null): string {
  const direct = detail?.revision_of;
  if (direct !== undefined && direct !== null && direct !== "") return String(direct);
  const metadata = detail?.metadata;
  if (!metadata || typeof metadata !== "object") return "";
  const provenance = (metadata as Record<string, unknown>).provenance;
  if (!provenance || typeof provenance !== "object") return "";
  const nested = (provenance as Record<string, unknown>).revision_of;
  return nested !== undefined && nested !== null && nested !== "" ? String(nested) : "";
}

export function InboxPage({
  selectedType,
  selectedId,
  onSelect,
}: {
  selectedType?: InboxType;
  selectedId?: string;
  onSelect: (type: InboxType, id: string) => void;
}) {
  const [items, setItems] = useState<InboxItem[]>([]);
  const [filter, setFilter] = useState<"all" | InboxType>("all");
  const [listError, setListError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ title: string; detail: string } | null>(null);
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);
  const [listLoading, setListLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const skillsOk = canManageSkills();
  const desktop = useMediaQuery("(min-width: 1024px)");
  const autoSelected = useRef(false);

  async function load() {
    setListError(null);
    setListLoading(true);
    try {
      const data = await apiFetch<{ items: InboxItem[] }>("/admin/inbox");
      setItems(data.items || []);
    } catch (e: any) {
      setListError(String(e.message || e));
    } finally {
      setListLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  const visibleItems = useMemo(
    () => items.filter((item) => item.type !== "skill_draft" || skillsOk),
    [items, skillsOk]
  );

  const counts = useMemo(
    () => ({
      all: visibleItems.length,
      skill_draft: visibleItems.filter((item) => item.type === "skill_draft").length,
      tool_approval: visibleItems.filter((item) => item.type === "tool_approval").length,
      workflow_gate: visibleItems.filter((item) => item.type === "workflow_gate").length,
    }),
    [visibleItems]
  );

  const visible = useMemo(() => {
    return visibleItems.filter((item) => {
      if (filter === "all") return true;
      return item.type === filter;
    });
  }, [visibleItems, filter]);

  const selectedItem = useMemo(
    () => visibleItems.find((i) => i.type === selectedType && i.id === selectedId),
    [selectedId, selectedType, visibleItems]
  );

  async function loadDetail(item = selectedItem) {
    if (!item) return;
    setDetailError(null);
    setDetailLoading(item.type !== "workflow_gate");
    try {
      if (item.type === "skill_draft") {
        const data = await apiFetch<Record<string, unknown>>(
          `/admin/skills/drafts/${encodeURIComponent(item.id)}`
        );
        setDetail(data);
      } else if (item.type === "tool_approval") {
        const data = await apiFetch<Record<string, unknown>>(
          `/admin/approvals/${encodeURIComponent(item.id)}`
        );
        setDetail(data);
      } else {
        setDetail(item.meta || itemRecord(item));
      }
    } catch (e: any) {
      setDetail(null);
      setDetailError(String(e.message || e));
    } finally {
      setDetailLoading(false);
    }
  }

  useEffect(() => {
    if (!selectedType || !selectedId) {
      setDetail(null);
      setDetailError(null);
      setDetailLoading(false);
      return;
    }
    if (!selectedItem) {
      setDetail(null);
      setDetailError(null);
      setDetailLoading(false);
      return;
    }
    void loadDetail(selectedItem);
  }, [selectedType, selectedId, selectedItem]);

  // Desktop shows both panes at once, so seeding the first item avoids an empty
  // detail pane. On mobile the detail is a slide-over: auto-selecting there would
  // immediately reopen whatever the operator just closed.
  useEffect(() => {
    if (!desktop || autoSelected.current || selectedId || !visible[0]) return;
    autoSelected.current = true;
    onSelect(visible[0].type, visible[0].id);
  }, [visible, selectedId, onSelect, desktop]);

  async function actSkill(approve: boolean) {
    if (!selectedId) return;
    setBusy(true);
    try {
      const path = approve ? "approve" : "reject";
      await apiFetch(`/admin/skills/drafts/${encodeURIComponent(selectedId)}/${path}`, {
        method: "POST",
        body: JSON.stringify({ reason: approve ? "" : "rejected from inbox" }),
      });
      await load();
      setDetail(null);
    } catch (e: any) {
      setDetailError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  async function actTool(approved: boolean) {
    if (!selectedId) return;
    setBusy(true);
    setNotice(null);
    try {
      const decided = await apiFetch<{ resume?: { status: string; error?: string } }>(
        `/admin/approvals/${encodeURIComponent(selectedId)}/decide`,
        { method: "POST", body: JSON.stringify({ approved }) }
      );
      if (decided?.resume?.status === "failed") {
        setNotice({
          title: "Workflow stage did not resume",
          detail: `${approved ? "Approved" : "Rejected"}, but the workflow stage did not resume: ${
            decided.resume.error || "unknown error"
          }`,
        });
      }
      await load();
      setDetail(null);
    } catch (e: any) {
      if (e instanceof ApiError && e.status === 409) {
        setNotice({
          title: "Already decided",
          detail: `Someone already decided this request (${e.message}).`,
        });
        await load();
        setDetail(null);
      } else {
        setDetailError(String(e.message || e));
      }
    } finally {
      setBusy(false);
    }
  }

  async function actGate(approve: boolean) {
    const taskId = String(detail?.task_id || selectedId || "");
    if (!taskId) return;
    setBusy(true);
    try {
      await apiFetch(approve ? "/admin/workflows/approve" : "/admin/workflows/reject", {
        method: "POST",
        body: JSON.stringify({ id: taskId }),
      });
      await load();
      setDetail(null);
    } catch (e: any) {
      setDetailError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  const detailTitle = selectedType ? typeLabel(selectedType) : "Inbox item";
  const detailStatus = statusFromMeta(detail) || statusFromMeta(selectedItem?.meta);

  function renderDetailSections(type: InboxType) {
    const fallbackDetail = selectedItem ? itemRecord(selectedItem) : null;
    const raw = detail || selectedItem?.meta || fallbackDetail || {};
    const timestampDetail = detail || selectedItem?.meta || fallbackDetail;
    const programId =
      typeof detail?.program_id === "string" || typeof detail?.program_id === "number"
        ? String(detail.program_id)
        : "";
    const summary = detailValue(detail, ["summary", "title", "content", "description"]);
    const actor = detailValue(detail, ["requester", "actor", "agent_id", "owner"]);
    const commandOrStep =
      type === "workflow_gate"
        ? detailValue(detail, ["step_name", "task_id", "workflow_id"])
        : detailValue(detail, ["command", "tool", "name"]);
    const revisionOf = type === "skill_draft" ? revisionOfValue(detail) : "";

    return (
      <div className="detail-sections">
        <section className="detail-card">
          <h3>Summary</h3>
          <p>{summary === "Not provided" ? selectedItem?.subtitle || summary : summary}</p>
        </section>
        <section className="detail-card">
          <h3>Actor / requester</h3>
          <p>{actor}</p>
        </section>
        <section className="detail-card">
          <h3>{type === "workflow_gate" ? "Workflow step" : "Command"}</h3>
          <p className="mono">{commandOrStep}</p>
        </section>
        <section className="detail-card">
          <h3>Timestamps</h3>
          <p>{detailValue(timestampDetail, ["created_at", "updated_at", "submitted_at"])}</p>
        </section>
        {type === "tool_approval" && detail?.workflow_id ? (
          <section className="detail-card">
            <h3>Workflow</h3>
            <p>Resumes workflow {String(detail.workflow_id)} when you decide.</p>
          </section>
        ) : null}
        {type === "workflow_gate" && programId && (
          <p>
            <a href={`#/programs/${programId}`}>Open program</a>
          </p>
        )}
        {type === "skill_draft" && detail && <SkillDraftProvenance detail={detail as SkillDraftDetail} />}
        {type === "skill_draft" && detail && selectedId && (
          <SkillDraftFiles skillId={selectedId} detail={detail as SkillDraftDetail} />
        )}
        {type === "skill_draft" && revisionOf && (
          <SkillDraftRevisionDiffs detail={(detail || {}) as SkillDraftDetail} revisionOf={revisionOf} />
        )}
        <details className="detail-raw">
          <summary>Raw metadata</summary>
          <pre className="mono">{JSON.stringify(raw, null, 2)}</pre>
        </details>
      </div>
    );
  }

  function renderActions(type: InboxType) {
    if (type === "skill_draft" && !skillsOk) return null;
    if (!detail && type !== "workflow_gate") return null;
    return (
      <footer className="detail-actions" aria-label="Item actions">
        {type === "skill_draft" && (
          <>
            <Button variant="danger" busy={busy} onClick={() => void actSkill(false)}>
              Reject
            </Button>
            <Button variant="primary" busy={busy} onClick={() => void actSkill(true)}>
              Approve
            </Button>
          </>
        )}
        {type === "tool_approval" && (
          <>
            <Button variant="danger" busy={busy} onClick={() => void actTool(false)}>
              Reject
            </Button>
            <Button variant="primary" busy={busy} onClick={() => void actTool(true)}>
              Approve
            </Button>
          </>
        )}
        {type === "workflow_gate" && (
          <>
            <Button variant="danger" busy={busy} onClick={() => void actGate(false)}>
              Reject
            </Button>
            <Button variant="primary" busy={busy} onClick={() => void actGate(true)}>
              Approve
            </Button>
          </>
        )}
      </footer>
    );
  }

  const list = (
    <div className="panel-pad">
      <h2>Needs you</h2>
      <div className="filters">
        {FILTERS.filter((f) => f.key !== "skill_draft" || skillsOk).map((f) => (
          <Button
            key={f.key}
            variant="ghost"
            aria-pressed={filter === f.key}
            className={filter === f.key ? "active" : ""}
            onClick={() => setFilter(f.key)}
          >
            {f.label} {counts[f.key]}
          </Button>
        ))}
        <Button variant="ghost" busy={listLoading && items.length > 0} onClick={() => void load()}>
          Refresh
        </Button>
      </div>
      {notice && <PageState kind="error" title={notice.title} detail={notice.detail} />}
      {listError && items.length > 0 && (
        <PageState
          kind="stale"
          title="Inbox may be stale"
          detail={listError}
          onRetry={() => void load()}
        />
      )}
      {listLoading && items.length === 0 ? (
        <PageState kind="loading" title="Loading inbox" />
      ) : listError && items.length === 0 ? (
        <PageState
          kind="error"
          title="Could not load inbox"
          detail={listError}
          onRetry={() => void load()}
        />
      ) : visible.length === 0 ? (
        <PageState kind="empty" title="Nothing waiting on you" />
      ) : (
        visible.map((item) => {
          const derived = statusFromMeta(item.meta);
          return (
            <button
              key={`${item.type}:${item.id}`}
              className={`row-card ${selectedType === item.type && selectedId === item.id ? "selected" : ""}`}
              title={item.title}
              onClick={() => onSelect(item.type, item.id)}
            >
              <span className="row-title">{item.title}</span>
              <div className="row-meta">
                <span className={badgeClass(item.type)}>{typeLabel(item.type)}</span>
                {derived && <StatusPill status={derived.status} label={derived.label} />}
                {item.subtitle && <span className="row-subtitle">{item.subtitle}</span>}
              </div>
            </button>
          );
        })
      )}
    </div>
  );

  const detailPane = !selectedType || !selectedId ? (
    <PageState kind="empty" title="Select an item" detail="Choose work from the queue to review it." />
  ) : !selectedItem ? (
    <PageState
      kind="empty"
      title="Item unavailable"
      detail="This item is not available for your current role."
    />
  ) : (
    <section className="inbox-detail" aria-label={`${detailTitle} detail`}>
      <header className="detail-header">
        <div>
          <h2>{detailTitle}</h2>
          <p className="mono muted">{selectedId}</p>
        </div>
        {detailStatus ? (
          <StatusPill status={detailStatus.status} label={detailStatus.label} />
        ) : (
          selectedItem?.subtitle && <span className="detail-subtitle">{selectedItem.subtitle}</span>
        )}
      </header>
      {detailLoading ? (
        <PageState kind="loading" title="Loading item detail" />
      ) : detailError ? (
        <PageState
          kind="error"
          title="Could not load item detail"
          detail={detailError}
          onRetry={() => void loadDetail()}
        />
      ) : (
        renderDetailSections(selectedType)
      )}
      {renderActions(selectedType)}
    </section>
  );

  return (
    <SplitView
      list={list}
      detail={detailPane}
      detailOpen={Boolean(selectedId)}
      onCloseDetail={() => onSelect(selectedType || "tool_approval", "")}
    />
  );
}
