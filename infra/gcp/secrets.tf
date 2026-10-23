# Secret catalog parity with infra/aws/secrets.tf (same key names).
# GCP Secret Manager secret IDs cannot contain '/'; logical path
# "${secret_prefix}/${key}" is stored as "${secret_prefix}-${key}".

locals {
  # Map of secret-name -> list of consumer agents.
  secrets_map = {
    "anthropic-api-key"                   = { consumers = ["orchestrator", "dev-agent", "ops-agent", "qa-agent", "review-agent", "analyst-agent", "pm-agent", "comms-agent"] }
    "db-password"                         = { consumers = ["gateway"] }
    "gateway-token"                       = { consumers = ["gateway"] }
    "memory-api-token"                    = { consumers = ["gateway"] }
    "sandbox-worker-token"                = { consumers = ["gateway"] }
    "coding-job-secret"                   = { consumers = ["gateway"] }
    "tailscale-authkey"                   = { consumers = ["gateway"] }
    "github-token"                        = { consumers = ["dev-agent"] }
    "github-app-private-key"              = { consumers = ["gateway"] }
    "github-app-webhook-secret"           = { consumers = ["gateway"] }
    "coding-backend-api-key"              = { consumers = ["dev-agent"] }
    "coding-backend-provider"             = { consumers = ["dev-agent"] }
    "coding-backend-model"                = { consumers = ["dev-agent"] }
    "jira-token"                          = { consumers = ["pm-agent"] }
    "jira-url"                            = { consumers = ["pm-agent"] }
    "jira-username"                       = { consumers = ["pm-agent"] }
    "telegram-token"                      = { consumers = ["orchestrator"] }
    "discord-token"                       = { consumers = ["orchestrator"] }
    "slack-bot-token"                     = { consumers = ["orchestrator", "comms-agent"] }
    "slack-app-token"                     = { consumers = ["orchestrator", "comms-agent"] }
    "slack-allowed-ids"                   = { consumers = ["orchestrator", "comms-agent"] }
    "slack-signing-secret"                = { consumers = ["gateway"] }
    "google-workspace-sa-json"            = { consumers = ["gateway"] }
    "google-oauth-client-id"              = { consumers = ["gateway"] }
    "google-oauth-client-secret"          = { consumers = ["gateway"] }
    "google-oauth-callback-url"           = { consumers = ["gateway"] }
    "google-workspace-default-email"      = { consumers = ["gateway"] }
    "google-comms-email"                  = { consumers = ["gateway"] }
    "google-marketing-email"              = { consumers = ["gateway"] }
    "google-sales-email"                  = { consumers = ["gateway"] }
    "google-pm-email"                     = { consumers = ["gateway"] }
    "google-pm-prd-folder-id"             = { consumers = ["gateway"] }
    "google-delivery-artifacts-folder-id" = { consumers = ["gateway"] }
    "agent-credentials-kms-key-id"        = { consumers = ["gateway"] }
  }

  embeddings_secret_by_provider = {
    openai  = ["openai-api-key"]
    voyage  = ["voyage-api-key"]
    gemini  = ["gemini-api-key"]
    mistral = ["mistral-api-key"]
    bedrock = []
    local   = []
  }

  all_secrets = merge(
    local.secrets_map,
    {
      for key in local.embeddings_secret_by_provider[var.embeddings_provider] :
      key => { consumers = ["orchestrator", "dev-agent", "ops-agent", "qa-agent", "review-agent", "analyst-agent", "pm-agent", "comms-agent"] }
    }
  )

  secret_strings = merge(
    { for key in keys(local.all_secrets) : key => " " },
    {
      "agent-credentials-kms-key-id" = local.credentials_encryption_key_id_effective
      "memory-api-token"             = random_password.memory_api_token.result
      "sandbox-worker-token"         = random_password.sandbox_worker_token.result
      "coding-job-secret"            = random_password.coding_job_secret.result
    }
  )
}

resource "random_password" "memory_api_token" {
  length  = 48
  special = false
}

resource "random_password" "coding_job_secret" {
  length  = 48
  special = false
}

resource "random_password" "sandbox_worker_token" {
  length  = 48
  special = false
}

resource "google_secret_manager_secret" "harness" {
  for_each = local.all_secrets

  secret_id = "${local.secret_prefix_effective}-${each.key}"

  labels = {
    name      = substr(replace("${var.project_name}-${each.key}", "_", "-"), 0, 63)
    consumers = substr(replace(join("-", each.value.consumers), "_", "-"), 0, 63)
  }

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_version" "harness" {
  for_each = local.all_secrets

  secret      = google_secret_manager_secret.harness[each.key].id
  secret_data = local.secret_strings[each.key]

  # Placeholders seed the secret once; operators populate real values in SM.
  lifecycle {
    ignore_changes = [secret_data]
  }
}
