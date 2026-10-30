export interface ValidationIssue {
  severity: "error" | "warning" | "info" | string;
  code: string;
  path: string;
  message: string;
}

export interface WorkflowStage {
  name: string;
  kind?: string;
  agent_id?: string;
  allowed_agents?: string[];
  allowed_contracts?: string[];
  allowed_external_effects?: string[];
  depends_on?: string[];
  approval_required?: boolean;
  plan_approval_required?: boolean;
  contract_ref?: string;
  required_artifacts?: string[];
  skill_hints?: string[];
  /** Absent inherits the agent's tools; present is the stage's whole tool set. */
  tool_allow?: string[];
  present_output?: boolean;
  present_prompt?: string;
  /** Output field shown as the gate message; overrides the contract's `presentation.body`. */
  present_body?: string;
  input_map?: Record<string, string>;
  retry?: {
    max_retries?: number;
    timeout_seconds?: number;
    /** Model turns per task attempt; absent uses the agent default. */
    max_turns?: number;
    [key: string]: unknown;
  };
  on_complete?: WorkflowHook;
  on_reject?: WorkflowHook;
  on_block?: WorkflowHook;
  on_failure?: WorkflowHook;
  wait_external?: Record<string, unknown>;
  barrier?: Record<string, unknown>;
  join?: Record<string, unknown>;
  /** Fragment include fields (`kind: "fragment"`). */
  fragment?: string;
  version?: string;
  params?: Record<string, string>;
  inputs?: Record<string, string>;
  prefix?: string;
  [key: string]: unknown;
}

export interface FragmentParam {
  kind?: "stage_ref" | string;
  required?: boolean;
  description?: string;
  [key: string]: unknown;
}

export interface FragmentInput {
  required?: boolean;
  description?: string;
  [key: string]: unknown;
}

export interface FragmentDefinition {
  fragment_id: string;
  definition_version: string;
  display_name?: string;
  description?: string;
  params?: Record<string, FragmentParam>;
  inputs?: Record<string, FragmentInput>;
  contracts?: Record<string, WorkflowContract>;
  stages: WorkflowStage[];
  [key: string]: unknown;
}

export interface FragmentSummary {
  id?: string;
  fragment_id: string;
  definition_version?: string;
  display_name?: string;
  status: "draft" | "active" | "built_in" | string;
  read_only?: boolean;
  revision?: string;
  params?: Record<string, FragmentParam>;
  inputs?: Record<string, FragmentInput>;
  stage_names?: string[];
}

export interface FragmentDraft {
  id: string;
  fragment_id: string;
  status: "draft" | string;
  revision: string;
  definition: FragmentDefinition;
  editor_metadata?: WorkflowEditorMetadata;
}

export type FragmentRoute =
  | { kind: "library" }
  | { kind: "new" }
  | { kind: "draft"; draftId: string }
  | { kind: "fragment"; fragmentId: string };

export interface ChildWorkflowOption {
  workflow_type: string;
  active_version?: string | null;
  resolvable?: boolean;
  workflow_input_keys?: string[];
}

export interface WorkflowHook {
  hook?: string;
  downstream?: string[];
  compensate_stage?: string;
  max_revisions?: number;
  revise?: Record<string, unknown>;
  re_gate?: Record<string, unknown>;
  resume_after?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface WorkflowContract {
  output_schema?: Record<string, unknown>;
  presentation?: { body?: string; title?: string };
  required_artifacts?: Record<string, string>;
  semantic_validators?: string[];
  outcome?: {
    pointer?: string;
    succeeded_values?: unknown[];
    blocked_values?: unknown[];
  };
  [key: string]: unknown;
}

export interface WorkflowDefinition {
  workflow_type: string;
  definition_version: string;
  display_name?: string;
  notifications?: {
    bound_channel?: boolean;
    events?: string[];
    [key: string]: unknown;
  };
  promotion?: {
    intake_stage?: string;
    confirm_required?: boolean;
    binding?: { format?: string; [key: string]: unknown };
    [key: string]: unknown;
  };
  session?: {
    orchestrator_agent_id?: string;
    phase_tracking?: Record<string, unknown>;
    artifact_steps?: string[];
    artifact_step_prompts?: Record<string, string>;
    on_gate_complete_hook?: string;
    [key: string]: unknown;
  };
  program?: {
    phase?: string;
    next_phase?: string | null;
    required_artifacts?: string[];
    auto_advance?: boolean;
    terminal?: boolean;
    [key: string]: unknown;
  };
  contracts?: Record<string, WorkflowContract>;
  on_workflow_complete?: WorkflowHook;
  on_workflow_cancel?: WorkflowHook;
  stages: WorkflowStage[];
  [key: string]: unknown;
}

export interface WorkflowEditorMetadata {
  layout?: Record<string, { x: number; y: number }>;
  viewport?: unknown;
  [key: string]: unknown;
}

export interface WorkflowDefinitionSummary {
  id?: string;
  workflow_type: string;
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

export interface WorkflowDraft {
  id: string;
  workflow_type: string;
  status: "draft" | string;
  revision: string;
  base_revision?: string | null;
  created_at?: string;
  updated_at?: string;
  definition: WorkflowDefinition;
  editor_metadata?: WorkflowEditorMetadata;
}

export interface CatalogOption {
  id: string;
  label: string;
}

export interface WorkflowDefinitionCatalog {
  hooks: string[];
  agents: CatalogOption[];
  agent_tools?: Record<string, string[]>;
  skills: CatalogOption[];
  stage_kinds: string[];
  semantic_validators?: string[];
  present_prompts?: string[];
  notification_events?: string[];
  templates: WorkflowDefinitionSummary[];
  child_workflows?: ChildWorkflowOption[];
}

export type WorkflowRoute =
  | { kind: "library" }
  | { kind: "new" }
  | { kind: "draft"; draftId: string }
  | { kind: "workflow"; workflowType: string };
