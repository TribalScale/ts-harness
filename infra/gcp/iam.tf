# Runtime service account for the GCE VM.
# Grants: Secret Manager access, Cloud KMS crypto, Monitoring metrics, Cloud SQL client.

resource "google_service_account" "runtime" {
  account_id   = substr(replace("${var.project_name}-runtime", "_", "-"), 0, 30)
  display_name = "${var.project_name} runtime"
  description  = "GCE runtime identity for TS Harness secrets, KMS, monitoring, and Cloud SQL"

  depends_on = [google_project_service.required]
}

resource "google_project_iam_member" "secretmanager_accessor" {
  project = var.gcp_project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_project_iam_member" "secretmanager_viewer" {
  project = var.gcp_project_id
  role    = "roles/secretmanager.viewer"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_project_iam_member" "kms_crypto" {
  project = var.gcp_project_id
  role    = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_project_iam_member" "monitoring_writer" {
  project = var.gcp_project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_project_iam_member" "logging_writer" {
  project = var.gcp_project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_project_iam_member" "cloudsql_client" {
  count = local.create_sql

  project = var.gcp_project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}
