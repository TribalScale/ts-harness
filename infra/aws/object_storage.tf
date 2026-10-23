# Skill files and agent workspaces in S3, and where runtime state lives.
# state_backend=postgres moves definitions, cron, scheduler state, audit and
# dedup into the database; object_backend=s3 moves skill files and workspaces
# into the two buckets below. Both default from deployment_profile.

locals {
  state_backend_effective  = var.state_backend != "" ? var.state_backend : (var.deployment_profile == "scale_out" ? "postgres" : "local")
  object_backend_effective = var.object_backend != "" ? var.object_backend : (var.deployment_profile == "scale_out" ? "s3" : "local")
  object_buckets_enabled   = local.object_backend_effective == "s3"
  skills_bucket_name       = "${var.project_name}-skills"
  workspaces_bucket_name   = "${var.project_name}-workspaces"
  skills_bucket_arn        = "arn:aws:s3:::${local.skills_bucket_name}"
  workspaces_bucket_arn    = "arn:aws:s3:::${local.workspaces_bucket_name}"

  object_storage_policy = {
    Version = "2012-10-17"
    Statement = local.object_buckets_enabled ? [
      {
        Sid      = "ListObjectBuckets"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [local.skills_bucket_arn, local.workspaces_bucket_arn]
      },
      {
        Sid      = "ReadWriteSkillsAndWorkspaces"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = ["${local.skills_bucket_arn}/*", "${local.workspaces_bucket_arn}/*"]
      },
      {
        # Approved skill versions are immutable: the runtime may only delete drafts.
        Sid      = "DeleteSkillDraftsOnly"
        Effect   = "Allow"
        Action   = ["s3:DeleteObject"]
        Resource = ["${local.skills_bucket_arn}/skills/drafts/*"]
      },
      {
        Sid      = "DeleteWorkspaceFiles"
        Effect   = "Allow"
        Action   = ["s3:DeleteObject"]
        Resource = ["${local.workspaces_bucket_arn}/*"]
      },
      {
        Sid      = "UseObjectStorageKey"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"]
        Resource = [aws_kms_key.object_storage[0].arn]
      },
    ] : []
  }
}

resource "terraform_data" "storage_backends" {
  input = {
    state  = local.state_backend_effective
    object = local.object_backend_effective
  }

  lifecycle {
    precondition {
      condition     = local.object_backend_effective == "local" || local.state_backend_effective == "postgres"
      error_message = "object_backend=s3 needs state_backend=postgres: skill files in S3 are indexed in Postgres."
    }
    precondition {
      condition     = local.state_backend_effective == "local" || var.memory_backend == "rds"
      error_message = "state_backend=postgres needs memory_backend=rds."
    }
  }
}

resource "aws_kms_key" "object_storage" {
  count                   = local.object_buckets_enabled ? 1 : 0
  description             = "Encrypt skill files and agent workspaces for ${var.project_name}"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  tags = {
    Name    = "${var.project_name}-object-storage"
    Purpose = "skills-and-workspaces"
  }
}

resource "aws_kms_alias" "object_storage" {
  count         = local.object_buckets_enabled ? 1 : 0
  name          = "alias/${var.project_name}-object-storage"
  target_key_id = aws_kms_key.object_storage[0].key_id
}

resource "aws_s3_bucket" "skills" {
  count  = local.object_buckets_enabled ? 1 : 0
  bucket = local.skills_bucket_name

  tags = {
    Name    = local.skills_bucket_name
    Purpose = "managed-skill-files"
  }
}

resource "aws_s3_bucket" "workspaces" {
  count  = local.object_buckets_enabled ? 1 : 0
  bucket = local.workspaces_bucket_name

  tags = {
    Name    = local.workspaces_bucket_name
    Purpose = "agent-workspaces"
  }
}

locals {
  object_buckets = local.object_buckets_enabled ? {
    skills     = aws_s3_bucket.skills[0].id
    workspaces = aws_s3_bucket.workspaces[0].id
  } : {}
}

resource "aws_s3_bucket_versioning" "objects" {
  for_each = local.object_buckets
  bucket   = each.value

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "objects" {
  for_each = local.object_buckets
  bucket   = each.value

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.object_storage[0].arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "objects" {
  for_each                = local.object_buckets
  bucket                  = each.value
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "objects" {
  for_each = local.object_buckets
  bucket   = each.value

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "skills" {
  count  = local.object_buckets_enabled ? 1 : 0
  bucket = aws_s3_bucket.skills[0].id

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }

  rule {
    id     = "expire-abandoned-drafts"
    status = "Enabled"
    filter {
      prefix = "skills/drafts/"
    }
    expiration {
      days = 30
    }
  }

  depends_on = [aws_s3_bucket_versioning.objects]
}

resource "aws_s3_bucket_lifecycle_configuration" "workspaces" {
  count  = local.object_buckets_enabled ? 1 : 0
  bucket = aws_s3_bucket.workspaces[0].id

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }

  depends_on = [aws_s3_bucket_versioning.objects]
}

resource "aws_s3_bucket_policy" "objects_tls_only" {
  for_each = local.object_buckets
  bucket   = each.value
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = ["arn:aws:s3:::${each.value}", "arn:aws:s3:::${each.value}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.objects]
}

resource "aws_iam_role_policy" "object_storage" {
  count  = local.object_buckets_enabled ? 1 : 0
  name   = "${var.project_name}-object-storage"
  role   = aws_iam_role.harness.id
  policy = jsonencode(local.object_storage_policy)
}
