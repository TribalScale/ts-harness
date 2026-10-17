export type SettingsSection =
  | "defaults"
  | "identity"
  | "routing"
  | "policy"
  | "sandbox"
  | "channels"
  | "toolsets"
  | "connector"
  | "advanced";

export interface RepoRow {
  id: string;
  default_branch?: string;
}

export interface RuntimeSettingsDefinition {
  definition_type: string;
  definition_version: string;
  program_defaults?: {
    repo_picker?: { repos?: RepoRow[] };
    jira_filing?: {
      default_issue_type?: string;
      issue_type_by_project?: Record<string, string>;
    };
    ticket_picker?: {
      assignee?: string;
      statuses?: string[];
    };
    github?: { org?: string };
    governance?: { independent_review?: { enabled?: boolean } };
    delivery?: { max_parallel_work_orders?: number };
    compensation?: {
      jira?: string;
      jira_close_transition?: string;
      delete_branches?: boolean;
    };
    merge_readiness?: Record<string, unknown>;
    release?: Record<string, unknown>;
  };
  identity?: {
    slack_user_emails?: Record<string, string>;
    jira_account_ids?: Record<string, string>;
    github_logins?: Record<string, string>;
    roles?: Record<string, string[]>;
    role_bundles?: Record<string, string[]>;
    departments?: Record<string, string>;
    service_permissions?: Record<string, string[]>;
    group_roles?: Record<string, string[]>;
  };
  policy?: Record<string, unknown>;
  sandbox?: Record<string, unknown>;
  bindings?: Record<string, string>;
  toolsets?: Record<string, string[]>;
  connectors?: Record<string, unknown>;
}

export interface RuntimeSettingsDraft {
  id: string;
  revision: string;
  definition: RuntimeSettingsDefinition;
}

export interface RuntimeSettingsGet {
  status: string;
  definition: RuntimeSettingsDefinition;
  draft: RuntimeSettingsDraft | null;
  active_revision?: string | null;
  yaml: Record<string, unknown>;
  merged: RuntimeSettingsDefinition | Record<string, unknown>;
  sources: Record<string, "inherited" | "overlay">;
  redacted_yaml_draft: string;
  redacted_yaml_live: string;
}

export interface RoutingIntent {
  id: string;
  signals: string[];
  exclude?: string[];
  program_type?: string;
  workflow_type?: string;
  delegate_agent?: string;
  entry_phase?: string;
  orchestrator_hint?: string;
  policy?: {
    mode?: string;
    deny_tools?: string[];
    deny_start_workflow_types?: string[];
  };
}

export interface RoutingDraft {
  id: string;
  revision: string;
  definition: {
    definition_type: string;
    definition_version: string;
    intents: RoutingIntent[];
  };
}

export interface RoutingGet {
  status: string;
  definition: {
    definition_type: string;
    definition_version: string;
    intents: RoutingIntent[];
  };
  draft?: RoutingDraft | null;
  active_revision?: string | null;
  yaml_intents: RoutingIntent[];
  merged_intents: RoutingIntent[];
}
