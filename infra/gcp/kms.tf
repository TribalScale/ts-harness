# Cloud KMS key for encrypting Google OAuth / agent credentials at rest.

resource "google_kms_key_ring" "main" {
  count = var.credentials_encryption_key_id == null ? 1 : 0

  name     = "${var.project_name}-keyring"
  location = var.cloud_region

  depends_on = [google_project_service.required]
}

resource "google_kms_crypto_key" "agent_credentials" {
  count = var.credentials_encryption_key_id == null ? 1 : 0

  name            = "${var.project_name}-agent-credentials"
  key_ring        = google_kms_key_ring.main[0].id
  purpose         = "ENCRYPT_DECRYPT"
  rotation_period = "7776000s" # 90 days

  labels = {
    purpose = "google-oauth-token-encryption"
  }

  lifecycle {
    prevent_destroy = false
  }
}

locals {
  credentials_encryption_key_id_effective = coalesce(
    var.credentials_encryption_key_id,
    try(google_kms_crypto_key.agent_credentials[0].id, null)
  )
}
