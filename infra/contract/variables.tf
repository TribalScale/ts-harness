# Shared install-contract input names.
#
# These variables document the normalized vocabulary that each provider root
# must accept (directly or via mapping). This file is documentation-oriented;
# provider roots under infra/aws and infra/gcp implement their own variables.tf
# and are NOT expected to module-source this file to create dual-cloud resources.

variable "cloud_provider" {
  description = "Install provider: aws or gcp (exactly one per installation)."
  type        = string
}

variable "environment" {
  description = "Logical environment name (e.g. prod, staging)."
  type        = string
}

variable "project_name" {
  description = "Resource naming / tagging prefix."
  type        = string
}

variable "cloud_region" {
  description = "Provider region for the installation."
  type        = string
}

variable "cloud_zone" {
  description = "Availability zone (AWS) or zone (GCP) for the VM and data disk."
  type        = string
}

variable "secret_prefix" {
  description = "Prefix for provider secret names."
  type        = string
}

variable "notification_email" {
  description = "Email for budget and health alert notifications."
  type        = string
}

variable "monthly_budget_limit" {
  description = "Monthly spend limit in USD. 0 disables budget alerts."
  type        = number
}

# Nested groups are represented here as flat documentation stubs so the
# required names appear in contract tests and operator docs.

variable "managed_db_instance_class" {
  description = "Managed Postgres sizing class (RDS instance class / Cloud SQL tier)."
  type        = string
}

variable "managed_db_name" {
  description = "Managed Postgres database name."
  type        = string
}

variable "managed_db_user" {
  description = "Managed Postgres application username."
  type        = string
}

variable "managed_db_backup_retention_days" {
  description = "Managed Postgres backup retention in days."
  type        = number
}

variable "credentials_encryption_key_id" {
  description = "KMS key id/alias/resource name used for credential encryption (may be created by the root)."
  type        = string
  default     = null
}

variable "vm_instance_type" {
  description = "VM machine type (EC2 instance type or GCE machine type)."
  type        = string
}

variable "vm_root_volume_size_gb" {
  description = "Root disk size in GB."
  type        = number
}

variable "vm_data_disk_size_gb" {
  description = "Persistent data disk size in GB (mounted at /vol/ts-harness-data)."
  type        = number
}
