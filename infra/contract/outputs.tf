# Required normalized install-contract output names.
#
# Each provider root (infra/aws, infra/gcp) must emit these outputs.
# Values below are placeholders for documentation only — do not apply this
# directory as a live Terraform root.

output "cloud_provider" {
  description = "aws or gcp"
  value       = null
}

output "cloud_region" {
  description = "Region of the installation"
  value       = null
}

output "compute_resource_id" {
  description = "EC2 instance id or GCE instance self-link/id"
  value       = null
}

output "data_disk_resource_id" {
  description = "EBS volume id or Persistent Disk id"
  value       = null
}

output "data_disk_device_hint" {
  description = "Device path hint (e.g. /dev/sdf or /dev/disk/by-id/google-…)"
  value       = null
}

output "secret_prefix" {
  description = "Secret catalog prefix"
  value       = null
}

output "managed_db_host" {
  description = "Managed Postgres hostname"
  value       = null
}

output "managed_db_port" {
  description = "Managed Postgres port"
  value       = null
}

output "managed_db_name" {
  description = "Managed Postgres database name"
  value       = null
}

output "managed_db_user" {
  description = "Managed Postgres username"
  value       = null
}

output "managed_db_credential_ref" {
  description = "Provider secret reference for DB credentials"
  value       = null
}

output "credentials_encryption_key_id" {
  description = "KMS key id/alias/resource name for credential encryption"
  value       = null
}

output "runtime_private_url" {
  description = "Private/Tailscale URL for the gateway"
  value       = null
}

output "operator_connection_command" {
  description = "SSH or gcloud compute ssh command"
  value       = null
}

output "monitoring_namespace" {
  description = "Metric namespace / resource type grouping"
  value       = null
}

output "notification_channel_id" {
  description = "SNS topic ARN or GCP notification channel id"
  value       = null
}
