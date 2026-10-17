resource "aws_kms_key" "agent_credentials" {
  description             = "Encrypt Google OAuth tokens at rest for ${var.project_name}"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  tags = {
    Name    = "${var.project_name}-agent-credentials"
    Purpose = "google-oauth-token-encryption"
  }
}

resource "aws_kms_alias" "agent_credentials" {
  name          = "alias/${var.project_name}-agent-credentials"
  target_key_id = aws_kms_key.agent_credentials.key_id
}
