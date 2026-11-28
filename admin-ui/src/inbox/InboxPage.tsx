import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, apiFetch } from "../api/client";
import type {
  HeldTask,
  ImprovementProposal,
  InboxItem,
  InboxType,
  PauseScope,
  SkillDraftDetail,
  StuckDecision,
} from "../api/types";
import { canManageSkills } from "../auth/session";
import {
  SkillDraftFiles,
  SkillDraftProvenance,
  SkillDraftRevisionDiffs,
} from "./SkillDraftFiles";
import { Button } from "../components/Button";
import { PageState } from "../components/PageState";
import { StatusPill, type SemanticStatus } from "../components/StatusPill";
import { SplitView } from "../layout/SplitView";
import { useMediaQuery } from "../layout/useMediaQuery";
import { GateReview } from "../programs/GateReview";
import { ProposalDetail } from "../live/ImprovementsPanel";
import { PauseButton } from "../live/PauseControls";

const FILTERS: { key: "all" | InboxType; label: string }[] = [
  { key: "all", label: "All for me" },
  { key: "skill_draft", label: "Skills" },
  { key: "tool_approval", label: "Approvals" },
  { key: "workflow_gate", label: "Gates" },
  { key: "stuck_step", label: "Stuck" },
  { key: "knowledge_note", label: "Notes" },
  { key: "pending_proposal", label: "Proposals" },
  { key: "improvement_proposal", label: "Improvements" },
  { key: "paused", label: "Paused" },
];

const HELD_KIND_TEXT: Record<string, string> = {
  revision: "Paused (revision waiting)",
  first_run: "Paused (first coding run waiting)",
  refresh: "Paused (branch refresh waiting)",
  retry: "Paused (retry waiting)",
  queue_start: "Paused (new tickets waiting)",
};

