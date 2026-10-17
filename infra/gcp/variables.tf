###############################################################################
# Install-contract inputs (see infra/contract/variables.tf)
###############################################################################

variable "cloud_provider" {
  description = "Install provider (must be gcp for this root)."
  type        = string
  default     = "gcp"

  validation {
    condition     = var.cloud_provider == "gcp"
    error_message = "infra/gcp requires cloud_provider = \"gcp\"."
  }
}

variable "environment" {
  description = "Logical environment name (e.g. prod, staging)."
  type        = string
  default     = "prod"
}

variable "project_name" {
  description = "Project name used for resource naming and labeling"
  type        = string
  default     = "ts-harness"
}

variable "gcp_project_id" {
  description = "GCP project ID that owns this installation"
  type        = string
}

variable "cloud_region" {
  description = "GCP region for regional resources (Cloud SQL, subnet, KMS)"
  type        = string
  default     = "us-central1"
}

variable "cloud_zone" {
  description = "GCP zone for the GCE VM and Persistent Disk"
  type        = string
  default     = "us-central1-a"
}

variable "secret_prefix" {
  description = "Prefix for Secret Manager secret IDs (logical catalog uses prefix/key; GCP IDs use prefix-key)"
  type        = string
  default     = ""
}

variable "notification_email" {
  description = "Email address for budget and health alert notifications"
  type        = string
  default     = ""
}

variable "monthly_budget_limit" {
  description = "Monthly GCP spend limit in USD. An alert fires at 80% and 100%. Set to 0 to disable. Requires billing_account_id."
  type        = number
  default     = 500
}

variable "billing_account_id" {
  description = "GCP billing account ID (e.g. 012345-ABCDEF-678901) required when monthly_budget_limit > 0. Budgets use the google-beta provider."
  type        = string
  default     = ""
}

variable "managed_db_instance_class" {
  description = "Cloud SQL tier (e.g. db-f1-micro, db-custom-1-3840)"
  type        = string
  default     = "db-f1-micro"
}

variable "managed_db_name" {
  description = "Managed Postgres database name"
  type        = string
  default     = "ts_harness"
}

variable "managed_db_user" {
  description = "Managed Postgres application username"
  type        = string
  default     = "ts_harness"
}

variable "cloud_sql_availability_type" {
  description = "Cloud SQL availability. REGIONAL is the default and roughly doubles database cost. ZONAL is for dev accounts. Changing this restarts the instance in place."
  type        = string
  default     = "REGIONAL"

  validation {
    condition     = contains(["REGIONAL", "ZONAL"], var.cloud_sql_availability_type)
    error_message = "cloud_sql_availability_type must be REGIONAL or ZONAL."
  }
}

variable "managed_db_backup_retention_days" {
  description = "Number of days to retain automated Cloud SQL backups (1-365)"
  type        = number
  default     = 7
}

variable "credentials_encryption_key_id" {
  description = "Optional existing Cloud KMS crypto key resource name. When null, this root creates one."
  type        = string
  default     = null
}

variable "vm_instance_type" {
  description = "GCE machine type for the agent runtime server"
  type        = string
  default     = "e2-medium"
}

variable "vm_root_volume_size_gb" {
  description = "Size in GB for the boot Persistent Disk"
  type        = number
  default     = 40
}

variable "vm_data_disk_size_gb" {
  description = "Size in GB for the persistent data disk mounted at /vol/ts-harness-data"
  type        = number
  default     = 60
}

###############################################################################
# AWS-equivalent / GCP-native knobs
###############################################################################

variable "ssh_allowed_cidrs" {
  description = "CIDR blocks allowed to SSH into the instance on port 2222"
  type        = list(string)
}

variable "ssh_public_key" {
  description = "SSH public key material for the admin user (contents of an .pub file)"
  type        = string
}

variable "admin_username" {
  description = "Admin user created on the GCE instance for SSH access"
  type        = string
  default     = "artemis"
}

variable "agent_runtime_version" {
  description = "TS Harness agent-runtime image tag or version"
  type        = string
  default     = "0.1.0"
}

variable "memory_backend" {
  description = "Agent memory backend: 'local' (filesystem) or 'managed' (Cloud SQL PostgreSQL with pgvector)"
  type        = string
  default     = "local"

  validation {
    condition     = contains(["local", "managed"], var.memory_backend)
    error_message = "memory_backend must be either 'local' or 'managed'."
  }
}

variable "enterprise_repo_url" {
  description = "Git URL for this ts-harness repo (cloned on the instance for scripts, workspace, skills, and docker overrides)"
  type        = string
  default     = "https://github.com/TribalScale/ts-harness.git"

  validation {
    condition     = !can(regex("your-org", var.enterprise_repo_url))
    error_message = "enterprise_repo_url still contains the placeholder your-org. Use https://github.com/TribalScale/ts-harness.git or a fork."
  }
}

variable "implement_work_orders_repo_url" {
  description = "Git URL of the application repo that implement_work_orders clones and opens PRs against (OpenCode coding backend)."
  type        = string
  default     = ""
}

variable "implement_work_orders_base_branch" {
  description = "Base branch for implement_work_orders clones and PRs."
  type        = string
  default     = "main"
}

variable "implement_work_orders_opencode_version" {
  description = "Pinned OpenCode release for implement_work_orders (phase 11h OPENCODE_VERSION). Prefer a semver pin over latest."
  type        = string
  default     = "1.18.4"
}

variable "identity_provider" {
  description = "Admin console identity provider. Use oidc for Auth0/OIDC; static keeps the gateway token."
  type        = string
  default     = "static"

  validation {
    condition     = contains(["static", "oidc"], var.identity_provider)
    error_message = "identity_provider must be either \"static\" or \"oidc\"."
  }
}

