variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "us-east-1"
}

variable "secret_prefix" {
  description = "Prefix for Secrets Manager names (<prefix>/<key>) and the SSM runtime-config path. Empty means project_name."
  type        = string
  default     = ""
}

locals {
  secret_prefix_effective = var.secret_prefix != "" ? var.secret_prefix : var.project_name
}

variable "aws_account_id" {
  description = "AWS account this install targets. When set, Terraform refuses to run with credentials for any other account."
  type        = string
  default     = null
}

variable "instance_type" {
  description = "EC2 instance type for the TS Harness agent runtime server"
  type        = string
  default     = "t3.medium"
}

variable "ebs_size" {
  description = "Size in GB for the persistent data EBS volume mounted at /vol/ts-harness-data"
  type        = number
  default     = 60
}

variable "root_volume_size" {
  description = "Size in GB for the root EBS volume"
  type        = number
  default     = 40
}

variable "agent_runtime_version" {
  description = "TS Harness agent-runtime image tag or version"
  type        = string
  default     = "0.1.0"
}

variable "memory_backend" {
  description = "Agent memory backend: 'local' (filesystem) or 'rds' (PostgreSQL with pgvector)"
  type        = string
  default     = "local"

  validation {
    condition     = contains(["local", "rds"], var.memory_backend)
    error_message = "memory_backend must be either 'local' or 'rds'."
  }
}

variable "ssh_allowed_cidrs" {
  description = "CIDR blocks allowed to SSH into the instance on port 2222"
  type        = list(string)
}

variable "admin_username" {
  description = "Admin user created on the EC2 instance for SSH access"
  type        = string
  default     = "artemis"
}

variable "project_name" {
  description = "Project name used for resource naming and tagging"
  type        = string
  default     = "ts-harness"
}

variable "ssh_key_name" {
  description = "Name of an existing AWS EC2 key pair for SSH access"
  type        = string
}

variable "tailscale_authkey_secret_arn" {
  description = "ARN of a Secrets Manager secret containing a Tailscale auth key; if provided, the instance auto-joins Tailscale on boot"
  type        = string
  default     = ""
}

variable "rds_instance_class" {
  description = "RDS instance class (only used when memory_backend = 'rds')"
  type        = string
  default     = "db.t3.micro"
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

variable "oidc_preset" {
  description = "Identity provider preset: auth0, entra, google, okta or generic. Empty keeps legacy defaults."
  type        = string
  default     = ""
}

variable "oidc_token_type" {
  description = "Token the gateway validates: access or id. Empty uses the preset default."
  type        = string
  default     = ""
}

variable "oidc_groups_claim" {
  description = "Claim holding groups, e.g. https://ts-harness/groups. Empty uses the preset default."
  type        = string
  default     = ""
}

variable "oidc_roles_claim" {
  description = "Claim holding roles, e.g. https://ts-harness/roles. Empty uses the preset default."
  type        = string
  default     = ""
}

variable "oidc_allowed_domains" {
  description = "Comma-separated email or hosted domains allowed to sign in. Empty allows any."
  type        = string
  default     = ""
}

variable "oidc_groups_source" {
  description = "Where sign-in groups come from: token, microsoft_graph or google_directory. Empty means token."
  type        = string
  default     = ""

  validation {
    condition     = contains(["", "token", "microsoft_graph", "google_directory"], var.oidc_groups_source)
    error_message = "oidc_groups_source must be empty, token, microsoft_graph or google_directory."
  }
}

variable "identity_directory" {
  description = "Directory synced every 15 minutes: seen_users, auth0_management, scim, microsoft_graph or google_directory. Empty means seen_users."
  type        = string
  default     = ""

  validation {
    condition     = contains(["", "seen_users", "auth0_management", "scim", "microsoft_graph", "google_directory"], var.identity_directory)
    error_message = "identity_directory must be empty or one of seen_users, auth0_management, scim, microsoft_graph, google_directory."
  }
}

variable "rds_skip_final_snapshot" {
  description = "When true, skip final snapshot on RDS instance destruction (dev/test only)"
  type        = bool
  default     = false
}

variable "rds_final_snapshot_identifier" {
  description = "Identifier for the final DB snapshot when the RDS instance is destroyed and rds_skip_final_snapshot is false"
  type        = string
  default     = ""
}

variable "multi_agent_enabled" {
  description = "When true, the bootstrap runs the multi-agent topology. When false, the legacy single-agent path is used. Toggle for staging validation before production promotion."
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
  description = "IANA timezone used for cron job schedules and heartbeat active-hours windows (e.g. 'America/New_York', 'UTC')."
  type        = string
  default     = "UTC"
}

variable "slack_standup_channel" {
  description = "Slack channel ID or name for the daily standup summary cron job (e.g. 'C01234ABCDE' or 'standup')."
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
  description = "List of Slack channel IDs or names that comms-agent is authorised to post to. When empty, defaults to [slack_ops_channel] so the instance always has at least one authorised channel."
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
  description = "Embedding model ID. Default text-embedding-3-small produces 1536-dimensional vectors, matching memories.embedding vector(1536) in database/migrations/001_init_memory.sql."
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

variable "rds_multi_az" {
  description = "Run RDS as Multi-AZ. Defaults on. A dev account can set this false. Conversion waits for the maintenance window unless rds_apply_immediately is true, then fails over for about a minute or two. Database cost roughly doubles."
  type        = bool
  default     = true
}

variable "rds_apply_immediately" {
  description = "Apply RDS changes immediately instead of waiting for the maintenance window. Defaults false so a Multi-AZ conversion is scheduled."
  type        = bool
  default     = false
}

variable "rds_backup_retention_days" {
  description = "Number of days to retain automated RDS backups (1-35). Set to 0 to disable (not recommended for production)."
  type        = number
  default     = 7
}

variable "rds_backup_window" {
  description = "Daily UTC time range for automated RDS backups (format: 'HH:MM-HH:MM'). Must not overlap with maintenance window."
  type        = string
  default     = "03:00-04:00"
}

variable "monthly_budget_limit" {
  description = "Monthly AWS spend limit in USD. An SNS alert fires at 80% and 100%. Set to 0 to disable."
  type        = number
  default     = 500
}

variable "budget_alert_email" {
  description = "Email address for budget and gateway health alerts. Required if monthly_budget_limit > 0."
  type        = string
  default     = ""
}

###############################################################################
# Redis / ElastiCache variables
###############################################################################

variable "enable_redis" {
  description = "When true, provision an ElastiCache Redis cluster for leader election and concurrency counters in the workflow runner."
  type        = bool
  default     = false
}

variable "vpc_id" {
  description = "VPC ID to place the ElastiCache Redis subnet group and security group in. Required when enable_redis = true."
  type        = string
  default     = ""
}

variable "redis_subnet_ids" {
  description = "List of subnet IDs for the ElastiCache Redis subnet group. Required when enable_redis = true."
  type        = list(string)
  default     = []
}

variable "redis_node_type" {
  description = "ElastiCache node instance class. Only applies when enable_redis = true."
  type        = string
  default     = "cache.t3.micro"
}

variable "redis_multi_az" {
  description = "When true, provision a primary and replica node with automatic failover and multi-AZ. Only applies when enable_redis = true."
  type        = bool
  default     = true
}

variable "deployment_profile" {
  description = "single_host keeps one EC2 host; scale_out adds Fargate coding workers and replicated services."
  type        = string
  default     = "single_host"

  validation {
    condition     = contains(["single_host", "scale_out"], var.deployment_profile)
    error_message = "deployment_profile must be single_host or scale_out."
  }
}