/** A paused ticket, ticket queue, program or all coding: who, when, why, what waits, and Resume. */
function PausedDetail({ item, onChanged }: { item: InboxItem; onChanged: () => void }) {
  const meta = (item.meta || {}) as {
    scope?: PauseScope;
    target_id?: string;
    by?: string;
    at?: string;
    reason?: string;
    label?: string;
    held?: HeldTask[];
    can_resume?: boolean;
  };
  const held = meta.held || [];
  return (
    <div className="detail-sections">
      <section className="detail-card">
        <h3>Paused</h3>
        <p>
          {item.title}. Paused by {meta.by || "someone"}
          {meta.at ? ` at ${new Date(meta.at).toLocaleString()}` : ""}.
        </p>
        {meta.reason && <p className="muted">Reason: {meta.reason}</p>}
        <p className="muted">
          Steps that were already running finish. The next coding run or revision waits here, with its feedback kept,
          and starts where it stopped when you resume.
        </p>
      </section>
      <section className="detail-card">
        <h3>Waiting for the resume</h3>
        {held.length ? (
          <ul>
            {held.map((task) => (
              <li key={task.task_id}>
                {HELD_KIND_TEXT[task.hold.kind || ""] || "Paused"} · <span className="mono">{task.step_name}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">Nothing is held yet: the next coding run will wait here.</p>
        )}
      </section>
      {meta.scope === "program" && meta.target_id && (
        <p>
          <a href={`#/programs/${meta.target_id}`}>Open program</a>
        </p>
      )}
      {meta.can_resume && meta.scope ? (
        <PauseButton
          scope={meta.scope}
          targetId={meta.target_id || ""}
          paused
          label={meta.scope === "global" ? "all coding" : meta.label || "it"}
          onChanged={onChanged}
        />
      ) : (
        <p className="muted">Only an admin can resume all coding (System page).</p>
      )}
    </div>
  );
}

const STUCK_DECISION_ORDER: StuckDecision[] = ["stop", "accept", "retry", "revise"];
const STUCK_DECISION_LABEL: Record<StuckDecision, string> = {
  retry: "Retry",
  revise: "Revise again",
  accept: "Accept as is",
  stop: "Stop work order",
};
const STUCK_DECISION_VARIANT = {
  retry: "secondary",
  revise: "primary",
  accept: "secondary",
  stop: "danger",
} as const;

type ArbiterView = {
  label?: string | null;
  category?: string | null;
  reason?: string | null;
  why?: string | null;
  question?: string | null;
  current?: boolean;
  recommendation?: { action: string; guidance?: string } | null;
  actions?: { action?: string; reason?: string; at?: string }[] | null;
};

function stuckDecisions(
  meta: Record<string, unknown> | null | undefined,
): StuckDecision[] {
  const raw = Array.isArray(meta?.decisions)
    ? (meta?.decisions as unknown[])
    : [];
  return STUCK_DECISION_ORDER.filter((decision) => raw.includes(decision));
}

function badgeClass(type: InboxType): string {
  if (type === "skill_draft") return "badge badge-skill";
  if (type === "tool_approval") return "badge badge-tool";
  if (type === "stuck_step") return "badge badge-stuck";
  if (type === "knowledge_note") return "badge badge-skill";
  if (type === "pending_proposal") return "badge badge-tool";
  if (type === "improvement_proposal") return "badge badge-skill";
  if (type === "paused") return "badge badge-stuck";
  return "badge badge-gate";
}

function typeLabel(type: InboxType): string {
  if (type === "skill_draft") return "Skill draft";
  if (type === "tool_approval") return "Tool approval";
  if (type === "stuck_step") return "Stuck step";
  if (type === "knowledge_note") return "Knowledge note";
  if (type === "pending_proposal") return "Chat proposal";
  if (type === "improvement_proposal") return "Improvement";
  if (type === "paused") return "Paused";
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
  meta: Record<string, unknown> | null | undefined,
): { status: SemanticStatus; label: string } | null {
  const raw = meta?.status ?? meta?.state ?? meta?.approval_status;
  if (typeof raw !== "string" || !raw.trim()) return null;
  const status = STATUS_SEMANTICS[raw.trim().toLowerCase()];
  if (!status) return null;
  return { status, label: raw.replace(/_/g, " ") };
}

function KnowledgeNoteReview({
  meta,
}: {
  meta: Record<string, unknown> | null | undefined;
}) {
  const note = (meta?.note || {}) as Record<string, unknown>;
  const target = (meta?.target || null) as Record<string, unknown> | null;
  const evidence = Array.isArray(meta?.evidence) ? meta.evidence : [];
  return (
    <section className="detail-card" aria-label="Knowledge note proposal">
      <h3>
        {String(note.scope || "note")} · {String(note.proposal_action || "add")}{" "}
        · {String(note.kind || "")}
      </h3>
      <p>
        <strong>{String(note.title || "")}</strong>
      </p>
      <p>{String(note.body || "")}</p>
      {note.why ? <p className="muted">{String(note.why)}</p> : null}
      {target && (
        <div>
          <h4>Current note</h4>
          <p>
            {String(target.title || "")}: {String(target.body || "")}
          </p>
        </div>
      )}
      {evidence.length > 0 && (
        <ul>
          {evidence.map((item, index) => {
            const entry = item as Record<string, unknown>;
            return (
              <li key={index}>
                {String(entry.type || "evidence")} {String(entry.ref || "")}{" "}
                {String(entry.excerpt || "")}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function stringifyValue(value: unknown): string {
  if (value === undefined || value === null || value === "")
    return "Not provided";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean")
    return String(value);
  return JSON.stringify(value, null, 2);
}

function detailValue(
  detail: Record<string, unknown> | null,
  keys: string[],
): string {
  if (!detail) return "Not provided";
  const key = keys.find(
    (candidate) =>
      detail[candidate] !== undefined && detail[candidate] !== null,
  );
  return key ? stringifyValue(detail[key]) : "Not provided";
}

function itemRecord(item: InboxItem): Record<string, unknown> {
  return { ...item };
}

function revisionOfValue(detail: Record<string, unknown> | null): string {
  const direct = detail?.revision_of;
  if (direct !== undefined && direct !== null && direct !== "")
    return String(direct);
  const metadata = detail?.metadata;
  if (!metadata || typeof metadata !== "object") return "";
  const provenance = (metadata as Record<string, unknown>).provenance;
  if (!provenance || typeof provenance !== "object") return "";
  const nested = (provenance as Record<string, unknown>).revision_of;
  return nested !== undefined && nested !== null && nested !== ""
    ? String(nested)
    : "";
}

/** What the automatic PRD checks found on the draft this gate approves. */
function PrdChecksCard({ findings }: { findings: string[] }) {
  return (
    <section className="detail-card" aria-label="Draft checks">
      <h3>Draft checks</h3>
      {findings.length === 0 ? (
        <p>The automatic PRD checks found no issues.</p>
      ) : (
        <>
          <p>
            The automatic PRD checks found {findings.length}{" "}
            {findings.length === 1 ? "issue" : "issues"}. They do not block
            approval.
          </p>
          <ul>
            {findings.map((finding) => (
              <li key={finding}>{finding}</li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
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
  const [notice, setNotice] = useState<{
    title: string;
    detail: string;
  } | null>(null);
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);
  const [listLoading, setListLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [guidance, setGuidance] = useState("");
  // What a gate's approval (or rejection) carries: a repo for a repo pick, answers, or feedback.
  const [gateReply, setGateReply] = useState("");
  const skillsOk = canManageSkills();
  const desktop = useMediaQuery("(min-width: 1024px)");
  const autoSelected = useRef(false);
  // Gates decided in this tab: kept out of the list so a lagging refresh cannot offer them again.
  const decided = useRef(new Set<string>());

  async function load() {
    setListError(null);
    setListLoading(true);
    try {
      const data = await apiFetch<{ items: InboxItem[] }>("/admin/inbox");
      setItems((data.items || []).filter((item) => !decided.current.has(item.id)));
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
    [items, skillsOk],
  );

  const counts = useMemo(
    () => ({
      all: visibleItems.length,
      skill_draft: visibleItems.filter((item) => item.type === "skill_draft")
        .length,
      tool_approval: visibleItems.filter(
        (item) => item.type === "tool_approval",
      ).length,
      workflow_gate: visibleItems.filter(
        (item) => item.type === "workflow_gate",
      ).length,
      stuck_step: visibleItems.filter((item) => item.type === "stuck_step")
        .length,
      knowledge_note: visibleItems.filter(
        (item) => item.type === "knowledge_note",
      ).length,
      pending_proposal: visibleItems.filter(
        (item) => item.type === "pending_proposal",
      ).length,
      improvement_proposal: visibleItems.filter(
        (item) => item.type === "improvement_proposal",
      ).length,
      paused: visibleItems.filter((item) => item.type === "paused").length,
    }),
    [visibleItems],
  );

  const visible = useMemo(() => {
    return visibleItems.filter((item) => {
      if (filter === "all") return true;
      return item.type === filter;
    });
  }, [visibleItems, filter]);

  const selectedItem = useMemo(
    () =>
      visibleItems.find((i) => i.type === selectedType && i.id === selectedId),
    [selectedId, selectedType, visibleItems],
  );

  async function loadDetail(item = selectedItem) {
    if (!item) return;
    setDetailError(null);
    setDetailLoading(
      item.type === "skill_draft" || item.type === "tool_approval",
    );
    try {
      if (item.type === "skill_draft") {
        const data = await apiFetch<Record<string, unknown>>(
          `/admin/skills/drafts/${encodeURIComponent(item.id)}`,
        );
        setDetail(data);
      } else if (item.type === "tool_approval") {
        const data = await apiFetch<Record<string, unknown>>(
          `/admin/approvals/${encodeURIComponent(item.id)}`,
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
    setGuidance("");
    setGateReply("");
  }, [selectedType, selectedId]);

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

  async function actNote(status: "active" | "rejected") {
    const note = (detail?.note || selectedItem?.meta?.note || {}) as {
      id?: string;
      version?: number;
    };
    const id = String(note.id || selectedId || "");
    if (!id) return;
    setBusy(true);
    try {
      await apiFetch(`/admin/notes/${encodeURIComponent(id)}/status`, {
        method: "POST",
        body: JSON.stringify({
          status,
          expected_version: note.version,
          reason: status === "rejected" ? "rejected from inbox" : "",
        }),
      });
      await load();
      setDetail(null);
    } catch (e: unknown) {
      if (e instanceof ApiError && e.status === 409) {
        setDetailError("Changed by someone else. Reload the inbox.");
      } else {
        setDetailError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setBusy(false);
    }
  }

  async function actSkill(approve: boolean) {
    if (!selectedId) return;
    setBusy(true);
    try {
      const path = approve ? "approve" : "reject";
      await apiFetch(
        `/admin/skills/drafts/${encodeURIComponent(selectedId)}/${path}`,
        {
          method: "POST",
          body: JSON.stringify({
            reason: approve ? "" : "rejected from inbox",
          }),
        },
      );
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
      const decided = await apiFetch<{
        resume?: { status: string; error?: string };
      }>(`/admin/approvals/${encodeURIComponent(selectedId)}/decide`, {
        method: "POST",
        body: JSON.stringify({ approved }),
      });
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

  async function actProposal() {
    const sessionId = String(selectedItem?.meta?.session_id || "");
    if (!sessionId) return;
    setBusy(true);
    setNotice(null);
    try {
      await apiFetch("/admin/proposals/cancel", {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId }),
      });
      setNotice({ title: "Proposal cancelled", detail: "A yes in that conversation will not start it now." });
      await load();
      setDetail(null);
    } catch (e: any) {
      setDetailError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  async function actImprovement(action: "approve" | "reject") {
    const proposal = selectedItem?.meta?.proposal as ImprovementProposal | undefined;
    if (!proposal) return;
    setBusy(true);
    setNotice(null);
    try {
      const result = await apiFetch<ImprovementProposal>(
        `/admin/improvements/${encodeURIComponent(String(proposal.id))}/decision`,
        {
          method: "POST",
          body: JSON.stringify({ action, reason: action === "reject" ? "rejected from inbox" : "" }),
        },
      );
      if (action === "approve") {
        setNotice({
          title: "Proposal promoted",
          detail: result?.promotion?.pr_url
            ? `Pull request opened (not merged): ${result.promotion.pr_url}`
            : `Apply the change by hand: ${result?.promotion?.reason || "see the proposal"}`,
        });
      }
      await load();
      setDetail(null);
    } catch (e: unknown) {
      if (e instanceof ApiError && e.status === 409) {
        setDetailError("Changed meanwhile. Reload the inbox.");
      } else {
        setDetailError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setBusy(false);
    }
  }

  async function actGate(approve: boolean) {
    const taskId = String(detail?.task_id || selectedId || "");
    if (!taskId || decided.current.has(taskId)) return;
    setBusy(true);
    setNotice(null);
    try {
      const result = await apiFetch<{ status?: string; approval_status?: string }>(
        approve ? "/admin/workflows/approve" : "/admin/workflows/reject",
        {
          method: "POST",
          body: JSON.stringify({ id: taskId, ...(gateReply.trim() ? { comments: gateReply.trim() } : {}) }),
        },
      );
      // Hide it at once: a refresh can still list it for a moment while the next step starts,
      // and a second click must not approve it again.
      decided.current.add(taskId);
      setItems((current) => current.filter((item) => item.id !== taskId));
      if (result?.status === "already_decided") {
        setNotice({ title: "Already decided", detail: `This gate was already ${result.approval_status || "decided"}.` });
      }
      await load();
      setDetail(null);
    } catch (e: any) {
      setDetailError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  async function actStuck(action: StuckDecision, guidanceText?: string) {
    const meta = detail || selectedItem?.meta || {};
    const workflowId = String(meta.workflow_id || "");
    const taskId = String(meta.task_id || selectedId || "");
    if (!workflowId || !taskId) return;
    setBusy(true);
    setNotice(null);
    try {
      await apiFetch("/admin/workflows/decide", {
        method: "POST",
        body: JSON.stringify({
          workflow_id: workflowId,
          task_id: taskId,
          action,
          ...(guidanceText !== undefined
            ? { guidance: guidanceText.trim() }
            : action === "revise" || action === "retry"
              ? { guidance: guidance.trim() }
              : {}),
        }),
      });
      await load();
      setDetail(null);
    } catch (e: any) {
      if (e instanceof ApiError && e.status === 409) {
        setNotice({ title: "Already decided", detail: String(e.message || e) });
        await load();
        setDetail(null);
      } else {
        setDetailError(String(e.message || e));
      }
    } finally {
      setBusy(false);
    }
  }

  function renderArbiter(meta: Record<string, unknown>, decisions: StuckDecision[]) {
    const arbiter = (meta.arbiter || null) as ArbiterView | null;
    if (!arbiter) return null;
    const actions = (arbiter.actions || []).filter((a) => a && a.action);
    const rec = arbiter.recommendation;
    const canApply = !!rec && decisions.includes(rec.action as StuckDecision);
    return (
      <section className="detail-card arbiter-card" aria-label="Arbiter">
        <h3>
          Arbiter{" "}
          {arbiter.current && arbiter.label && <span className="badge badge-arbiter">{arbiter.label}</span>}
        </h3>
        {(arbiter.why || arbiter.reason) && <p>{arbiter.why || arbiter.reason}</p>}
        {arbiter.why && arbiter.reason && arbiter.why !== arbiter.reason && <p className="muted">{arbiter.reason}</p>}
        {arbiter.question && (
          <p>
            <strong>Question:</strong> {arbiter.question}
          </p>
        )}
        {actions.length > 0 && (
          <ul className="arbiter-actions">
            {actions.map((a, index) => (
              <li key={index}>
                <span className="mono">{a.action}</span> {a.reason ? `· ${a.reason}` : ""}
              </li>
            ))}
          </ul>
        )}
        {rec && (
          <div className="arbiter-recommendation">
            <p>
              <strong>Recommended:</strong> <span className="mono">{rec.action}</span>
            </p>
            {rec.guidance && <pre className="mono">{rec.guidance}</pre>}
            {canApply && (
              <button
                type="button"
                className="btn btn-primary"
                disabled={busy}
                onClick={() => void actStuck(rec.action as StuckDecision, rec.guidance || "")}
              >
                Apply recommendation
              </button>
            )}
          </div>
        )}
      </section>
    );
  }

  function renderStuckSections(meta: Record<string, unknown>) {
    const workOrder = String(meta.work_order || "This work order");
    const step = String(meta.step_name || "a step");
    const prUrl = typeof meta.pr_url === "string" ? meta.pr_url : "";
    const programId =
      typeof meta.program_id === "string" ? meta.program_id : "";
    // The error is what stopped the step; the feedback is the last attempt's own summary or QA's
    // findings. Showing the feedback as the reason sent people after the wrong problem.
    const error = String(meta.error || "").trim();
    const feedback = String(meta.feedback || "").trim();
    const reason = error || feedback || "No reason recorded.";
    const decisions = stuckDecisions(meta);
    const codingRun = decisions.includes("revise");
    return (
      <div className="detail-sections">
        <section className="detail-card">
          <h3>Summary</h3>
          {codingRun ? (
            <p>
              {workOrder} stopped at <span className="mono">{step}</span> after
              using all automatic revisions. Revise again gives OpenCode a fresh
              set of revisions, accept continues the delivery as it is, and stop
              ends the work order.
            </p>
          ) : (
            <p>
              <span className="mono">{step}</span> stopped after its automatic
              retries. Retry runs it again, with your guidance if you add some;
              accept continues with its last output as it is; stop ends it.
            </p>
          )}
        </section>
        {prUrl && (
          <section className="detail-card">
            <h3>Pull request</h3>
            <p>
              <a href={prUrl} target="_blank" rel="noreferrer">
                {prUrl}
              </a>
            </p>
          </section>
        )}
        <section className="detail-card">
          <h3>Why it is stuck</h3>
          <p>{reason}</p>
        </section>
        {renderArbiter(meta, decisions)}
        {error && feedback && feedback !== error && (
          <section className="detail-card">
            <h3>Last attempt</h3>
            <p>{feedback}</p>
          </section>
        )}
        {(decisions.includes("revise") || decisions.includes("retry")) && (
          <section className="detail-card">
            <label className="stuck-guidance">
              <h3>
                {codingRun
                  ? "Guidance for OpenCode (optional)"
                  : "Guidance for the next attempt (optional)"}
              </h3>
              <textarea
                rows={4}
                value={guidance}
                onChange={(event) => setGuidance(event.target.value)}
                placeholder="What should the next attempt do differently?"
              />
            </label>
          </section>
        )}
        {programId && (
          <p>
            <a href={`#/programs/${programId}`}>Open program</a>
          </p>
        )}
        <details className="detail-raw">
          <summary>Raw metadata</summary>
          <pre className="mono">{JSON.stringify(meta, null, 2)}</pre>
        </details>
      </div>
    );
  }

  const detailTitle = selectedType ? typeLabel(selectedType) : "Inbox item";
  const detailStatus =
    statusFromMeta(detail) || statusFromMeta(selectedItem?.meta);

  function renderDetailSections(type: InboxType) {
    if (type === "stuck_step")
      return renderStuckSections(detail || selectedItem?.meta || {});
    if (type === "paused" && selectedItem)
      return <PausedDetail item={selectedItem} onChanged={() => void load()} />;
    if (type === "improvement_proposal" && selectedItem?.meta?.proposal)
      return <ProposalDetail proposal={selectedItem.meta.proposal as ImprovementProposal} />;
    const fallbackDetail = selectedItem ? itemRecord(selectedItem) : null;
    const raw = detail || selectedItem?.meta || fallbackDetail || {};
    const timestampDetail = detail || selectedItem?.meta || fallbackDetail;
    const programId =
      typeof detail?.program_id === "string" ||
      typeof detail?.program_id === "number"
        ? String(detail.program_id)
        : "";
    const summary = detailValue(detail, [
      "summary",
      "title",
      "content",
      "description",
    ]);
    const actor = detailValue(detail, [
      "requester",
      "actor",
      "agent_id",
      "owner",
    ]);
    const commandOrStep =
      type === "workflow_gate"
        ? detailValue(detail, ["step_name", "task_id", "workflow_id"])
        : detailValue(detail, ["command", "tool", "name"]);
    const revisionOf = type === "skill_draft" ? revisionOfValue(detail) : "";
    const gateTaskId =
      type === "workflow_gate"
        ? String(
            detail?.task_id || selectedItem?.meta?.task_id || selectedId || "",
          )
        : "";

    return (
      <div className="detail-sections">
        <section className="detail-card">
          <h3>Summary</h3>
          <p>
            {summary === "Not provided"
              ? selectedItem?.subtitle || summary
              : summary}
          </p>
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
          <p>
            {detailValue(timestampDetail, [
              "created_at",
              "updated_at",
              "submitted_at",
            ])}
          </p>
        </section>
        {type === "tool_approval" && detail?.workflow_id ? (
          <section className="detail-card">
            <h3>Workflow</h3>
            <p>
              Resumes workflow {String(detail.workflow_id)} when you decide.
            </p>
          </section>
        ) : null}
        {type === "workflow_gate" && gateTaskId && (
          <div className="detail-card">
            <GateReview taskId={gateTaskId} />
          </div>
        )}
        {type === "workflow_gate" && (
          <GateReplyCard
            reply={(selectedItem?.meta?.reply || detail?.reply) as GateReply | undefined}
            value={gateReply}
            onChange={setGateReply}
          />
        )}
        {type === "workflow_gate" && Array.isArray(detail?.prd_checks) && (
          <PrdChecksCard
            findings={(detail?.prd_checks as unknown[]).map(String)}
          />
        )}
        {type === "workflow_gate" && programId && (
          <p>
            <a href={`#/programs/${programId}`}>Open program</a>
          </p>
        )}
        {type === "skill_draft" && detail && (
          <SkillDraftProvenance detail={detail as SkillDraftDetail} />
        )}
        {type === "skill_draft" && detail && selectedId && (
          <SkillDraftFiles
            skillId={selectedId}
            detail={detail as SkillDraftDetail}
          />
        )}
        {type === "skill_draft" && revisionOf && (
          <SkillDraftRevisionDiffs
            detail={(detail || {}) as SkillDraftDetail}
            revisionOf={revisionOf}
          />
        )}
        {type === "knowledge_note" && (
          <KnowledgeNoteReview
            meta={(detail || selectedItem?.meta) as Record<string, unknown>}
          />
        )}
        {type === "pending_proposal" && (
          <p>
            This offer is waiting for a yes or no in {String(selectedItem?.meta?.source || "chat")}. Cancel it so a later
            yes there cannot start it, or answer it in the conversation. In the chat, <code>/cancel</code> does the same.
          </p>
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
    if (!detail && type !== "workflow_gate" && type !== "stuck_step" && type !== "pending_proposal")
      return null;
    return (
      <footer className="detail-actions" aria-label="Item actions">
        {type === "skill_draft" && (
          <>
            <Button
              variant="danger"
              busy={busy}
              onClick={() => void actSkill(false)}
            >
              Reject
            </Button>
            <Button
              variant="primary"
              busy={busy}
              onClick={() => void actSkill(true)}
            >
              Approve
            </Button>
          </>
        )}
        {type === "tool_approval" && (
          <>
            <Button
              variant="danger"
              busy={busy}
              onClick={() => void actTool(false)}
            >
              Reject
            </Button>
            <Button
              variant="primary"
              busy={busy}
              onClick={() => void actTool(true)}
            >
              Approve
            </Button>
          </>
        )}
        {type === "stuck_step" &&
          stuckDecisions(detail || selectedItem?.meta).map((decision) => (
            <Button
              key={decision}
              variant={STUCK_DECISION_VARIANT[decision]}
              busy={busy}
              onClick={() => void actStuck(decision)}
            >
              {STUCK_DECISION_LABEL[decision]}
            </Button>
          ))}
        {type === "knowledge_note" && (
          <>
            <Button
              variant="danger"
              busy={busy}
              onClick={() => void actNote("rejected")}
            >
              Reject
            </Button>
            <Button
              variant="primary"
              busy={busy}
              onClick={() => void actNote("active")}
            >
              Approve
            </Button>
          </>
        )}
        {type === "improvement_proposal" && (
          <>
            <Button variant="danger" busy={busy} onClick={() => void actImprovement("reject")}>
              {(selectedItem?.meta?.proposal as ImprovementProposal | undefined)?.type === "needs_design" ? "Dismiss" : "Reject"}
            </Button>
            {(selectedItem?.meta?.proposal as ImprovementProposal | undefined)?.type !== "needs_design" && (
              <Button variant="primary" busy={busy} onClick={() => void actImprovement("approve")}>
                Approve
              </Button>
            )}
          </>
        )}
        {type === "pending_proposal" && (
          <Button variant="danger" busy={busy} onClick={() => void actProposal()}>
            Cancel proposal
          </Button>
        )}
        {type === "workflow_gate" && (
          <>
            <Button
              variant="danger"
              busy={busy}
              onClick={() => void actGate(false)}
            >
              Reject
            </Button>
            <Button
              variant="primary"
              busy={busy}
              onClick={() => void actGate(true)}
            >
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
        <Button
          variant="ghost"
          busy={listLoading && items.length > 0}
          onClick={() => void load()}
        >
          Refresh
        </Button>
      </div>
      {notice && (
        <PageState kind="error" title={notice.title} detail={notice.detail} />
      )}
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
                <span className={badgeClass(item.type)}>
                  {typeLabel(item.type)}
                </span>
                {derived && (
                  <StatusPill status={derived.status} label={derived.label} />
                )}
                {item.subtitle && (
                  <span className="row-subtitle">{item.subtitle}</span>
                )}
              </div>
            </button>
          );
        })
      )}
    </div>
  );

  const detailPane =
    !selectedType || !selectedId ? (
      <PageState
        kind="empty"
        title="Select an item"
        detail="Choose work from the queue to review it."
      />
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
            <StatusPill
              status={detailStatus.status}
              label={detailStatus.label}
            />
          ) : (
            selectedItem?.subtitle && (
              <span className="detail-subtitle">{selectedItem.subtitle}</span>
            )
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


type GateReply = { kind?: string; options?: { id: string; default_branch?: string }[] };

/** The answer a gate's decision carries: Approve sends it as the reply, Reject as the feedback. */
export function GateReplyCard({
  reply,
  value,
  onChange,
}: {
  reply?: GateReply;
  value: string;
  onChange: (value: string) => void;
}) {
  const kind = reply?.kind || "";
  const options = reply?.options || [];
  if (kind === "repo") {
    return (
      <section className="detail-card">
        <label className="stuck-guidance">
          <h3>Repository</h3>
          <p className="muted">Pick the repository this is for, then Approve.</p>
          {options.length > 0 && (
            <select aria-label="Repository" value={value} onChange={(event) => onChange(event.target.value)}>
              <option value="">Choose a repository</option>
              {options.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.id}
                  {option.default_branch ? ` (${option.default_branch})` : ""}
                </option>
              ))}
            </select>
          )}
          {options.length === 0 && (
            <input
              aria-label="Repository"
              value={value}
              placeholder="owner/repo"
              onChange={(event) => onChange(event.target.value)}
            />
          )}
        </label>
      </section>
    );
  }
  return (
    <section className="detail-card">
      <label className="stuck-guidance">
        <h3>{kind === "answer" ? "Your answers" : "Reply (optional)"}</h3>
        <p className="muted">
          {kind === "answer"
            ? "Answer the questions above, then Approve to start the draft."
            : "Sent with Approve as your reply, or with Reject as the feedback for the revision."}
        </p>
        <textarea
          aria-label={kind === "answer" ? "Your answers" : "Reply"}
          rows={4}
          value={value}
          onChange={(event) => onChange(event.target.value)}
        />
      </label>
    </section>
  );
}
