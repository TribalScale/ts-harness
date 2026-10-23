# Install-contract normalized outputs (see infra/contract/outputs.tf)

output "cloud_provider" {
  description = "Install provider (gcp)"
  value       = "gcp"
}

output "cloud_region" {
  description = "GCP region of the installation"
  value       = var.cloud_region
}

output "compute_resource_id" {
  description = "GCE instance id / self-link identity"
  value       = google_compute_instance.harness.id
}

output "data_disk_resource_id" {
  description = "Persistent Disk id for /vol/ts-harness-data"
  value       = google_compute_disk.data.id
}

output "data_disk_device_hint" {
  description = "Block device hint for the data volume"
  value       = local.data_disk_device_hint
}

output "secret_prefix" {
  description = "Secret Manager name prefix"
  value       = local.secret_prefix_effective
}

output "managed_db_host" {
  description = "Managed Postgres hostname (null when memory_backend is local)"
  value       = local.create_sql == 1 ? local.managed_db_host_rendered : null
}

output "managed_db_port" {
  description = "Managed Postgres port (null when memory_backend is local)"
  value       = local.create_sql == 1 ? 5432 : null
}

output "managed_db_name" {
  description = "Managed Postgres database name"
  value       = local.sql_db_name_rendered
}

output "managed_db_user" {
  description = "Managed Postgres username (null when memory_backend is local)"
  value       = local.create_sql == 1 ? local.sql_db_user_rendered : null
}

output "managed_db_credential_ref" {
  description = "Secret Manager resource name for managed DB credentials"
  value       = local.create_sql == 1 ? local.managed_db_credential_ref_rendered : null
}

output "credentials_encryption_key_id" {
  description = "Cloud KMS crypto key id for credential encryption"
  value       = local.credentials_encryption_key_id_effective
}

output "runtime_private_url" {
  description = "Best-effort gateway URL (Tailscale URL is preferred after join)"
  value = (
    var.assign_public_ip && length(google_compute_instance.harness.network_interface[0].access_config) > 0
    ? "http://${google_compute_instance.harness.network_interface[0].access_config[0].nat_ip}:18789"
    : "http://${google_compute_instance.harness.network_interface[0].network_ip}:18789"
  )
}

output "operator_connection_command" {
  description = "gcloud compute ssh command for operators (SSH port 2222 after harden)"
  value       = "gcloud compute ssh ${var.admin_username}@${google_compute_instance.harness.name} --project=${var.gcp_project_id} --zone=${var.cloud_zone} -- -p 2222"
}

output "monitoring_namespace" {
  description = "Cloud Monitoring custom metric namespace"
  value       = "TSHarness"
}

output "notification_channel_id" {
  description = "Cloud Monitoring notification channel id"
  value       = local.create_notify == 1 ? google_monitoring_notification_channel.email[0].id : null
}

# Convenience / GCP-native outputs
output "instance_name" {
  description = "GCE instance name"
  value       = google_compute_instance.harness.name
}

output "instance_self_link" {
  description = "GCE instance self_link"
  value       = google_compute_instance.harness.self_link
}

output "external_ip" {
  description = "Ephemeral external IP when assign_public_ip is true"
  value = (
    var.assign_public_ip && length(google_compute_instance.harness.network_interface[0].access_config) > 0
    ? google_compute_instance.harness.network_interface[0].access_config[0].nat_ip
    : null
  )
}

output "secret_ids" {
  description = "Map of secret key to Secret Manager secret_id"
  value       = { for k, v in google_secret_manager_secret.harness : k => v.secret_id }
}

output "service_account_email" {
  description = "Runtime service account email"
  value       = google_service_account.runtime.email
}

output "skills_bucket" {
  description = "GCS bucket for managed skill files (object_backend=gcs)"
  value       = try(google_storage_bucket.skills[0].name, "")
}

output "workspaces_bucket" {
  description = "GCS bucket for agent workspaces (object_backend=gcs)"
  value       = try(google_storage_bucket.workspaces[0].name, "")
}
