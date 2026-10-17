export interface ValidationIssue {
  severity: "error" | "warning" | "info" | string;
  code: string;
  path: string;
  message: string;
}

export interface WorkflowVariant {
  workflow_type: string;
  when: Record<string, string | number | boolean | null>;
}

export interface InputBinding {
  kind: "program_summary" | "artifact" | "static" | "transcript_context" | string;
  target: string;
  name?: string;
  value?: unknown;
  limit?: number;
}

export interface ProgramNodeReentry {
  target_node: string;
  next_item_artifact?: string;
  completed_list_artifact?: string;
  remaining_list_artifact?: string;
}

export interface ProgramNode {
  id: string;
  default_workflow_type: string;
  workflow_variants: WorkflowVariant[];
  required_artifacts: string[];
  produces_artifacts: string[];
  input_bindings: InputBinding[];
  completion_adapter: string;
  terminal: boolean;
  reentry?: ProgramNodeReentry;
  complete_channel_text?: string;
}

export interface RoutingIntentDraft {
  id: string;
  signals: string[];
  exclude?: string[];
  program_type?: string;
  entry_phase?: string;
  workflow_type?: string;
  orchestrator_hint?: string;
  policy?: {
    mode?: string;
    deny_tools?: string[];
    deny_start_workflow_types?: string[];
  };
}

export interface ProgramEdge {
  source: string;
  target: string;
  mode: "manual" | "automatic" | string;
  confirm_required?: boolean;
  signals?: string[];
  artifact_patch?: Record<string, string | number | boolean | null>;
}

export interface ProgramRepoRow {
  id: string;
  default_branch?: string;
}

export interface ProgramRepoPicker {
  mode?: "inherit" | "custom";
  repos?: ProgramRepoRow[];
}

export interface ProgramJiraFiling {
  mode?: "inherit" | "custom";
  default_issue_type?: string;
  issue_type_by_project?: Record<string, string>;
}

export interface ProgramTicketPicker {
  mode?: "inherit" | "custom";
  assignee?: string;
  statuses?: string[];
}

export interface ProgramRuntimeConfig {
  repo_picker?: ProgramRepoPicker;
  jira_filing?: ProgramJiraFiling;
  ticket_picker?: ProgramTicketPicker;
}

export interface ProgramDefinition {
  program_type: string;
  definition_version: string;
  display_name?: string;
  startable_nodes: string[];
  nodes: ProgramNode[];
  edges: ProgramEdge[];
  config?: ProgramRuntimeConfig;
  [key: string]: unknown;
}

export interface ProgramEditorMetadata {
  layout?: Record<string, { x: number; y: number }>;
  viewport?: unknown;
  [key: string]: unknown;
}

export interface ProgramDefinitionSummary {
  id?: string;
  program_type: string;
  definition_version?: string;
  display_name?: string;
  status: "draft" | "active" | "built_in" | string;
  read_only?: boolean;
  revision?: string;
  base_revision?: string | null;
  created_at?: string;
  updated_at?: string;
  activated_at?: string;
}

export interface ProgramDraft {
  id: string;
  program_type: string;
  status: "draft" | string;
  revision: string;
  base_revision?: string | null;
  created_at?: string;
  updated_at?: string;
  definition: ProgramDefinition;
  editor_metadata?: ProgramEditorMetadata;
}

export interface CatalogOption {
  id: string;
  label: string;
}

export interface ProgramWorkflowCatalogEntry {
  workflow_type: string;
  status?: string;
  available_versions?: string[];
  active_version?: string;
  resolvable?: boolean;
}

export interface ProgramDefinitionCatalog {
  workflows: ProgramWorkflowCatalogEntry[];
  adapters: CatalogOption[];
  transition_modes: string[];
  templates: ProgramDefinitionSummary[];
}

export type ProgramRoute =
  | { kind: "library" }
  | { kind: "new" }
  | { kind: "draft"; draftId: string }
  | { kind: "program"; programType: string }
  | { kind: "build"; sessionId?: string };

export type StudioTab = "programs" | "workflows" | "fragments";
