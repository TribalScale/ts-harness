export type InboxType = "skill_draft" | "tool_approval" | "workflow_gate" | "stuck_step";

export type StuckDecision = "revise" | "accept" | "stop";

export interface WorkflowStep {
  task_id: string;
  step_name: string;
  label: string;
  agent_id: string;
  status: string;
  approval_pending: boolean;
  error: string;
  updated_at: string;
}

export interface WorkflowProgress {
  workflow_id: string;
  workflow_type: string;
  label: string;
  workflow_status: string;
  pr_url: string;
  current_step: string;
  state: string;
  kind: "running" | "waiting" | "stuck" | "blocked" | "done" | "failed";
  revisions: number;
  stuck_task_id: string;
  updated_at: string;
  steps: WorkflowStep[];
}

export interface PhaseActivity {
  phase: string;
  state: string;
  workflow_id: string;
  workflow: WorkflowProgress | null;
  children: WorkflowProgress[];
}

export interface InboxItem {
  type: InboxType;
  id: string;
  title: string;
  subtitle?: string;
  created_at?: string;
  href?: string;
  meta?: Record<string, unknown>;
}

export type SkillOrigin = "builtin" | "managed";

export interface SkillDraftFile {
  path: string;
  size: number;
  scan_findings?: string[];
}

export interface SkillRevisionDiff {
  path: string;
  status: "added" | "removed" | "changed";
  diff?: string;
}

export interface SkillDraftProvenance {
  created_by?: string;
  created_by_agent?: string;
  source?: string;
  created_at?: string;
  revision_of?: string;
  [key: string]: unknown;
}

export interface SkillDraftDetail {
  skill_id?: string;
  layout?: "file" | "folder";
  files?: SkillDraftFile[];
  provenance?: SkillDraftProvenance;
  revision_diff?: string;
  revision_diffs?: SkillRevisionDiff[];
  [key: string]: unknown;
}

export interface SkillCreateResult {
  status: string;
  skill_id: string;
  layout: "file" | "folder";
  files?: string[];
  inbox_href: string;
}

export interface UploadFile {
  path: string;
  content: string;
}

export interface AuthConfig {
  mode: "oidc" | "static" | "disabled";
  issuer?: string;
  client_id?: string;
  authorization_endpoint?: string;
  token_endpoint?: string;
  audience?: string | null;
  scopes?: string[];
  allow_static_fallback?: boolean;
  error?: string;
  permissions?: string[];
  roles?: string[];
  authenticated?: boolean;
}

export interface ProgramSummary {
  id: string;
  summary?: string;
  display_name?: string;
  current_phase?: string;
  status?: string;
  updated_at?: string;
  artifacts?: Record<string, unknown>;
  program_type?: string;
  program_definition_version?: string;
  program_definition_revision?: string;
  project_id?: string;
  project_id_source?: string;
  excluded_from_autoshare?: boolean;
  owner?: string;
  child_count?: number;
  waiting_on_kind?: string;
  waiting_on_event?: string;
  wait_age_seconds?: number;
  escalated?: boolean;
  recommended_action?: string;
  compensation_status?: string;
}

export interface PortfolioFilters {
  status: string;
  phase: string;
  owner: string;
  waitingHours: string;
}

export interface PhaseState {
  state: string;
  workflow_id?: string;
  workflow_type?: string;
  workflow_definition_version?: string;
  active_step?: string;
  pending_approvals?: unknown[];
}

export interface ProgramTopologyNode {
  id: string;
  default_workflow_type?: string;
  terminal?: boolean;
  [key: string]: unknown;
}

export interface ProgramTopologyEdge {
  source: string;
  target: string;
  mode?: string;
  [key: string]: unknown;
}

export interface ProgramTopology {
  program_type?: string;
  definition_version?: string;
  display_name?: string;
  nodes?: ProgramTopologyNode[];
  edges?: ProgramTopologyEdge[];
  startable_nodes?: string[];
  [key: string]: unknown;
}

export interface PinnedWorkflowVersion {
  workflow_type?: string;
  definition_version?: string;
  [key: string]: unknown;
}

