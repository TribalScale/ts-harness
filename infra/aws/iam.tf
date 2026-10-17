data "aws_iam_policy_document" "ec2_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "secrets_read" {
  statement {
    sid       = "AllowReadTSHarnessSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [for s in aws_secretsmanager_secret.harness : s.arn]
  }

  dynamic "statement" {
    for_each = var.memory_backend == "rds" ? [1] : []
    content {
      sid       = "AllowReadRDSMasterSecret"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = [aws_db_instance.harness[0].master_user_secret[0].secret_arn]
    }
  }
}

resource "aws_iam_role" "harness" {
  name               = "${var.project_name}-ec2"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json

  tags = {
    Name = "${var.project_name}-ec2"
  }
}

resource "aws_iam_role_policy" "secrets_read" {
  name   = "${var.project_name}-secrets-read"
  role   = aws_iam_role.harness.id
  policy = data.aws_iam_policy_document.secrets_read.json
}

data "aws_iam_policy_document" "kms_agent_credentials" {
  statement {
    sid    = "AllowOAuthTokenCrypto"
    effect = "Allow"
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
    ]
    resources = [aws_kms_key.agent_credentials.arn]
  }
}

resource "aws_iam_role_policy" "kms_agent_credentials" {
  name   = "${var.project_name}-kms-agent-credentials"
  role   = aws_iam_role.harness.id
  policy = data.aws_iam_policy_document.kms_agent_credentials.json
}

data "aws_iam_policy_document" "cloudwatch_metrics" {
  statement {
    sid       = "AllowGatewayHealthMetric"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["TSHarness"]
    }
  }
}

resource "aws_iam_role_policy" "cloudwatch_metrics" {
  name   = "${var.project_name}-cloudwatch-metrics"
  role   = aws_iam_role.harness.id
  policy = data.aws_iam_policy_document.cloudwatch_metrics.json
}

resource "aws_iam_instance_profile" "harness" {
  name = "${var.project_name}-ec2"
  role = aws_iam_role.harness.name

  tags = {
    Name = "${var.project_name}-ec2"
  }
}

# Bedrock InvokeModel — only created when embeddings_provider = "bedrock".
# Grants the instance role permission to call the configured embedding model.
data "aws_iam_policy_document" "bedrock_invoke_model" {
  count = var.embeddings_provider == "bedrock" ? 1 : 0

  statement {
    sid     = "AllowBedrockEmbeddingInvoke"
    effect  = "Allow"
    actions = ["bedrock:InvokeModel"]
    resources = [
      "arn:aws:bedrock:*::foundation-model/${var.embeddings_model}"
    ]
  }
}

resource "aws_iam_role_policy" "bedrock_invoke_model" {
  count  = var.embeddings_provider == "bedrock" ? 1 : 0
  name   = "${var.project_name}-bedrock-embed"
  role   = aws_iam_role.harness.id
  policy = data.aws_iam_policy_document.bedrock_invoke_model[0].json
}