variable "oidc_issuer" {
  description = "OIDC issuer URL (Auth0 domain with trailing slash). Empty when identity_provider is static."
  type        = string
  default     = ""
}

variable "oidc_audience" {
  description = "OIDC API audience used for access-token validation and SPA token exchange."
  type        = string
  default     = ""
}

variable "oidc_jwks_url" {
  description = "OIDC JWKS URL for JWT signature verification."
  type        = string
  default     = ""
}

variable "oidc_client_id" {
  description = "Public OIDC SPA client ID rendered into runtime YAML. Not a secret."
  type        = string
  default     = ""
}

variable "oidc_authorization_endpoint" {
  description = "OIDC authorization endpoint for the admin console login redirect."
  type        = string
  default     = ""
}

variable "oidc_token_endpoint" {
  description = "OIDC token endpoint for the admin console code exchange."
  type        = string
  default     = ""
}

variable "multi_agent_enabled" {
  description = "When true, the bootstrap runs the multi-agent topology."
  type        = bool
  default     = false
}

variable "workflow_runner_enabled" {
  description = "When true and multi_agent_enabled is true, deploy the optional workflow-runner sidecar."
  type        = bool
  default     = false
}

variable "workflow_runner_poll_interval_seconds" {
  description = "Workflow runner polling interval in seconds."
  type        = number
  default     = 10
}

variable "workflow_runner_batch_size" {
  description = "Maximum number of ready workflow tasks the runner processes per tick."
  type        = number
  default     = 5
}

variable "workflow_runner_lease_seconds" {
  description = "Workflow task lease duration in seconds."
  type        = number
  default     = 1800
}

variable "workflow_runner_stale_policy" {
  description = "How the workflow runner handles stale running tasks."
  type        = string
  default     = "block"

  validation {
    condition     = contains(["block", "fail"], var.workflow_runner_stale_policy)
    error_message = "workflow_runner_stale_policy must be either 'block' or 'fail'."
  }
}

variable "agent_timezone" {
  description = "IANA timezone used for cron job schedules and heartbeat active-hours windows."
  type        = string
  default     = "UTC"
}

variable "slack_standup_channel" {
  description = "Slack channel ID or name for the daily standup summary cron job."
  type        = string
  default     = ""
}

variable "slack_ops_channel" {
  description = "Slack channel ID or name for ops-agent heartbeat and CI/CD health reports."
  type        = string
  default     = ""
}

variable "slack_dev_channel" {
  description = "Slack channel ID or name for dev-agent cron reports."
  type        = string
  default     = ""
}

variable "slack_team_channel" {
  description = "Slack channel ID or name for team-level sprint and ticket reports."
  type        = string
  default     = ""
}

variable "slack_marketing_channel" {
  description = "Slack channel ID or name for marketing-agent cron reports."
  type        = string
  default     = ""
}

variable "slack_sales_channel" {
  description = "Slack channel ID or name for sales-agent cron reports."
  type        = string
  default     = ""
}

variable "approved_slack_channels" {
  description = "List of Slack channel IDs or names that comms-agent is authorised to post to."
  type        = list(string)
  default     = []
}

variable "embeddings_provider" {
  description = "Embedding provider for agent memory search."
  type        = string
  default     = "openai"
  validation {
    condition     = contains(["openai", "voyage", "gemini", "mistral", "bedrock", "local"], var.embeddings_provider)
    error_message = "embeddings_provider must be one of: openai, voyage, gemini, mistral, bedrock, local."
  }
}

variable "embeddings_model" {
  description = "Embedding model ID. Default text-embedding-3-small produces 1536-dimensional vectors."
  type        = string
  default     = "text-embedding-3-small"
}

variable "embeddings_dimensions" {
  description = "Output dimensionality. MUST equal the vector() width in database/migrations/001_init_memory.sql (currently 1536)."
  type        = number
  default     = 1536
}

variable "embeddings_fallback" {
  description = "Fallback embedding adapter when the primary fails. Keep 'none' to prevent silent provider switching."
  type        = string
  default     = "none"
}

variable "vpc_cidr" {
  description = "CIDR for the installation VPC subnet"
  type        = string
  default     = "10.20.0.0/24"
}

variable "cloud_sql_private_ip" {
  description = "When true (default), Cloud SQL uses a private IP via Service Networking. When false, uses a public IP gated by cloud_sql_authorized_networks."
  type        = bool
  default     = true
}

variable "cloud_sql_authorized_networks" {
  description = "Authorized CIDR blocks when cloud_sql_private_ip = false. Ignored when private IP is enabled."
  type = list(object({
    name = string
    cidr = string
  }))
  default = []
}

variable "assign_public_ip" {
  description = "When true, the GCE VM gets an ephemeral external IP for bootstrap/SSH. App ports remain closed by firewall."
  type        = bool
  default     = true
}

locals {
  secret_prefix_effective = var.secret_prefix != "" ? var.secret_prefix : var.project_name
  create_sql              = var.memory_backend == "managed" ? 1 : 0
  create_budget           = var.monthly_budget_limit > 0 && var.billing_account_id != "" ? 1 : 0
  create_notify           = var.notification_email != "" ? 1 : 0
  sql_db_name_rendered    = var.memory_backend == "managed" ? var.managed_db_name : "ts_harness_memory"
  sql_db_user_rendered    = var.memory_backend == "managed" ? var.managed_db_user : ""
  sql_db_port_rendered    = "5432"
  data_disk_device_name   = "ts-harness-data"
  data_disk_device_hint   = "/dev/disk/by-id/google-${local.data_disk_device_name}"
  monitoring_namespace    = "TSHarness"
}