export interface ProgramEvidenceExport {
  filename: string;
  sha256: string;
}

export interface ProgramStatus {
  program_id?: string;
  summary?: string;
  current_phase?: string;
  entry_phase?: string;
  status?: string;
  program_type?: string;
  program_definition_version?: string;
  program_definition_revision?: string;
  /** Visible/executed branch order from the pinned snapshot (or legacy compat topology). */
  phase_order?: string[];
  phases?: Record<string, PhaseState>;
  /** Full pinned snapshot topology when exposed by status; otherwise loaded from program detail. */
  topology?: ProgramTopology;
  program_definition_snapshot?: ProgramTopology;
  resolved_workflow_versions?: Record<string, PinnedWorkflowVersion>;
  active_workflow_id?: string;
  artifacts?: Record<string, unknown>;
  waiting_on?: {
    kind?: string;
    match?: { task_id?: string; step_name?: string };
    since?: string;
    escalated?: boolean;
  } | null;
  recommended_action?: string;
  compensation?: {
    status?: string;
    failures?: Array<{ kind?: string; ref?: string; detail?: string }>;
  } | null;
  project_id?: string;
  project_id_source?: string;
  excluded_from_autoshare?: boolean;
  outbound_link_count?: number;
  inbound_link_count?: number;
  links?: {
    outbound?: Array<{ to_program_id?: string; from_program_id?: string }>;
    inbound?: Array<{ to_program_id?: string; from_program_id?: string }>;
  };
}

export interface ProgramDetail {
  id?: string;
  summary?: string;
  program_type?: string;
  program_definition_version?: string;
  program_definition_revision?: string;
  program_definition_snapshot?: ProgramTopology | string | null;
  resolved_workflow_versions?: Record<string, PinnedWorkflowVersion> | string | null;
  [key: string]: unknown;
}

export interface WorkflowTask {
  id?: string;
  step_name?: string;
  agent_id?: string;
  status?: string;
  approval_required?: boolean;
  approval_status?: string;
  created_at?: string;
  updated_at?: string;
}

export interface CronProgramConfig {
  program_type: string;
  summary: string;
  entry_node?: string;
  artifacts?: Record<string, unknown>;
  input?: Record<string, unknown>;
  notify?: { channel: string; text?: string };
}

export interface CronJob {
  name: string;
  agent_id: string;
  cron?: string;
  interval_seconds?: number;
  prompt?: string;
  skill_id?: string;
  skill_hints?: string[];
  timezone?: string;
  active_start?: string;
  active_end?: string;
  announce?: Record<string, unknown>;
  kind?: "cron" | "system";
  handler?: string;
  program?: CronProgramConfig;
  last_run?: number | null;
  next_run?: string | null;
}

export interface SessionSummary {
  session_id: string;
  agent_id?: string;
  source?: string;
  binding_key?: string;
  started_at?: string;
}

export interface TranscriptHit {
  session_id: string;
  seq: number;
  role: string;
  snippet: string;
  created_at?: string;
  agent_id?: string;
  source?: string;
}

export type HealthStatus = "ok" | "degraded" | "unavailable" | "not_configured";

export interface ComponentHealth {
  status: HealthStatus;
  detail?: string | null;
}

export interface HealthReport {
  status: HealthStatus;
  version?: string;
  components?: Record<string, ComponentHealth>;
  metrics?: Record<string, number>;
}

export interface ChatMessage {
  role?: "user" | "assistant" | "tool" | "system" | string;
  content?: unknown;
  text?: string;
  name?: string;
  created_at?: string;
}

export interface SessionMessageResponse {
  status?: string;
  session_id?: string;
  output?: { summary?: string; [key: string]: unknown } | string | null;
  reply?: string;
  content?: string;
  message?: string;
  error?: string | null;
}

export interface NormalizedComponentHealth extends ComponentHealth {
  name: string;
  label: string;
}

export interface NormalizedHealthReport {
  status: HealthStatus;
  version?: string;
  components: NormalizedComponentHealth[];
}

export interface ArtifactRow {
  key: string;
  label: string;
  value: string;
  href?: string;
  expandable: boolean;
}
