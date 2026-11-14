# Skill files and agent workspaces in GCS, and where runtime state lives.
# state_backend=postgres moves definitions, cron, scheduler state, audit and
# dedup into Cloud SQL; object_backend=gcs moves skill files and workspaces
# into the two buckets below. Files attached to requests (intake) go to a
# third bucket that keeps them 14 days.

locals {
  object_buckets_enabled = var.object_backend == "gcs"
  skills_bucket_name     = "${var.project_name}-skills"
  workspaces_bucket_name = "${var.project_name}-workspaces"
  intake_bucket_name     = "${var.project_name}-intake"
}

resource "terraform_data" "storage_backends" {
  input = {
    state  = var.state_backend
    object = var.object_backend
  }

  lifecycle {
    precondition {
      condition     = var.object_backend == "local" || var.state_backend == "postgres"
      error_message = "object_backend=gcs needs state_backend=postgres: skill files in GCS are indexed in Postgres."
    }
    precondition {
      condition     = var.state_backend == "local" || var.memory_backend == "managed"
      error_message = "state_backend=postgres needs memory_backend=managed (Cloud SQL)."
    }
  }
}

resource "google_storage_bucket" "skills" {
  count                       = local.object_buckets_enabled ? 1 : 0
  name                        = local.skills_bucket_name
  location                    = var.cloud_region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      days_since_noncurrent_time = 90
    }
    action {
      type = "Delete"
    }
  }

  lifecycle_rule {
    condition {
      age            = 30
      matches_prefix = ["skills/drafts/"]
    }
    action {
      type = "Delete"
    }
  }

  labels = {
    purpose = "managed-skill-files"
  }

  depends_on = [google_project_service.required]
}

resource "google_storage_bucket" "workspaces" {
  count                       = local.object_buckets_enabled ? 1 : 0
  name                        = local.workspaces_bucket_name
  location                    = var.cloud_region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      days_since_noncurrent_time = 30
    }
    action {
      type = "Delete"
    }
  }

  labels = {
    purpose = "agent-workspaces"
  }

  depends_on = [google_project_service.required]
}

# Not versioned, so expired files are really deleted.
resource "google_storage_bucket" "intake" {
  count                       = local.object_buckets_enabled ? 1 : 0
  name                        = local.intake_bucket_name
  location                    = var.cloud_region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  # Matches the 14-day sweep used with the local backend (docs/runbooks/intake-sources.md).
  lifecycle_rule {
    condition {
      age            = 14
      matches_prefix = ["intake/"]
    }
    action {
      type = "Delete"
    }
  }

  labels = {
    purpose = "request-intake-files"
  }

  depends_on = [google_project_service.required]
}

resource "google_storage_bucket_iam_member" "skills_read" {
  count  = local.object_buckets_enabled ? 1 : 0
  bucket = google_storage_bucket.skills[0].name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.runtime.email}"
}

# objectCreator cannot overwrite or delete, so approved skill versions stay immutable.
resource "google_storage_bucket_iam_member" "skills_create" {
  count  = local.object_buckets_enabled ? 1 : 0
  bucket = google_storage_bucket.skills[0].name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_storage_bucket_iam_member" "skills_drafts_admin" {
  count  = local.object_buckets_enabled ? 1 : 0
  bucket = google_storage_bucket.skills[0].name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.runtime.email}"

  condition {
    title      = "skill-drafts-only"
    expression = "resource.name.startsWith(\"projects/_/buckets/${local.skills_bucket_name}/objects/skills/drafts/\")"
  }
}

resource "google_storage_bucket_iam_member" "workspaces_admin" {
  count  = local.object_buckets_enabled ? 1 : 0
  bucket = google_storage_bucket.workspaces[0].name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.runtime.email}"
}

# The runtime reads and writes intake files; the lifecycle rule deletes them.
resource "google_storage_bucket_iam_member" "intake_read" {
  count  = local.object_buckets_enabled ? 1 : 0
  bucket = google_storage_bucket.intake[0].name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_storage_bucket_iam_member" "intake_create" {
  count  = local.object_buckets_enabled ? 1 : 0
  bucket = google_storage_bucket.intake[0].name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.runtime.email}"
}
