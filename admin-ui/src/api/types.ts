export type InboxType =
  | "skill_draft"
  | "tool_approval"
  | "workflow_gate"
  | "stuck_step"
  | "knowledge_note"
  | "pending_proposal";

export type StuckDecision = "retry" | "revise" | "accept" | "stop";

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

export interface CodingRunProgress {
  phase: string;
  phase_seconds?: number;
  detail?: string;
  done?: Record<string, number>;
}

export interface CodingJobProgress {
  id: string;
  status: "queued" | "starting" | "running";
  last_output: string;
  started_at: string;
  created_at: string;
  model?: string;
  progress?: CodingRunProgress | null;
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
  coding_job?: CodingJobProgress | null;
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
  token_type?: "access" | "id";
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
  visibility?: ProgramVisibility;
}

export type ProgramVisibility = "private" | "public";
export type ProgramGrantLevel = "read" | "write" | "approve" | "admin";
export type ProgramAccessLevel = "none" | ProgramGrantLevel;

export interface ProgramGrant {
  principal_type: "user" | "team";
  principal_id: string;
  level: ProgramGrantLevel;
  email?: string | null;
  display_name?: string | null;
  granted_by?: string | null;
}

export interface ProgramAccessList {
  program_id: string;
  access_root_id?: string;
  inherited?: boolean;
  visibility?: ProgramVisibility;
  owner?: {
    subject: string;
    email?: string | null;
    display_name?: string | null;
  } | null;
  grants: ProgramGrant[];
  access_level?: ProgramAccessLevel;
}

