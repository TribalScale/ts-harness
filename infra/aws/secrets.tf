locals {
  # Map of secret-name -> list of consumer agents.
  # Consumer metadata documents intent for future IAM-per-agent scoping.
  # Current deployment uses a shared instance role; this map drives
  # per-agent auth distribution in scripts/phases/11b-agent-auth-init.sh.
  secrets_map = {
    "anthropic-api-key"                   = { consumers = ["orchestrator", "dev-agent", "ops-agent", "qa-agent", "review-agent", "analyst-agent", "pm-agent", "comms-agent"] }
    "db-password"                         = { consumers = ["gateway"] }
    "gateway-token"                       = { consumers = ["gateway"] }
    "memory-api-token"                    = { consumers = ["gateway"] }
    "sandbox-worker-token"                = { consumers = ["gateway"] }
    "coding-job-secret"                   = { consumers = ["gateway"] }
    "tailscale-authkey"                   = { consumers = ["gateway"] }
    "github-token"                        = { consumers = ["dev-agent"] }
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

  # Provider-conditional embedding API key.
  # bedrock and local providers use credential chains, not a stored secret.
  embeddings_secret_by_provider = {
    openai  = ["openai-api-key"]
    voyage  = ["voyage-api-key"]
    gemini  = ["gemini-api-key"]
    mistral = ["mistral-api-key"]
    bedrock = []
    local   = []
  }

  # Merge base secrets with embedding provider secret (if any).
  all_secrets = merge(
    local.secrets_map,
    {
      for key in local.embeddings_secret_by_provider[var.embeddings_provider] :
      key => { consumers = ["orchestrator", "dev-agent", "ops-agent", "qa-agent", "review-agent", "analyst-agent", "pm-agent", "comms-agent"] }
    }
  )

  # Terraform-managed secrets get real values; others stay as placeholders for manual population.
  secret_strings = merge(
    { for key in keys(local.all_secrets) : key => " " },
    {
      "agent-credentials-kms-key-id" = aws_kms_alias.agent_credentials.name
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

resource "random_password" "sandbox_worker_token" {
  length  = 48
  special = false
}

resource "random_password" "coding_job_secret" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "harness" {
  for_each = local.all_secrets
  name     = "${local.secret_prefix_effective}/${each.key}"

  tags = {
    Name      = "${var.project_name}-${each.key}"
    Consumers = join(" ", each.value.consumers)
  }
}

resource "aws_secretsmanager_secret_version" "harness" {
  for_each      = local.all_secrets
  secret_id     = aws_secretsmanager_secret.harness[each.key].id
  secret_string = local.secret_strings[each.key]

  # Placeholders seed the secret once; operators populate real values in SM.
  # Without ignore_changes, every terraform apply resets values back to " ".
  lifecycle {
    ignore_changes = [secret_string]
  }
}
