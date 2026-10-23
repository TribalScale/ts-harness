# ---------------------------------------------------------------------------
# Legacy outputs (retained for existing operators and freeze tests)
# ---------------------------------------------------------------------------

output "instance_id" {
  description = "EC2 instance ID"
  value       = aws_instance.harness.id
}

output "elastic_ip" {
  description = "Elastic IP address assigned to the instance"
  value       = aws_eip.harness.public_ip
}

output "ebs_volume_id" {
  description = "EBS volume ID for persistent data at /vol/ts-harness-data"
  value       = aws_ebs_volume.harness_data.id
}

output "secret_arns" {
  description = "Map of secret key to ARN for all Secrets Manager entries"
  value       = { for k, v in aws_secretsmanager_secret.harness : k => v.arn }
}

output "agent_credentials_kms_key_arn" {
  description = "KMS key ARN used to encrypt Google OAuth tokens at rest"
  value       = aws_kms_key.agent_credentials.arn
}

output "agent_credentials_kms_key_alias" {
  description = "KMS alias written to ts-harness/agent-credentials-kms-key-id in Secrets Manager"
  value       = aws_kms_alias.agent_credentials.name
}

output "rds_address" {
  description = "RDS PostgreSQL hostname only, without port (null when memory_backend is 'local')"
  value       = var.memory_backend == "rds" ? aws_db_instance.harness[0].address : null
}

output "rds_endpoint" {
  description = "RDS PostgreSQL hostname:port (null when memory_backend is 'local')"
  value       = var.memory_backend == "rds" ? aws_db_instance.harness[0].endpoint : null
}

output "rds_master_secret_arn" {
  description = "ARN of the RDS managed master user secret (null when memory_backend is 'local')"
  value       = var.memory_backend == "rds" ? aws_db_instance.harness[0].master_user_secret[0].secret_arn : null
}

output "ssh_command" {
  description = "SSH command to connect to the TS Harness instance"
  value       = "ssh -p 2222 ${var.admin_username}@${aws_eip.harness.public_ip}"
}

# ---------------------------------------------------------------------------
# Install-contract normalized outputs (see infra/contract/outputs.tf)
# ---------------------------------------------------------------------------

output "cloud_provider" {
  description = "Install provider (aws)"
  value       = "aws"
}

output "cloud_region" {
  description = "AWS region of the installation"
  value       = var.aws_region
}

output "compute_resource_id" {
  description = "EC2 instance id"
  value       = aws_instance.harness.id
}

output "data_disk_resource_id" {
  description = "EBS volume id for /vol/ts-harness-data"
  value       = aws_ebs_volume.harness_data.id
}

output "data_disk_device_hint" {
  description = "Block device hint for the data volume"
  value       = "/dev/sdf"
}

output "secret_prefix" {
  description = "Secrets Manager name prefix"
  value       = local.secret_prefix_effective
}

output "managed_db_host" {
  description = "Managed Postgres hostname (null when memory_backend is local)"
  value       = var.memory_backend == "rds" ? aws_db_instance.harness[0].address : null
}

output "managed_db_port" {
  description = "Managed Postgres port (null when memory_backend is local)"
  value       = var.memory_backend == "rds" ? 5432 : null
}

output "managed_db_name" {
  description = "Managed Postgres database name"
  value       = var.memory_backend == "rds" ? aws_db_instance.harness[0].db_name : local.rds_db_name_rendered
}

output "managed_db_user" {
  description = "Managed Postgres username (null when memory_backend is local)"
  value       = var.memory_backend == "rds" ? aws_db_instance.harness[0].username : null
}

output "managed_db_credential_ref" {
  description = "Secrets Manager ARN for managed DB credentials"
  value       = var.memory_backend == "rds" ? aws_db_instance.harness[0].master_user_secret[0].secret_arn : null
}

output "credentials_encryption_key_id" {
  description = "KMS alias for credential encryption"
  value       = aws_kms_alias.agent_credentials.name
}

output "runtime_private_url" {
  description = "Best-effort gateway URL (Tailscale URL is preferred after join)"
  value       = "http://${aws_eip.harness.public_ip}:18789"
}

output "operator_connection_command" {
  description = "SSH command for operators"
  value       = "ssh -p 2222 ${var.admin_username}@${aws_eip.harness.public_ip}"
}

output "monitoring_namespace" {
  description = "CloudWatch metric namespace"
  value       = "TSHarness"
}

output "notification_channel_id" {
  description = "SNS topic ARN for operational alerts"
  value       = aws_sns_topic.alerts.arn
}

# ---------------------------------------------------------------------------
# Scale-out (deployment_profile = scale_out)
# ---------------------------------------------------------------------------

output "coding_worker_repository_url" {
  description = "ECR repository for the coding-worker image (scale_out)"
  value       = try(aws_ecr_repository.coding_worker[0].repository_url, "")
}

output "coding_ecs_cluster" {
  description = "ECS cluster that runs coding workers (scale_out)"
  value       = try(aws_ecs_cluster.coding[0].name, "")
}

output "coding_worker_task_family" {
  description = "Task definition family for coding workers (scale_out)"
  value       = local.ecs_enabled ? local.coding_worker_family : ""
}

output "memory_api_internal_url" {
  description = "Memory API URL that coding workers use through the internal load balancer (scale_out)"
  value       = local.memory_api_worker_url
}

output "skills_bucket" {
  description = "S3 bucket for managed skill files (object_backend=s3)"
  value       = try(aws_s3_bucket.skills[0].id, "")
}

output "workspaces_bucket" {
  description = "S3 bucket for agent workspaces (object_backend=s3)"
  value       = try(aws_s3_bucket.workspaces[0].id, "")
}