export interface DirectorySearch {
  users: Array<{
    subject: string;
    email?: string | null;
    display_name?: string | null;
  }>;
  teams: Array<{ slug: string; display_name?: string; member_count?: number }>;
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
  access_level?: ProgramAccessLevel;
  visibility?: ProgramVisibility;
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
  resolved_workflow_versions?:
    Record<string, PinnedWorkflowVersion> | string | null;
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

export type RegistryType = "npm" | "pypi" | "maven" | "docker";

export interface Project {
  id: string;
  tenant_id: string;
  key: string;
  name: string;
  team_slug: string | null;
  jira_project_key: string | null;
  default_base_branch: string | null;
  /** Gherkin acceptance tests: auto (repos with no tests or already using Gherkin), always, never. */
  bdd_mode?: "auto" | "always" | "never";
  revision_autonomy?: "autonomous" | "standard" | "conservative";
  max_parallel_tickets?: number;
  worker_cpu: number | null;
  worker_memory_mb: number | null;
  can_manage?: boolean;
}

export interface KnowledgeNote {
  id: string;
  scope: "org" | "project";
  project_id: string | null;
  kind: "avoid" | "howto";
  title: string;
  body: string;
  why: string;
  applies_to: string[];
  exception_to: string | null;
  status: "proposed" | "active" | "retired" | "rejected";
  version: number;
  evidence: { type?: string; ref?: string; url?: string; excerpt?: string }[];
  source: string;
  proposal_action: string;
  revision_of: string | null;
  base_version: number | null;
  promoted_from: string | null;
  use_count: number;
  updated_at?: string | null;
}

export interface ProjectRegistry {
  id: string;
  project_id: string;
  type: RegistryType;
  url: string;
  scope: string | null;
  username: string | null;
  has_credential: boolean;
  last_tested_at: string | null;
  last_test_ok: boolean | null;
  last_test_error: string | null;
}

export interface GitHubOverview {
  apps: {
    host: string;
    configured: boolean;
    app_id: string;
    app_slug: string;
    source: "environment" | "stored" | "";
    webhook_secret: boolean;
    install_url: string;
  }[];
  installations: {
    host: string;
    installation_id: number;
    account_login: string;
    account_type: string;
    repository_selection: string;
    suspended: boolean;
    repositories: string[];
    synced_at: string | null;
  }[];
  webhooks: {
    host: string;
    last_received_at: string | null;
    last_day: number;
  }[];
  rate_limits: {
    host: string;
    remaining: number;
    limit: number;
    reset: number;
    observed_at: number;
  }[];
  pat_fallback: boolean;
  can_sync: boolean;
}

export interface ProjectRepository {
  id: string;
  project_id: string;
  github_host: string;
  owner: string;
  repo: string;
  full_name: string;
  repo_url: string;
  installation_id: number | null;
  default_branch: string | null;
  role: string | null;
}

export interface ProjectChecklistItem {
  id: string;
  title: string;
  status: "ok" | "warning" | "failing";
  summary: string;
  details: string[];
  fix: string;
  /** A console route (#/...) where the item is fixed, when there is one. */
  fix_link: string;
}

export interface ProjectChecklist {
  project_id: string;
  items: ProjectChecklistItem[];
  ready: number;
  total: number;
  summary: string;
}

export type BudgetStatus = "ok" | "warning" | "reached";

export interface TeamBudgetOverride {
  id: string;
  team_id: string;
  month: string | null;
  scope: "month" | "permanent";
  amount_usd: number;
  raised_by: string;
  reason: string;
  created_at: string;
}

export interface TeamBudget {
  id: string;
  slug: string;
  display_name: string;
  budget_usd: number;
  spent_usd: number;
  percent: number;
  status: BudgetStatus;
  held_tasks: number;
  overrides: TeamBudgetOverride[];
  can_raise: boolean;
}

export interface BudgetsOverview {
  month: string;
  currency: string;
  default_team_monthly: number;
  warn_at_percent: number;
  manager_max_monthly: number;
  teams: TeamBudget[];
  unassigned_spent_usd?: number;
  is_admin: boolean;
  raise_limit: number | null;
}

export type ProgramBudgetAction = "downgrade" | "stop";

/** A delivery program's own LLM budget: a total, not monthly. budget_usd 0 means none. */
export interface ProgramOwnBudget {
  budget_usd: number;
  source: "program" | "default" | "none";
  spent_usd: number;
  spent_month_usd: number;
  percent: number | null;
  status: BudgetStatus | "none";
  action: ProgramBudgetAction;
  currency: string;
  warn_at_percent: number;
  held_jobs: number;
  set_by: string | null;
  set_at: string | null;
  can_set: boolean;
  set_limit: number | null;
}

export interface ProgramBudget {
  team: Pick<
    TeamBudget,
    "id" | "slug" | "display_name" | "status" | "percent" | "held_tasks"
  > | null;
  program?: ProgramOwnBudget | null;
}

export interface TimelineBar {
  task_id: string;
  step_name: string;
  label: string;
  category: "coding" | "ci" | "review" | "qa" | "merge" | "decision" | "other";
  status: string;
  run: number;
  start: string;
  end: string;
  seconds: number;
  open: boolean;
}

export interface LiveItem {
  workflow_id: string;
  workflow_type: string;
  label: string;
  state: string;
  kind: string;
  current_step: string;
  revisions: number;
  pr_url: string;
  stuck_task_id: string;
  coding_job?: CodingJobProgress | null;
  jira_issue_key: string;
  waiting_on: "you" | "agent" | "ci" | "queue" | "done";
  in_step_since: string;
  in_step_seconds: number;
  started_at: string;
  elapsed_seconds: number;
  stronger_model: boolean;
  timeline: TimelineBar[];
  program_id?: string;
  program_label?: string;
}

export interface LiveResponse {
  generated_at: string;
  items: LiveItem[];
  capacity: { running: number; queued: number; limit: number | null };
}

export interface RunOutcome {
  outcome?: "merged" | "accepted" | "stopped" | "cancelled" | "failed" | "replaced" | "done";
  decided_by?: string;
  finished_at?: string;
}

export interface HistoryItem extends LiveItem, RunOutcome {
  project: string;
  repo: string;
}

export interface HistoryResponse {
  generated_at: string;
  days: number;
  items: HistoryItem[];
  total: number;
  facets: { projects: string[]; repos: string[]; outcomes: string[] };
}

export interface MetricsTicket {
  workflow_id: string;
  program_id: string;
  program_label: string;
  jira_issue_key: string;
  label: string;
  pr_url: string;
  project: string;
  repo: string;
  outcome: string;
  finished_at: string;
  revisions: number;
  revisions_by_trigger: Record<string, number>;
  first_try: boolean;
  time_to_merge_seconds: number | null;
  you_seconds: number;
  agent_seconds: number;
  coding_runs: number;
  reviewed_runs: number;
  review_fixed: number;
  gate_compared: number;
  gate_disagreed: number;
  cost_usd: number;
  strong_cost_usd: number;
  lost_jobs: number;
}

export interface MetricsStats {
  median: number | null;
  mean: number | null;
  max: number | null;
}

export interface MetricsSummary {
  tickets: number;
  finished: number;
  merged: number;
  first_try: number;
  first_try_rate: number | null;
  revisions: MetricsStats & { total: number; by_trigger: Record<string, number> };
  time_to_merge_seconds: MetricsStats;
  you_seconds: number;
  agent_seconds: number;
  you_share: number | null;
  coding_runs: number;
  reviewed_runs: number;
  review_catch_rate: number | null;
  review_fixed: number;
  gate_compared: number;
  gate_disagreed: number;
  gate_disagreement_rate: number | null;
  cost_usd: { median: number | null; total: number };
  strong_cost_share: number | null;
  lost_jobs: number;
}

export interface MetricsResponse {
  generated_at: string;
  days: number;
  include_running: boolean;
  summary: MetricsSummary;
  weekly: { week: string; tickets: number; first_try_rate: number | null; revisions_mean: number }[];
  tickets: MetricsTicket[];
  facets: { projects: string[]; repos: string[] };
}

export interface StoryFinding {
  severity: string;
  category: string;
  file: string;
  line: number;
  message: string;
}

export interface PrePushReview {
  model?: string;
  reviews?: number;
  fixed?: number;
  open_blocking?: StoryFinding[];
  summary?: string;
  status?: string;
}

export interface StoryEvent {
  at: string;
  kind: "coding" | "ci" | "gate" | "wait" | "decision" | "done";
  title: string;
  subtitle?: string;
  status: "ok" | "blocked" | "failed" | "running" | "done";
  seconds?: number | null;
  model?: string;
  queued_seconds?: number;
  cost_usd?: number | null;
  phases?: Record<string, number> | null;
  review?: PrePushReview | null;
  outcome?: string;
  findings?: StoryFinding[];
  other_findings?: number;
}

export interface CurrentRun {
  title: string;
  model: string;
  started_at: string;
  seconds: number | null;
  queued: boolean;
  phase: string;
  phase_seconds?: number | null;
  detail: string;
  done: Record<string, number>;
}

export interface TicketStoryResponse {
  workflow_id: string;
  generated_at: string;
  started_at: string;
  events: StoryEvent[];
  current_run: CurrentRun | null;
  totals: { category: string; label: string; seconds: number; share: number }[];
  cost_usd: number;
}

export interface BoardItem {
  key: string;
  title: string;
  column: string;
  blocked_by: string[];
  item: (LiveItem & RunOutcome) | null;
  attempts?: (LiveItem & RunOutcome)[];
}

export interface BoardResponse {
  generated_at: string;
  program_id: string;
  columns: string[];
  items: BoardItem[];
  totals: { category: string; label: string; seconds: number; share: number }[];
  parallel_limit: number | null;
}
